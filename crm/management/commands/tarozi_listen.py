"""TCP receiver for scales on the network.

A scale's RS232 output goes into a serial-to-Ethernet converter (USR-TCP232-410S and
the like) set to TCP Client. The converter connects HERE and forwards the scale's
lines as they come — `wn001.171kg` many times a second. This process keeps each
scale's latest weight on its Scale row, which is what the CRM reads. It runs as its
own Railway service (`PROCESS=tarozi`, see bin/start) behind a TCP proxy, so the
website never shares a process with an open port.

Who is on the other end. A converter that can send a Device ID first is matched by
it. One that cannot is taken for the scale marked "ID shart emas" — and since
Railway's proxy hides every client's address (they all arrive as 100.64.x.x), such a
connection can only be judged by how it behaves:

  * it must deliver a valid weight line within FIRST_READING_TIMEOUT, or it is closed
    before it ever counts as the scale;
  * while the scale's current connection is healthy (a reading within HEALTHY_FOR),
    a new ID-less connection is refused — a stranger cannot push the real converter
    off, and a reconnecting converter gets in as soon as its dead socket goes quiet;
  * MAX_BAD_FRAMES invalid lines in a row, or more than MAX_BYTES_PER_SECOND, closes it.

What keeps the process itself safe whoever connects: at most MAX_CONNECTIONS open
and MAX_NEW_PER_MINUTE new ones a minute, a bounded buffer per connection (see
crm.tarozi.MAX_FRAME), at most one database write per MIN_WRITE_GAP per scale, and
rejections summed into one log line a minute instead of one line each. A silent
connection is dropped after IDLE_TIMEOUT, which is what clears the half-open sockets
a router reboot leaves behind; the converter reconnects by itself.
"""
import asyncio
import os
from collections import Counter, deque
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.db import InterfaceError, OperationalError, connection
from django.utils import timezone

from crm.models import Scale
from crm.tarozi import Stability, decode, split_frames

AUTH_TIMEOUT = 5             # seconds to send a Device ID, for a converter that sends one
FIRST_READING_TIMEOUT = 5    # seconds FROM CONNECTING for an ID-less one to show a weight
HEALTHY_FOR = 10             # a connection with a reading this recent keeps its scale
IDLE_TIMEOUT = 60            # seconds of silence before a connection is dropped
MAX_CONNECTIONS = 20
MAX_NEW_PER_MINUTE = 60
MAX_BYTES_PER_SECOND = 4096  # a scale sends ~200 B/s; twenty times that is not a scale
MAX_BAD_FRAMES = 20          # invalid lines in a row before the connection is closed
MAX_LINE = 64                # a weight line is ~11 bytes; longer is not one
MIN_WRITE_GAP = 0.25         # at most 4 writes a second per scale, however fast it changes
WRITE_EVERY = 1.0            # last_seen refresh while nothing changes
STABLE_NEED = 5              # same value this many times in a row = barqaror (as on the page)
MAX_KG = Decimal("100000")   # 100 t — above any scale here; past it a "weight" is noise
REJECT_LOG_EVERY = 60        # seconds between rejection summaries


def _update(pk, **fields):
    """One UPDATE on a scale row, surviving a database connection that went away
    while the receiver sat idle — the process lives for weeks, the connection may not."""
    try:
        return Scale.objects.filter(pk=pk).update(**fields)
    except (OperationalError, InterfaceError):
        connection.close()
        return Scale.objects.filter(pk=pk).update(**fields)


def _active_scales():
    """The active Device IDs, and the one scale (if any) that takes connections
    sending no ID at all."""
    connection.close_if_unusable_or_obsolete()
    scales = list(Scale.objects.filter(is_active=True))
    ids = {s.device_id.encode(): s for s in scales}
    open_scale = next((s for s in scales if not s.require_id), None)
    return ids, open_scale


def _all_offline():
    return Scale.objects.filter(online=True).update(online=False, peer="")


update = sync_to_async(_update)
active_scales = sync_to_async(_active_scales)
all_offline = sync_to_async(_all_offline)


@dataclass
class Live:
    """The connection currently speaking for a scale."""
    writer: asyncio.StreamWriter
    last_reading: float


class RejectLog:
    """Rejections, summed: the first goes out at once, the rest once a minute — a
    port scan or a flood must not become a flood of log lines."""

    def __init__(self, log):
        self.log = log
        self.counts = Counter()
        self.flushed = None

    def add(self, reason, now):
        self.counts[reason] += 1
        if self.flushed is None or now - self.flushed >= REJECT_LOG_EVERY:
            parts = ", ".join(f"{r} — {n}" for r, n in self.counts.most_common())
            self.log(f"rad etildi: {parts}")
            self.counts.clear()
            self.flushed = now


class Receiver:
    def __init__(self, log=print):
        self.log = log
        self.rejects = RejectLog(log)
        self.live = {}              # scale pk → Live
        self.connections = 0
        self.recent = deque()       # start times of connections in the last minute
        self.server = None

    async def start(self, host, port):
        # Rows the previous container left `online` are not online any more.
        await all_offline()
        self.server = await asyncio.start_server(self.handle, host, port)
        return self.server

    # ---- bookkeeping --------------------------------------------------------------

    def _admit(self, now):
        while self.recent and now - self.recent[0] >= 60:
            self.recent.popleft()
        if len(self.recent) >= MAX_NEW_PER_MINUTE or self.connections >= MAX_CONNECTIONS:
            return False
        self.recent.append(now)
        return True

    def _healthy(self, pk, now):
        live = self.live.get(pk)
        return live is not None and now - live.last_reading < HEALTHY_FOR

    def _take(self, scale, writer, now):
        previous = self.live.get(scale.pk)
        if previous is not None and previous.writer is not writer:
            previous.writer.close()
        self.live[scale.pk] = Live(writer, now)

    def _reject(self, reason):
        self.rejects.add(reason, asyncio.get_running_loop().time())

    # ---- one connection -------------------------------------------------------------

    async def handle(self, reader, writer):
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "?"
        loop = asyncio.get_running_loop()
        connected_at = loop.time()
        if not self._admit(connected_at):
            self._reject("ulanishlar juda ko'p")
            writer.close()
            return
        self.connections += 1
        scale = None
        try:
            scale, buf, by_id = await self.authenticate(reader)
            if scale is None:
                return
            if by_id:
                # A Device ID proves who it is: the new socket wins over an old one.
                self._take(scale, writer, loop.time())
                await update(scale.pk, online=True, peer=peer, last_seen=timezone.now())
                self.log(f"{scale.name}: ulandi ({peer})")
            elif self._healthy(scale.pk, loop.time()):
                self._reject("tarozi band (ID'siz ikkinchi ulanish)")
                return
            await self.read_loop(scale, reader, writer, buf, peer, by_id, connected_at)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.connections -= 1
            live = self.live.get(scale.pk) if scale is not None else None
            if live is not None and live.writer is writer:
                del self.live[scale.pk]
                await update(scale.pk, online=False)
                self.log(f"{scale.name}: uzildi ({peer})")
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def authenticate(self, reader):
        """→ (scale, the bytes after the ID, whether an ID was given).

        A Device ID is matched as a prefix of the raw bytes — the converter sends it
        with no line ending, and the first weight line may ride in the same packet.
        Bytes that cannot be the start of any ID belong to the "ID shart emas" scale,
        kept whole as its first data; with no such scale they are refused."""
        ids, open_scale = await active_scales()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + AUTH_TIMEOUT
        buf = bytearray()
        while True:
            for key, scale in ids.items():
                if buf.startswith(key):
                    del buf[:len(key)]
                    return scale, buf, True
            maybe_id = any(key.startswith(bytes(buf)) for key in ids)
            if buf and not maybe_id:
                return self._no_id(open_scale, buf, "Device ID noto'g'ri")
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._no_id(open_scale, buf, "Device ID kelmadi")
            try:
                chunk = await asyncio.wait_for(reader.read(256), remaining)
            except TimeoutError:
                return self._no_id(open_scale, buf, "Device ID kelmadi")
            if not chunk:
                return None, None, False
            buf += chunk

    def _no_id(self, open_scale, buf, reason):
        if open_scale is not None:
            return open_scale, buf, False
        self._reject(reason)
        return None, None, False

    async def read_loop(self, scale, reader, writer, buf, peer, registered, started):
        """Read lines until the connection ends. An ID-less connection becomes the
        scale's (`registered`) only with its first valid weight, and only if no
        healthy connection holds the scale by then. `started` is when it CONNECTED:
        the wait for a Device ID counts against the same deadline, so a silent
        connection holds a slot for FIRST_READING_TIMEOUT, not for twice that."""
        stability = Stability(STABLE_NEED)
        loop = asyncio.get_running_loop()
        written, written_at = None, 0.0
        pending = None      # a change held back by MIN_WRITE_GAP, written when it ends
        bad = 0
        window_at, window_bytes = started, 0

        async def write(reading, now):
            nonlocal written, written_at, pending
            kg, stable, raw = reading
            rows = await update(scale.pk, last_kg=kg, last_stable=stable, last_raw=raw,
                                last_seen=timezone.now(), online=True, peer=peer)
            written, written_at, pending = reading[:2], now, None
            return rows

        while True:
            # Read what is already buffered BEFORE waiting: the lines that came in the
            # same packet as the Device ID are here on the first pass.
            latest = None
            for frame in split_frames(buf):
                reading = decode(frame) if len(frame) <= MAX_LINE else None
                if reading is None or abs(reading.kg) > MAX_KG:
                    bad += 1
                    if bad >= MAX_BAD_FRAMES:
                        self._reject("noto'g'ri ma'lumot")
                        return
                    continue
                bad = 0
                kg = reading.kg.quantize(Decimal("0.001"))
                latest = (kg, stability.push(kg, reading.flag), reading.raw[:128])
            if latest is not None:
                now = loop.time()
                if not registered:
                    if self._healthy(scale.pk, now):
                        self._reject("tarozi band (ID'siz ikkinchi ulanish)")
                        return
                    self._take(scale, writer, now)
                    registered = True
                    written = None
                    self.log(f"{scale.name}: ulandi, ID'siz ({peer})")
                self.live[scale.pk].last_reading = now
                changed = latest[:2] != written
                if (changed and now - written_at >= MIN_WRITE_GAP) or now - written_at >= WRITE_EVERY:
                    if not await write(latest, now):
                        self.log(f"{scale.name}: tarozi o'chirilgan — ulanish yopildi")
                        return
                elif changed:
                    pending = latest
            # How long to wait for more: until a held-back change is due, until the
            # first-weight deadline of a connection not yet trusted, or the idle limit.
            if pending is not None:
                wait = max(written_at + MIN_WRITE_GAP - loop.time(), 0)
            elif registered:
                wait = IDLE_TIMEOUT
            else:
                wait = started + FIRST_READING_TIMEOUT - loop.time()
                if wait <= 0:
                    self._reject(f"{FIRST_READING_TIMEOUT} s ichida vazn kelmadi")
                    return
            try:
                chunk = await asyncio.wait_for(reader.read(1024), wait)
            except TimeoutError:
                if pending is not None:
                    # A scale that goes quiet right after a change (one line per
                    # button press) must still have that change saved.
                    if not await write(pending, loop.time()):
                        return
                    continue
                if registered:
                    self.log(f"{scale.name}: {IDLE_TIMEOUT} s jimlik — ulanish yopildi")
                else:
                    self._reject(f"{FIRST_READING_TIMEOUT} s ichida vazn kelmadi")
                return
            if not chunk:
                return
            now = loop.time()
            if now - window_at >= 1:
                window_at, window_bytes = now, 0
            window_bytes += len(chunk)
            if window_bytes > MAX_BYTES_PER_SECOND:
                self._reject("juda ko'p ma'lumot")
                if registered:
                    self.log(f"{scale.name}: juda ko'p ma'lumot — ulanish yopildi")
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
