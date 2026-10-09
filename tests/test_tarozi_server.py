"""Network scales: the frame/decode rules in crm.tarozi, the Scale endpoints the
Tarozi sinovi page polls, and the `tarozi_listen` receiver over a real socket."""
import asyncio
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
