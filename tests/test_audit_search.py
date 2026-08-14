"""Audit jurnalidagi universal qidiruv.

One box across every column the table draws. A jurnal is read by remembering a
fragment — "sotuv", the hamkor's name, a figure — not by knowing which column it fell
in, so there is nothing to choose first.

The awkward one is `action`: it is stored as a code ("payment") while the screen shows
the Uzbek label ("To'lov"). Searching for the words in front of you has to work, so
the labels are matched and turned back into codes.
"""
from io import BytesIO

import openpyxl
from crm.models import AuditLog


def _log(action, target_type, target_id, summary, user=None):
    return AuditLog.record(user, action, target_type, target_id, summary)


def _found(client, q, **params):
    resp = client.get("/audit/", {"q": q, **params})
    assert resp.status_code == 200
    return [row.summary for row in resp.context["page"].object_list]


def test_search_matches_the_tafsilot(admin_client, db):
    _log(AuditLog.Action.CREATE, "Sotuv", 1, "Sotuv qo'shildi · Alisher · 1200$")
    _log(AuditLog.Action.CREATE, "Yuk", 2, "Yuk qo'shildi · Pars")

    assert _found(admin_client, "Alisher") == ["Sotuv qo'shildi · Alisher · 1200$"]


def test_search_matches_the_obyekt(admin_client, db):
    _log(AuditLog.Action.CREATE, "Kelishuv", 1, "birinchi")
    _log(AuditLog.Action.CREATE, "Yuk", 2, "ikkinchi")

    assert _found(admin_client, "kelishuv") == ["birinchi"]


def test_search_matches_the_amal_by_the_label_on_screen(admin_client, db):
    """The column shows "To'lov"; the row holds "payment". Typing what is written in
    front of you is the only search anybody will try."""
    _log(AuditLog.Action.PAYMENT, "Mijoz to'lovi", 1, "500$ qabul qilindi")
    _log(AuditLog.Action.STATUS, "Yuk", 2, "Holat: yo'lda")

    assert _found(admin_client, "to'lov") == ["500$ qabul qilindi"]
    assert _found(admin_client, "Holat o'zgardi") == ["Holat: yo'lda"]


def test_search_matches_the_person_who_did_it(admin_client, admin_user, db):
    _log(AuditLog.Action.CREATE, "Sotuv", 1, "admin kiritgan", user=admin_user)
    _log(AuditLog.Action.CREATE, "Sotuv", 2, "tizim kiritgan")

    assert _found(admin_client, admin_user.username) == ["admin kiritgan"]


def test_a_number_finds_both_the_id_and_a_figure_in_the_text(admin_client, db):
    """"#38" and "38 546 940" are both things people search for, and a bare 38 is how
    they type either."""
    _log(AuditLog.Action.CREATE, "Yuk", 38, "yuk qo'shildi")
    _log(AuditLog.Action.PAYMENT, "Bojxona", 9, "38 546 940 so'm yuborildi")
    _log(AuditLog.Action.CREATE, "Sotuv", 7, "boshqa yozuv")

    assert sorted(_found(admin_client, "38")) == ["38 546 940 so'm yuborildi", "yuk qo'shildi"]
    # The hash is how the table itself prints an id, so it is accepted and ignored.
    assert _found(admin_client, "#38") == ["yuk qo'shildi"]


def test_the_search_and_the_davr_narrow_together(admin_client, db):
    """Searching must not widen the window back out, and vice versa."""
    _log(AuditLog.Action.CREATE, "Sotuv", 1, "iyuldagi sotuv")
    _log(AuditLog.Action.CREATE, "Sotuv", 2, "avgustdagi sotuv")
    july, august = AuditLog.objects.order_by("pk")
    AuditLog.objects.filter(pk=july.pk).update(created_at="2026-07-15 10:00:00+05:00")
    AuditLog.objects.filter(pk=august.pk).update(created_at="2026-08-15 10:00:00+05:00")

    assert _found(admin_client, "sotuv", **{"from": "2026-07-01", "to": "2026-07-31"}) \
        == ["iyuldagi sotuv"]


def test_nothing_found_says_what_was_looked_for(admin_client, db):
    _log(AuditLog.Action.CREATE, "Sotuv", 1, "bor")
    html = admin_client.get("/audit/", {"q": "yoq-narsa"}).content.decode()
    assert "«yoq-narsa» bo&#x27;yicha yozuv topilmadi" in html or "«yoq-narsa» bo'yicha yozuv topilmadi" in html


def test_the_excel_button_downloads_what_the_search_left(admin_client, db):
    _log(AuditLog.Action.CREATE, "Sotuv", 1, "sotuv qo'shildi")
    _log(AuditLog.Action.CREATE, "Yuk", 2, "yuk qo'shildi")

    resp = admin_client.get("/audit/export.xlsx", {"q": "sotuv"})
    ws = openpyxl.load_workbook(BytesIO(resp.content)).active
    assert [row[5] for row in ws.iter_rows(min_row=2, values_only=True)] == ["sotuv qo'shildi"]


def test_the_search_box_keeps_the_davr_and_the_term(admin_client, db):
    html = admin_client.get("/audit/", {
        "q": "sotuv", "from": "2026-07-01", "to": "2026-07-31"}).content.decode()
    row = html.split('class="searchbar"')[1].split("</form>")[0]
    assert 'name="from" value="2026-07-01"' in row
    assert 'name="to" value="2026-07-31"' in row
    assert 'value="sotuv"' in row


def test_the_jurnal_is_still_read_only(admin_client, db):
    """Adding a search must not add a way to write: the page stays a table plus a GET
    form, and the only form on it is that search."""
    body = admin_client.get("/audit/").content.decode().split("<main")[-1]
    assert 'method="post"' not in body.lower()
    assert body.lower().count("<form") == 1
