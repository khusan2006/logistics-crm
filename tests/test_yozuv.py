"""Yozuv — the kiril / lotin switcher.

The transliteration itself runs in the browser (static/js/yozuv.js), because a good
part of the Uzbek on screen is written by base.html's own JS or arrives in a modal
long after the response has been sent. What CAN be pinned from here is the wiring —
that every page carries the script, that the switcher is on the toolbar, that the
default is kiril, and that the app's own name is exempted — plus the one rule with
money behind it: what an operator typed is never transliterated on its way back.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.django_db

JS = "static/js/yozuv.js"
JS_PATH = Path(__file__).resolve().parent.parent / JS


def _page(client, url="/"):
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def test_every_page_loads_the_transliterator(admin_client):
    html = _page(admin_client)
    assert re.search(r'<script src="/static/js/yozuv\.js\?v=\d+"></script>', html)


def test_kiril_is_the_default_and_is_decided_before_the_first_paint(admin_client):
    """Served in lotin and converted in the browser, so the choice has to be made in
    the <head> — after paint the operator watches the page change alphabet."""
    html = _page(admin_client)
    head = html.split("</head>", 1)[0]
    assert "localStorage.getItem('yozuv')" in head
    # kiril unless it was explicitly turned off — including when localStorage throws
    assert "var yozuv = 'kiril';" in head
    assert "=== 'lotin'" in head
    assert "data-yozuv-wait" in head


def test_the_page_is_never_left_hidden(admin_client):
    """`data-yozuv-wait` hides the body until the conversion is done. It is removed
    in a `finally`, so a transliterator that throws costs the wrong alphabet rather
    than a blank screen."""
    html = _page(admin_client)
    tail = html.rsplit("Yozuv.start()", 1)[1]
    assert "finally" in tail
    assert "removeAttribute('data-yozuv-wait')" in tail


def test_the_switcher_sits_beside_the_theme_toggle(admin_client):
    html = _page(admin_client)
    assert 'id="yozuv-toggle"' in html
    assert html.index('id="yozuv-toggle"') < html.index('id="theme-toggle"')
    # its face names the script it switches TO, so both words ship and CSS picks one
    assert "Кир" in html and "Lot" in html


def test_the_apps_own_name_is_not_transliterated(admin_client):
    """"GranulaLog" is a name, not a word. The elements holding it are marked, and
    the <title> — which cannot be marked up — is covered by the KEEP list in the JS."""
    html = _page(admin_client)
    assert '<span class="brand-text" data-lotin>GranulaLog</span>' in html
    assert "granulalog: 1" in JS_PATH.read_text(encoding="utf-8")


def test_the_switcher_itself_is_left_in_lotin(admin_client):
    """The button offers "Lot"; transliterating that to "Лот" would name the thing it
    is not offering."""
    html = _page(admin_client)
    button = html.split('id="yozuv-toggle"', 1)[1].split("</button>", 1)[0]
    assert "data-lotin" in button


def test_what_the_operator_typed_is_never_transliterated(admin_client):
    """The rule with money behind it. An input's value is data on its way back to the
    server: converting one would post Cyrillic into the database on the next Saqlash,
    and the marka it names would stop matching every row already stored under it."""
    js = JS_PATH.read_text(encoding="utf-8")
    skip = js.split("var SKIP = {", 1)[1].split("}", 1)[0]
    assert "TEXTAREA" in skip and "SCRIPT" in skip
    attrs = js.split("var ATTRS = [", 1)[1].split("]", 1)[0]
    assert "value" not in attrs        # placeholder/title yes, value never
    assert "placeholder" in attrs and "title" in attrs


def test_the_search_box_still_searches_the_stored_lotin(admin_client):
    """Known limit, pinned so it is a decision rather than a surprise: the rows in the
    database are lotin, so a qidiruv typed in kiril finds nothing. The placeholder is
    converted for reading; the value the operator types is not touched, and goes to
    the server exactly as typed."""
    from crm.models import Partner
    Partner.objects.create(name="Vazifadon", phone="1", city="Tehron")
    assert "Vazifadon" in _page(admin_client, "/partners/?q=Vazifadon")
    assert "Vazifadon" not in _page(admin_client, "/partners/?q=Вазифадон")


# =====================================================================
#     the transliteration itself, run through node where there is one
# =====================================================================
#
# yozuv.js is browser code and the suite has no JS runner, so this drives it with
# whatever node is on the machine and skips where there is none rather than making
# one a requirement of running the tests. The alphabet is the whole feature: every
# label in the app goes through it, and the cases below are the ones that are easy
# to get wrong, not a sample.

CASES = [
    # the app's own furniture
    ("Yuklar", "Юклар"), ("Kelishuvlar", "Келишувлар"), ("Hamkorlar", "Ҳамкорлар"),
    ("Mijozlar", "Мижозлар"), ("Bojxona", "Божхона"), ("Qaytarish", "Қайтариш"),
    ("Saqlash", "Сақлаш"), ("Bekor qilish", "Бекор қилиш"),
    ("1 kg narxi (kelishuvdan)", "1 кг нархи (келишувдан)"),
    ("Jo'natilgan sana", "Жўнатилган сана"),
    ("Haydovchiga avans", "Ҳайдовчига аванс"),
    ("105 600 so'm", "105 600 сўм"),
    # x and h are two different letters and the usual way a mapping goes wrong
    ("Taxminiy", "Тахминий"), ("Mahsulot", "Маҳсулот"), ("Izoh", "Изоҳ"),
    # o' and g' are single letters; the apostrophe is not a sign of its own there
    ("To'lov", "Тўлов"), ("g'alla", "ғалла"), ("yig'ish", "йиғиш"),
    # ...but on its own it is the hard sign
    ("ma'lumot", "маълумот"), ("san'at", "санъат"),
    # o' inside "yo'" beats the yo digraph — "йўл", never "ёъл"
    ("yo'l", "йўл"), ("Yo'lda", "Йўлда"), ("yo'q", "йўқ"),
    # ...while a g' AFTER it does not
    ("yog'", "ёғ"),
    # e opens a word as э and is е everywhere after
    ("eshik", "эшик"), ("ber", "бер"), ("Element", "Элемент"),
    # ts is not ц: "qaytsa" is a real word and ц would break it
    ("qaytsa", "қайтса"),
    # codes, acronyms and foreign markalar are left where they are
    ("LLDPE 209AA", "LLDPE 209AA"), ("MSKU 123456 7", "MSKU 123456 7"),
    ("QR kod", "QR код"), ("2102 campaund", "2102 campaund"),
    ("Excel", "Excel"), ("GranulaLog", "GranulaLog"),
    # and a marka that IS Uzbek still converts
    ("2102 repak", "2102 репак"),
]


def _node_translit(samples):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed — the browser code cannot be driven here")
    script = (
        "global.window = {};"
        f"require({json.dumps(str(JS_PATH))});"
        "const t = global.window.Yozuv.toKiril;"
        f"const inputs = {json.dumps(samples)};"
        "console.log(JSON.stringify(inputs.map(t)));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_alphabet_is_uzbek_kiril():
    got = _node_translit([latin for latin, _ in CASES])
    assert got == [kiril for _, kiril in CASES]


def test_converting_twice_changes_nothing_the_second_time():
    """What the MutationObserver relies on. It re-runs over anything that enters the
    page and cannot remember what it has already seen, so kiril in has to be kiril
    out — otherwise every redraw would grind the text a little further."""
    once = _node_translit([latin for latin, _ in CASES])
    assert _node_translit(once) == once
