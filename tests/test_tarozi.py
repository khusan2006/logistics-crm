"""Tarozi sinovi — the scale bench page. Its work happens in static/js/tarozi.js;
the server only has to serve it, and only to an admin."""


def test_admin_gets_the_bench_with_its_script(admin_client):
    resp = admin_client.get("/tarozi/test/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "data-tarozi" in html
    assert "js/tarozi.js" in html


def test_skladchi_cannot_open_the_bench(skladchi_client):
    assert skladchi_client.get("/tarozi/test/").status_code == 403


def test_translator_cannot_open_the_bench(translator_client):
    assert translator_client.get("/tarozi/test/").status_code == 403
