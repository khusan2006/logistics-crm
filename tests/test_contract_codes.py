"""Kelishuv kodi: har bir hamkor uchun alohida, 1 dan boshlanadigan `nom-raqam`."""
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from django.db import IntegrityError, transaction

from crm.models import (
    AuditLog, Contract, Partner, Shipment, ShipmentStatus, SupplierPayment, partner_code_slug,
)


def _partner(name):
    return Partner.objects.create(name=name, phone="1", city="Tehron")


def _contract(partner, brand="LLDPE 209AA", **kw):
    defaults = dict(partner=partner, brand=brand, kg=Decimal("50000"),
                    price=Decimal("0.96"), created="2026-07-01", deadline="2026-07-28")
    defaults.update(kw)
    return Contract.objects.create(**defaults)


def _listed(client, **params):
    resp = client.get("/contracts/", params)
    assert resp.status_code == 200
    return [c.code for c in resp.context["page"].object_list]


# --- slug ------------------------------------------------------------------

@pytest.mark.parametrize("name, slug", [
    ("sobir", "sobir"),
    ("Sobir", "sobir"),
    ("Ali Valiyev", "ali-valiyev"),
    ("G'ayrat", "gayrat"),
    ("  Azim  ", "azim"),
    ("Шариф", "шариф"),
    ("!!!", "hamkor"),          # tinish belgilaridan slug chiqmaydi → zaxira
])
def test_slug_derives_from_the_hamkor_name(name, slug):
    assert partner_code_slug(name) == slug


# --- numbering -------------------------------------------------------------

def test_first_kelishuv_of_a_hamkor_is_number_one(db):
    assert _contract(_partner("Sobir")).code == "sobir-1"


def test_numbers_run_on_within_one_hamkor(db):
    sobir = _partner("Sobir")
    assert [_contract(sobir).code for _ in range(3)] == ["sobir-1", "sobir-2", "sobir-3"]


def test_every_hamkor_starts_at_one(db):
    sobir, azim = _partner("Sobir"), _partner("Azim")
    assert _contract(sobir).code == "sobir-1"
    assert _contract(azim).code == "azim-1"
    assert _contract(sobir).code == "sobir-2"
    assert _contract(azim).code == "azim-2"


def test_deleting_a_kelishuv_never_frees_its_number(db):
    sobir = _partner("Sobir")
    _contract(sobir)
    second = _contract(sobir)
    second.delete()
    assert _contract(sobir).code == "sobir-3"


# --- frozen at creation ----------------------------------------------------

def test_renaming_a_hamkor_leaves_existing_codes_alone(db):
    sobir = _partner("Sobir")
    old = _contract(sobir)
    sobir.name = "Sobirjon"
    sobir.save()
    old.refresh_from_db()
    assert old.code == "sobir-1"


def test_after_a_rename_the_next_code_continues_the_hamkors_numbers(db):
    """Yangi slug bo'sh bo'lsa ham raqam 1 ga tushmaydi — hamkorning o'z tarixi
    hisobga olinadi, shunda bitta hamkorda ikkita `-1` paydo bo'lmaydi."""
    sobir = _partner("Sobir")
    _contract(sobir), _contract(sobir)
    sobir.name = "Sobirjon"
    sobir.save()
    assert _contract(sobir).code == "sobirjon-3"


def test_editing_a_hamkor_does_not_reset_their_numbering(db):
    """Hamkorning telefonini o'zgartirish raqamlashni nolga qaytarmasligi kerak —
    aks holda keyingi kelishuv allaqachon berilgan kodni olishga urinadi."""
    sobir = _partner("Sobir")
    _contract(sobir), _contract(sobir)
    sobir.phone = "+998900000000"          # eskirgan nusxa saqlanadi
    sobir.save()
    assert _contract(sobir).code == "sobir-3"


def test_editing_other_fields_keeps_the_code(db):
    c = _contract(_partner("Sobir"))
    c.brand = "HDPE 7000F"
    c.kg = Decimal("999")
    c.save()
    c.refresh_from_db()
    assert c.code == "sobir-1"


# --- moving between hamkorlar ---------------------------------------------

def test_moving_to_another_hamkor_reissues_the_code(db):
    sobir, javod = _partner("Sobir"), _partner("Javod")
    _contract(javod)                       # javod-1 band
    moved = _contract(sobir)               # sobir-1
    moved.partner = javod
    moved.save()
    moved.refresh_from_db()
    assert moved.code == "javod-2"


def test_a_retired_code_is_never_handed_out_again(db):
    sobir, javod = _partner("Sobir"), _partner("Javod")
    moved = _contract(sobir)               # sobir-1
    moved.partner = javod
    moved.save()
    assert _contract(sobir).code == "sobir-2"


# --- slug collisions -------------------------------------------------------

def test_two_hamkors_sharing_a_slug_do_not_share_numbers(db):
    """`G'ayrat` va `Gayrat` — ikkalasi ham `gayrat`. Kodlar baribir yagona."""
    a, b = _partner("G'ayrat"), _partner("Gayrat")
    codes = [_contract(a).code, _contract(a).code, _contract(b).code]
    assert codes == ["gayrat-1", "gayrat-2", "gayrat-3"]


def test_duplicate_codes_are_rejected_by_the_database(db):
    sobir = _partner("Sobir")
    first, second = _contract(sobir), _contract(sobir)
    second.code_number = first.code_number
    with pytest.raises(IntegrityError), transaction.atomic():
        second.save()


def test_save_retries_when_the_number_is_taken_behind_its_back(db, monkeypatch):
    """Ikki admin bir vaqtda saqlasa — yutqazgani xato ko'rsatmay, qayta uriniladi."""
    sobir = _partner("Sobir")
    taken = _contract(sobir)               # sobir-1

    real = Contract._next_code_number
    calls = []

    def stale_first(self, slug):
        calls.append(slug)
        return taken.code_number if len(calls) == 1 else real(self, slug)

    monkeypatch.setattr(Contract, "_next_code_number", stale_first)
    assert _contract(sobir).code == "sobir-2"
    assert len(calls) == 2                 # birinchi urinish to'qnashdi, ikkinchisi o'tdi


# --- display ---------------------------------------------------------------

def test_str_leads_with_the_code(db):
    c = _contract(_partner("Sobir"), brand="LLDPE 209AA")
    assert str(c) == "sobir-1 · LLDPE 209AA"


# --- search ----------------------------------------------------------------

def test_search_by_full_code(admin_client, db):
    sobir, azim = _partner("Sobir"), _partner("Azim")
    _contract(sobir), _contract(sobir), _contract(azim)
    assert _listed(admin_client, q="sobir-2") == ["sobir-2"]


def test_search_by_hamkor_slug(admin_client, db):
    sobir, azim = _partner("Sobir"), _partner("Azim")
    _contract(sobir), _contract(azim)
    assert _listed(admin_client, q="sobir") == ["sobir-1"]


def test_search_by_slug_finds_codes_left_behind_by_a_rename(admin_client, db):
    sobir = _partner("Sobir")
    _contract(sobir)
    sobir.name = "Sobirjon"
    sobir.save()
    assert _listed(admin_client, q="sobir") == ["sobir-1"]


def test_a_bare_number_searches_the_code_number(admin_client, db):
    sobir, azim = _partner("Sobir"), _partner("Azim")
    for p in (sobir, sobir, azim, azim):
        _contract(p, brand="Granula")      # raqamsiz marka — faqat kod bo'yicha topilsin
    assert sorted(_listed(admin_client, q="2")) == ["azim-2", "sobir-2"]


def test_a_number_still_finds_brands_containing_it(admin_client, db):
    """Marka nomlarida raqam bor (LLDPE 209AA) — kod qidiruvi ularni yo'qotmasin."""
    sobir = _partner("Sobir")
    _contract(sobir, brand="LLDPE 209AA")
    assert _listed(admin_client, q="209") == ["sobir-1"]


# --- audit trail and confirmations ----------------------------------------

def test_audit_note_names_the_code(admin_client, db):
    """Audit qatorining `target_id` si raqamligicha qoladi (u barcha modellar uchun
    umumiy), shuning uchun kod o'qiladigan izohga yoziladi."""
    sobir = _partner("Sobir")
    admin_client.post("/contracts/new/", {
        "partner": sobir.pk, "brand": "HDPE 7000F", "kg": "30000", "price": "1.05",
        "created": "2026-07-04", "deadline": "2026-08-05", "note": "",
    })
    assert "sobir-1" in AuditLog.objects.get(target_type="Kelishuv").summary


def test_delete_confirmation_shows_the_code(admin_client, db):
    c = _contract(_partner("Sobir"))
    html = admin_client.get(f"/contracts/{c.pk}/delete/").content.decode()
    assert "sobir-1" in html and f"#{c.pk}" not in html


# --- exports ---------------------------------------------------------------

def _first_data_row(resp):
    ws = openpyxl.load_workbook(BytesIO(resp.content)).active
    return [c.value for c in next(ws.iter_rows(min_row=2, max_row=2))]


@pytest.mark.parametrize("url, column", [
    ("/reports/export/contracts.xlsx", 0),
    ("/reports/export/supplier-payments.xlsx", 1),
    ("/reports/export/shipments.xlsx", 1),
])
def test_exports_carry_the_code_not_the_row_id(admin_client, db, url, column):
    """Mijoz Excel ni ham o'qiydi — u yerda `#12` chiqsa, kodning ma'nosi yo'qoladi."""
    contract = _contract(_partner("Sobir"))
    Shipment.objects.create(contract=contract, kg=Decimal("500"),
                            status=ShipmentStatus.objects.first(), sent="2026-07-05")
    SupplierPayment.objects.create(contract=contract, date="2026-07-11",
                                   amount=Decimal("200.00"), method="cash")

    assert _first_data_row(admin_client.get(url))[column] == "sobir-1"
