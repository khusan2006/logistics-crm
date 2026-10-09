"""TCP receiver for scales on the network.

A scale's RS232 output goes into a serial-to-Ethernet converter (USR-TCP232-410S and
the like) set to TCP Client. The converter connects HERE, sends its Device ID once,
then forwards the scale's lines as they come — `wn001.171kg` many times a second.
This process checks the ID against the active Scale rows, reads the lines by the
rules in crm.tarozi and keeps each scale's latest weight on its row, which is what
the CRM reads. It runs as its own Railway service (`PROCESS=tarozi`, see bin/start)
behind a TCP proxy, so the website never shares a process with an open port.

Every connection is untrusted until it names a scale: wrong ID, nothing within
AUTH_TIMEOUT, or more bytes than an ID before one matches — closed. After that a
silent connection is dropped after IDLE_TIMEOUT, which is what clears the half-open
sockets a router reboot leaves behind; the converter reconnects by itself.
"""
import asyncio
import os
from contextlib import suppress
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.db import InterfaceError, OperationalError, connection
from django.utils import timezone

from crm.models import Scale
from crm.tarozi import Stability, decode, split_frames

AUTH_TIMEOUT = 10        # seconds to send the Device ID
IDLE_TIMEOUT = 60        # seconds of silence before a connection is dropped
MAX_CONNECTIONS = 50
WRITE_EVERY = 1.0        # seconds between last_seen refreshes while nothing changes
STABLE_NEED = 5          # same value this many times in a row = barqaror (as on the page)
MAX_KG = Decimal("999999999")


def _update(pk, **fields):
    """One UPDATE on a scale row, surviving a database connection that went away
    while the receiver sat idle — the process lives for weeks, the connection may not."""
    try:
        return Scale.objects.filter(pk=pk).update(**fields)
    except (OperationalError, InterfaceError):
        connection.close()
        return Scale.objects.filter(pk=pk).update(**fields)


def _active_ids():
    connection.close_if_unusable_or_obsolete()
    return {s.device_id.encode(): s for s in Scale.objects.filter(is_active=True)}


def _all_offline():
    return Scale.objects.filter(online=True).update(online=False, peer="")


update = sync_to_async(_update)
active_ids = sync_to_async(_active_ids)
all_offline = sync_to_async(_all_offline)


class Receiver:
    def __init__(self, log=print):
        self.log = log
        self.live = {}          # scale pk → the writer currently speaking for it
        self.connections = 0
        self.server = None

    async def start(self, host, port):
        # Rows the previous container left `online` are not online any more.
        await all_offline()
        self.server = await asyncio.start_server(self.handle, host, port)
        return self.server

    async def handle(self, reader, writer):
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "?"
        if self.connections >= MAX_CONNECTIONS:
            self.log(f"{peer}: rad etildi — ulanishlar soni chegarada")
            writer.close()
            return
        self.connections += 1
        scale = None
        try:
            scale, buf = await self.authenticate(reader, peer)
            if scale is None:
                return
            previous = self.live.get(scale.pk)
            if previous is not None:
                # The same converter reconnecting: the new socket wins.
                previous.close()
            self.live[scale.pk] = writer
            await update(scale.pk, online=True, peer=peer, last_seen=timezone.now())
            self.log(f"{scale.name}: ulandi ({peer})")
            await self.read_loop(scale, reader, buf, peer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.connections -= 1
            if scale is not None and self.live.get(scale.pk) is writer:
                del self.live[scale.pk]
                await update(scale.pk, online=False)
                self.log(f"{scale.name}: uzildi ({peer})")
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def authenticate(self, reader, peer):
        """Match the first bytes against the active Device IDs. The converter sends
        the ID raw, with no line ending, and the first weight line may ride in the
        same packet — so it is a prefix match on bytes, and what follows is kept."""
        ids = await active_ids()
        longest = max(map(len, ids), default=0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + AUTH_TIMEOUT
        buf = bytearray()
        while True:
            for key, scale in ids.items():
                if buf.startswith(key):
                    del buf[:len(key)]
                    return scale, buf
            if not ids or len(buf) >= longest:
                self.log(f"{peer}: rad etildi — Device ID noto'g'ri")
                return None, None
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.log(f"{peer}: rad etildi — Device ID kelmadi")
                return None, None
            try:
                chunk = await asyncio.wait_for(reader.read(256), remaining)
            except TimeoutError:
                self.log(f"{peer}: rad etildi — Device ID kelmadi")
                return None, None
            if not chunk:
                return None, None
            buf += chunk

    async def read_loop(self, scale, reader, buf, peer):
        stability = Stability(STABLE_NEED)
        loop = asyncio.get_running_loop()
        written = None
        written_at = 0.0
        while True:
            # Read what is already buffered BEFORE waiting: the lines that came in the
            # same packet as the Device ID are here on the first pass.
            latest = None
            for frame in split_frames(buf):
                reading = decode(frame)
                if reading is None or abs(reading.kg) > MAX_KG:
                    continue
                kg = reading.kg.quantize(Decimal("0.001"))
                latest = (kg, stability.push(kg, reading.flag), reading.raw[:128])
            if latest is not None:
                now = loop.time()
                if latest[:2] != written or now - written_at >= WRITE_EVERY:
                    kg, stable, raw = latest
                    rows = await update(scale.pk, last_kg=kg, last_stable=stable, last_raw=raw,
                                        last_seen=timezone.now(), online=True)
                    if not rows:
                        self.log(f"{scale.name}: tarozi o'chirilgan — ulanish yopildi")
                        return
                    written, written_at = latest[:2], now
            try:
                chunk = await asyncio.wait_for(reader.read(1024), IDLE_TIMEOUT)
            except TimeoutError:
                self.log(f"{scale.name}: {IDLE_TIMEOUT} s jimlik — ulanish yopildi")
                return
            if not chunk:
                return
            buf += chunk


class Command(BaseCommand):
    help = "Tarozilarning TCP ulanishlarini qabul qiladi (USR-TCP232 va shu kabi konvertorlar)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=int(os.environ.get("TAROZI_PORT", "9000")))

    def handle(self, *args, **options):
        asyncio.run(self.serve(options["host"], options["port"]))

    async def serve(self, host, port):
        receiver = Receiver(log=lambda msg: self.stdout.write(msg))
        server = await receiver.start(host, port)
        self.stdout.write(f"Tarozi qabul qiluvchi {host}:{port} da tinglayapti")
        async with server:
            await server.serve_forever()
