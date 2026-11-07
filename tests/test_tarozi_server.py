"""Network scales: the frame/decode rules in crm.tarozi, the Scale endpoints the
Tarozi sinovi page polls, and the `tarozi_listen` receiver over a real socket."""
import asyncio
from contextlib import suppress
from decimal import Decimal

import pytest
from django.test import Client

from accounts.models import User
from crm.management.commands.tarozi_listen import Receiver
from crm.models import Scale
from crm.tarozi import Stability, decode, split_frames


# ---- frames and readings ------------------------------------------------------

def test_lines_arriving_in_pieces_are_cut_whole():
    buf = bytearray()
    buf += b"wn001.17"
    assert split_frames(buf) == []
    buf += b"1kg\r\nwn001.172kg\r\nwn0"
    assert split_frames(buf) == [b"wn001.171kg", b"wn001.172kg"]
    assert bytes(buf) == b"wn0"


def test_the_clients_scale_line_reads_as_kg():
    r = decode(b"wn001.171kg")
    assert r.kg == Decimal("1.171") and r.flag is None


def test_st_us_text_carries_the_scales_own_flag():
    assert decode(b"ST,GS,+0012450kg").flag == "ST"
    assert decode(b"US,NT,-  12.5 kg").kg == Decimal("-12.5")
    assert decode(b"OL,GS,+999999kg") is None


def test_yaohua_a9_frame_and_its_checksum():
    frame = bytes.fromhex("02 2B 30 31 32 34 35 30 30 31 39 03")
    assert split_frames(bytearray(frame * 2)) == [frame, frame]
    assert decode(frame).kg == Decimal("12450")
    assert decode(bytes.fromhex("02 2B 30 31 32 34 35 30 30 31 38 03")) is None


def test_a_runaway_stream_is_dropped_not_held():
    buf = bytearray(b"x" * 500)
    assert split_frames(buf) == []
    assert len(buf) == 0


def test_stable_after_the_same_value_n_times():
    s = Stability(3)
    assert [s.push(Decimal("1.171"), None) for _ in range(3)] == [False, False, True]
    assert s.push(Decimal("1.172"), None) is False
    assert s.push(Decimal("5"), "ST") is True


# ---- endpoints ---------------------------------------------------------------

@pytest.fixture
def tarozichi_client(db):
    user = User.objects.create_user(username="tarozichi", password="x-test-only",
                                    role=User.Role.TAROZICHI)
    client = Client()
    client.force_login(user)
    return client


def test_tarozichi_adds_lists_and_removes_a_scale(tarozichi_client):
    resp = tarozichi_client.post("/tarozi/scales/new/", {"name": "Ombor 1"})
    assert resp.status_code == 201
    made = resp.json()
    assert made["device_id"].startswith("TRZ-") and len(made["device_id"]) == 24

    scales = tarozichi_client.get("/tarozi/scales/").json()["scales"]
    assert [s["name"] for s in scales] == ["Ombor 1"]
    assert scales[0]["kg"] is None and scales[0]["online"] is False

    assert tarozichi_client.post(f"/tarozi/scales/{made['id']}/delete/").status_code == 200
    assert not Scale.objects.exists()


def test_a_scale_needs_a_name(tarozichi_client):
    assert tarozichi_client.post("/tarozi/scales/new/", {"name": "  "}).status_code == 400


def test_skladchi_cannot_see_network_scales(skladchi_client):
    assert skladchi_client.get("/tarozi/scales/").status_code == 403
    assert skladchi_client.post("/tarozi/scales/new/", {"name": "x"}).status_code == 403


def test_bench_page_shows_the_receiver_address(admin_client, settings):
    settings.TAROZI_PUBLIC_ADDR = "abc.proxy.rlwy.net:12345"
    html = admin_client.get("/tarozi/test/").content.decode()
    assert "abc.proxy.rlwy.net:12345" in html
    assert "data-tz-net" in html


# ---- the receiver, over a real socket --------------------------------------------

async def _with_receiver(scenario):
    receiver = Receiver(log=lambda msg: None)
    server = await receiver.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        return await scenario(port)
    finally:
        server.close()
        await server.wait_closed()


async def _wait_for(check, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await check():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
def test_converter_with_its_id_feeds_the_latest_weight():
    scale = Scale.objects.create(name="Ombor 1")

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # The 410S sends the ID raw and the first line may share its packet.
        writer.write(scale.device_id.encode() + b"wn001.17")
        await writer.drain()
        writer.write(b"1kg\r\n" + b"wn001.171kg\r\n" * 5)
        await writer.drain()

        async def landed():
            row = await Scale.objects.aget(pk=scale.pk)
            return row.online and row.last_kg == Decimal("1.171") and row.last_stable
        ok = await _wait_for(landed)
        writer.close()
        await writer.wait_closed()

        async def offline():
            return not (await Scale.objects.aget(pk=scale.pk)).online
        return ok, await _wait_for(offline)

    landed, went_offline = asyncio.run(_with_receiver(scenario))
    assert landed
    assert went_offline
    row = Scale.objects.get(pk=scale.pk)
    assert row.last_raw == "wn001.171kg" and row.peer.startswith("127.0.0.1:")


@pytest.mark.django_db(transaction=True)
def test_a_wrong_device_id_is_disconnected():
    Scale.objects.create(name="Ombor 1")

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"TRZ-NOTAREALIDXXXXXXXXXwn001.171kg\r\n")
        await writer.drain()
        closed = await asyncio.wait_for(reader.read(10), 3)
        writer.close()
        return closed

    assert asyncio.run(_with_receiver(scenario)) == b""
    assert Scale.objects.get().last_kg is None


@pytest.mark.django_db(transaction=True)
def test_a_retired_scale_is_refused():
    scale = Scale.objects.create(name="Eski", is_active=False)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(scale.device_id.encode() + b"wn001.171kg\r\n")
        await writer.drain()
        closed = await asyncio.wait_for(reader.read(10), 3)
        writer.close()
        return closed

    assert asyncio.run(_with_receiver(scenario)) == b""


# ---- "ID shart emas": a converter not sending a Device ID -------------------------

def test_opening_one_scale_closes_the_others(tarozichi_client):
    a = Scale.objects.create(name="A")
    b = Scale.objects.create(name="B")
    assert tarozichi_client.post(f"/tarozi/scales/{a.pk}/open/").json()["require_id"] is False
    assert tarozichi_client.post(f"/tarozi/scales/{b.pk}/open/").json()["require_id"] is False
    a.refresh_from_db()
    assert a.require_id is True
    assert tarozichi_client.post(f"/tarozi/scales/{b.pk}/open/").json()["require_id"] is True


@pytest.mark.django_db(transaction=True)
def test_a_converter_without_id_feeds_the_open_scale():
    Scale.objects.create(name="ID bilan")
    open_scale = Scale.objects.create(name="ID siz", require_id=False)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # No ID at all: the very first bytes are already weight lines.
        writer.write(b"wn001.171kg\r\n" * 6)
        await writer.drain()

        async def landed():
            row = await Scale.objects.aget(pk=open_scale.pk)
            return row.last_kg == Decimal("1.171") and row.last_stable
        ok = await _wait_for(landed)
        writer.close()
        return ok

    assert asyncio.run(_with_receiver(scenario))
    assert Scale.objects.get(name="ID bilan").last_kg is None


@pytest.mark.django_db(transaction=True)
def test_with_an_open_scale_a_real_id_still_goes_to_its_own_scale():
    own = Scale.objects.create(name="O'zi")
    Scale.objects.create(name="ID siz", require_id=False)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(own.device_id.encode() + b"wn025.410kg\r\n" * 6)
        await writer.drain()

        async def landed():
            return (await Scale.objects.aget(pk=own.pk)).last_kg == Decimal("25.41")
        ok = await _wait_for(landed)
        writer.close()
        return ok

    assert asyncio.run(_with_receiver(scenario))
    assert Scale.objects.get(name="ID siz").last_kg is None


# ---- hardening: what an ID-less connection must prove, and the limits -------------

from crm.management.commands import tarozi_listen  # noqa: E402


async def _closed_within(reader, seconds=3):
    """True when the server closes the connection within `seconds`."""
    try:
        return await asyncio.wait_for(reader.read(10), seconds) == b""
    except (TimeoutError, ConnectionError):
        return False


@pytest.mark.django_db(transaction=True)
def test_a_stranger_cannot_push_the_live_converter_off():
    scale = Scale.objects.create(name="Tarozi 1", require_id=False)

    async def scenario(port):
        real_r, real_w = await asyncio.open_connection("127.0.0.1", port)
        real_w.write(b"wn001.171kg\r\n" * 6)
        await real_w.drain()
        await _wait_for(lambda: _row_kg(scale.pk, "1.171"))

        intruder_r, intruder_w = await asyncio.open_connection("127.0.0.1", port)
        intruder_w.write(b"wn099.999kg\r\n" * 6)
        await intruder_w.drain()
        refused = await _closed_within(intruder_r)

        real_w.write(b"wn002.000kg\r\n" * 6)
        await real_w.drain()
        kept = await _wait_for(lambda: _row_kg(scale.pk, "2"))
        real_w.close()
        return refused, kept

    refused, kept = asyncio.run(_with_receiver(scenario))
    assert refused, "the second ID-less connection must be refused"
    assert kept, "the real converter keeps feeding its scale"


async def _row_kg(pk, kg):
    return (await Scale.objects.aget(pk=pk)).last_kg == Decimal(kg)


@pytest.mark.django_db(transaction=True)
def test_garbage_is_closed_and_never_becomes_the_scale():
    scale = Scale.objects.create(name="Tarozi 1", require_id=False)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\n" + b"hello\r\n" * 30)
        await writer.drain()
        return await _closed_within(reader)

    assert asyncio.run(_with_receiver(scenario))
    row = Scale.objects.get(pk=scale.pk)
    assert not row.online and row.last_kg is None


@pytest.mark.django_db(transaction=True)
def test_an_id_less_connection_must_show_a_weight_quickly(monkeypatch):
    monkeypatch.setattr(tarozi_listen, "FIRST_READING_TIMEOUT", 0.5)
    Scale.objects.create(name="Tarozi 1", require_id=False)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x")          # says something, never a weight
        await writer.drain()
        return await _closed_within(reader, 3)

    assert asyncio.run(_with_receiver(scenario))


@pytest.mark.django_db(transaction=True)
def test_a_flood_of_bytes_is_cut_off(monkeypatch):
    monkeypatch.setattr(tarozi_listen, "MAX_BYTES_PER_SECOND", 300)
    Scale.objects.create(name="Tarozi 1", require_id=False)

    async def scenario(port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"wn001.171kg\r\n" * 6)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.write(b"wn001.171kg\r\n" * 200)    # ~2.6 KB at once
        with suppress(ConnectionError):
            await writer.drain()
        return await _closed_within(reader)

    assert asyncio.run(_with_receiver(scenario))


@pytest.mark.django_db(transaction=True)
def test_too_many_new_connections_a_minute_are_refused(monkeypatch):
    monkeypatch.setattr(tarozi_listen, "MAX_NEW_PER_MINUTE", 2)
    Scale.objects.create(name="Tarozi 1")       # requires an ID: nobody gets in anyway

    async def scenario(port):
        conns = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]
        third_reader = conns[2][0]
        refused_fast = await _closed_within(third_reader, 1)
        for _, w in conns:
            w.close()
        return refused_fast

    assert asyncio.run(_with_receiver(scenario))


def test_rejections_are_summed_not_logged_one_by_one():
    lines = []
    log = tarozi_listen.RejectLog(lines.append)
    for t in range(100):
        log.add("noto'g'ri ma'lumot", now=t * 0.1)     # 100 in 10 s
    assert len(lines) == 1
    log.add("noto'g'ri ma'lumot", now=61)
    assert len(lines) == 2 and "— 100" in lines[1]


@pytest.mark.django_db(transaction=True)
def test_a_silent_connection_is_closed_within_one_deadline(monkeypatch):
    # The wait for a Device ID and the wait for a first weight are ONE deadline
    # from connecting — a port scanner that says nothing must not hold a slot twice.
    monkeypatch.setattr(tarozi_listen, "AUTH_TIMEOUT", 0.6)
    monkeypatch.setattr(tarozi_listen, "FIRST_READING_TIMEOUT", 0.6)
    Scale.objects.create(name="ID bilan")
    Scale.objects.create(name="Tarozi 1", require_id=False)

    async def scenario(port):
        loop = asyncio.get_running_loop()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        t = loop.time()
        closed = await _closed_within(reader, 3)
        return closed, loop.time() - t

    closed, took = asyncio.run(_with_receiver(scenario))
    assert closed and took < 1.0
