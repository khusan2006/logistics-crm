"""Tests for the `import_opening` management command (Excel opening-balance import).

The sheet format is OUR documented definition (see docs/import-format.md) — the
client's real files aren't available yet. Build against this format now; adapt
the command later if their files differ.
"""
from datetime import date
from io import BytesIO

import pytest
from django.core.management import call_command
from openpyxl import Workbook

from crm.models import Contract, Customer, Partner


def _build_workbook(partners=None, customers=None, contracts=None, opening_debts=None):
    """Build an in-memory xlsx with named sheets matching the documented format.
    Each arg is a list of row-tuples; pass None to omit that sheet entirely."""
    wb = Workbook()
    # openpyxl always creates a default "Sheet" — repurpose or remove it.
    default_sheet = wb.active
    used_default = False

    def add_sheet(name, header, rows):
        nonlocal used_default
        if not used_default:
            ws = default_sheet
            ws.title = name
            used_default = True
        else:
            ws = wb.create_sheet(name)
        ws.append(header)
        for row in rows:
            ws.append(row)

    if partners is not None:
        add_sheet("Partners", ["name", "phone", "city", "note"], partners)
    if customers is not None:
        add_sheet("Customers", ["name", "phone", "address", "note"], customers)
    if contracts is not None:
        add_sheet(
            "Contracts",
            ["partner", "brand", "kg", "price", "created", "deadline", "note"],
            contracts,
        )
    if opening_debts is not None:
        add_sheet("CustomerOpeningDebt", ["customer", "amount"], opening_debts)

    if not used_default:
        # no sheets requested — keep the default empty sheet so the file is valid
        pass

    return wb


def _save(wb, tmp_path, name="opening.xlsx"):
    path = tmp_path / name
    buffer = BytesIO()
    wb.save(buffer)
    path.write_bytes(buffer.getvalue())
    return path


@pytest.mark.django_db
def test_imports_partners_customers_contracts(tmp_path):
    wb = _build_workbook(
        partners=[
            ("Pars Polymer", "+98 912 440 1122", "Tehron", "yaxshi hamkor"),
            ("Arya Petrochem", "+98 21 555 0000", "Shiroz", ""),
        ],
        customers=[
            ("Alisher Trading", "+998 90 123 45 67", "Toshkent, Chilonzor", ""),
        ],
        contracts=[
            ("Pars Polymer", "PP-R", 5000, 1.25, date(2026, 1, 10), date(2026, 3, 1), "birinchi partiya"),
        ],
    )
    path = _save(wb, tmp_path)

    call_command("import_opening", str(path))

    assert Partner.objects.filter(name="Pars Polymer", city="Tehron").exists()
    assert Partner.objects.filter(name="Arya Petrochem").exists()
    assert Customer.objects.filter(name="Alisher Trading", address="Toshkent, Chilonzor").exists()

    contract = Contract.objects.get(lines__brand="PP-R")
    assert contract.partner.name == "Pars Polymer"
    line = contract.lines.get()
    assert line.kg == 5000
    assert line.price == 1.25
    assert contract.created == date(2026, 1, 10)
    assert contract.note == "birinchi partiya"


@pytest.mark.django_db
def test_import_is_idempotent(tmp_path):
    wb = _build_workbook(
        partners=[("Pars Polymer", "+98 912 440 1122", "Tehron", "")],
        customers=[("Alisher Trading", "+998 90 123 45 67", "Toshkent", "")],
        contracts=[
            ("Pars Polymer", "PP-R", 5000, 1.25, date(2026, 1, 10), date(2026, 3, 1), ""),
        ],
    )
    path = _save(wb, tmp_path)

    call_command("import_opening", str(path))
    call_command("import_opening", str(path))

    assert Partner.objects.count() == 1
    assert Customer.objects.count() == 1
    assert Contract.objects.count() == 1


@pytest.mark.django_db
def test_row_with_missing_required_field_is_skipped_not_crashing(tmp_path):
    wb = _build_workbook(
        partners=[
            ("", "+98 912 440 1122", "Tehron", "blank name — should be skipped"),
            ("Arya Petrochem", "+98 21 555 0000", "Shiroz", ""),
        ],
    )
    path = _save(wb, tmp_path)

    call_command("import_opening", str(path))

    assert Partner.objects.count() == 1
    assert Partner.objects.filter(name="Arya Petrochem").exists()


@pytest.mark.django_db
def test_missing_sheets_are_skipped_gracefully(tmp_path):
    wb = _build_workbook(customers=[("Solo Customer", "", "", "")])
    path = _save(wb, tmp_path)

    call_command("import_opening", str(path))

    assert Customer.objects.count() == 1
    assert Partner.objects.count() == 0
    assert Contract.objects.count() == 0


@pytest.mark.django_db
def test_opening_debt_sheet_is_reported_not_imported_as_transaction(tmp_path, capsys):
    wb = _build_workbook(
        customers=[("Alisher Trading", "", "", "")],
        opening_debts=[("Alisher Trading", 340.50)],
    )
    path = _save(wb, tmp_path)

    call_command("import_opening", str(path))

    captured = capsys.readouterr()
    assert "Alisher Trading" in captured.out
    assert "340.5" in captured.out or "340.50" in captured.out
