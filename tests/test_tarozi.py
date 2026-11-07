"""Tarozi sinovi — the scale bench page. Its work happens in static/js/tarozi.js;
the server only has to serve it, to an admin or a tarozichi, and keep a tarozichi
away from everything else."""
import pytest
from django.test import Client

from accounts.models import User


@pytest.fixture
def tarozichi_client(db):
    user = User.objects.create_user(
        username="tarozichi", password="x-test-only", role=User.Role.TAROZICHI)
    client = Client()
    client.force_login(user)
    return client


def test_admin_gets_the_bench_with_its_script(admin_client):
    resp = admin_client.get("/tarozi/test/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "data-tarozi" in html
    assert "js/tarozi.js" in html


def test_tarozichi_gets_the_bench(tarozichi_client):
    assert tarozichi_client.get("/tarozi/test/").status_code == 200


def test_tarozichi_lands_on_the_bench_after_login(tarozichi_client):
    resp = tarozichi_client.get("/")
    assert resp.status_code == 302
    assert resp.url == "/tarozi/test/"


@pytest.mark.parametrize("url", ["/shipments/", "/ombor/", "/sales/", "/kassa/", "/users/"])
def test_tarozichi_sees_nothing_else(tarozichi_client, url):
    assert tarozichi_client.get(url).status_code == 403


def test_tarozichi_menu_holds_only_the_bench(tarozichi_client):
    html = tarozichi_client.get("/tarozi/test/").content.decode()
    assert 'href="/tarozi/test/"' in html
    for other in ('href="/shipments/"', 'href="/ombor/"', 'href="/kassa/"'):
        assert other not in html


def test_skladchi_cannot_open_the_bench(skladchi_client):
    assert skladchi_client.get("/tarozi/test/").status_code == 403


def test_translator_cannot_open_the_bench(translator_client):
    assert translator_client.get("/tarozi/test/").status_code == 403
