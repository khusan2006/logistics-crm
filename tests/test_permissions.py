import pytest

ADMIN_ONLY_URLS = [
    "/partners/", "/partners/new/", "/contracts/", "/contracts/new/",
    "/supplier-payments/", "/supplier-payments/new/", "/statuses/",
    "/expenses/new/", "/audit/",
]


@pytest.mark.parametrize("url", ADMIN_ONLY_URLS)
def test_translator_gets_403(translator_client, url):
    assert translator_client.get(url).status_code == 403


@pytest.mark.parametrize("url", ["/shipments/"])
def test_translator_allowed(translator_client, url):
    assert translator_client.get(url).status_code == 200


def test_anonymous_redirected(client, db):
    assert client.get("/shipments/").status_code == 302
