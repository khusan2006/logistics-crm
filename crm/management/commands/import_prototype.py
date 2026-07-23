"""Import a prototype JSON export into the database (wipe-then-load).

The old JS/localStorage prototype exports a JSON blob with `partners`,
`contracts`, `payments`, and `shipments`. This command wipes existing business
data, creates the 'Otabek Yo'ldoshev' owner, and loads that export faithfully:

  * partners  -> Partner            (joined by the export's numeric `id`)
  * contracts -> Contract           (joined by `id`; `partnerId` -> partner)
  * payments  -> SupplierPayment    (`contractId` -> contract; method mapped)
  * shipments -> Shipment           (`contractId` -> contract; status by name)
                 + one ShipmentExpense per non-zero transport/customs/handling/
                 other expense bucket.

Brands are NOT unique in the export (the same brand recurs across contracts),
so contracts are keyed by their own `id`, never by brand.

DESTRUCTIVE. Prompts for confirmation unless --noinput is given. Everything runs
in one atomic transaction, so a failure leaves the DB untouched. Re-running
resets the DB to the file's contents.

Usage:
    python manage.py import_prototype                      # default committed file
    python manage.py import_prototype --file path.json --noinput
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.models import (
    Contract,
    Partner,
    Shipment,
    ShipmentExpense,
    ShipmentStatus,
    SupplierPayment,
)
from crm.seeding import OWNER_PASSWORD, OWNER_USERNAME, ensure_owner, wipe_business_data

DEFAULT_FILE = Path(settings.BASE_DIR) / "crm" / "seed_data" / "prototype.json"

# Prototype payment-method label -> PayMethod value. Curly apostrophes in the
# export are normalised to straight ones before lookup.
METHOD_MAP = {
    "Naqd": "cash",
    "Karta": "card",
    "Bank o'tkazmasi": "transfer",
}

# Prototype shipment expense key -> (ShipmentExpense category, fallback note).
# The model has no "handling" category, so handling folds into "other" but keeps
# a note so the meaning survives.
EXPENSE_FIELDS = [
    ("transportExpense", "transport", ""),
    ("customsExpense", "customs", ""),
    ("handlingExpense", "other", "Yuk ortish-tushirish"),
    ("otherExpense", "other", ""),
]

# Sections this command knows how to load.
IMPORTED_SECTIONS = ("partners", "contracts", "payments", "shipments")

# Sections the prototype exports that have no importer yet. They are empty in
# every export seen so far; if a real client export ever fills one, the run
# reports it loudly rather than dropping the rows in silence.
KNOWN_UNIMPORTED = ("audit", "settings", "sales", "cashEntries", "debtPayments")


def _d(value):
    """ISO date string -> date, or None for empty/missing values."""
    return date.fromisoformat(value) if value else None


def _norm(value):
    """Normalise curly apostrophes to straight ones.

    The export writes Uzbek text with U+2018/U+2019 ("Yo‘lda", "Bank o‘tkazmasi")
    while the DB and choice labels use a straight quote, so any lookup keyed on
    that text must compare normalised forms or it silently misses.
    """
    return (value or "").replace("‘", "'").replace("’", "'").strip()


def _method(raw):
    key = _norm(raw)
    if key not in METHOD_MAP:
        raise CommandError(f"Noma'lum to'lov usuli: {raw!r}")
    return METHOD_MAP[key]


def _size(value):
    """Row count for a section, for the 'not imported' report."""
    if isinstance(value, (list, dict)):
        return len(value)
    return 0 if value in (None, "") else 1


class Command(BaseCommand):
    help = "Wipe business data and import a prototype JSON export."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", default=str(DEFAULT_FILE),
            help="Path to the prototype JSON export (defaults to the committed file).",
        )
        parser.add_argument(
            "--noinput", "--no-input", action="store_true", dest="noinput",
            help="Skip the destructive-action confirmation prompt.",
        )
        parser.add_argument(
            "--if-empty", action="store_true", dest="if_empty",
            help="Do nothing if business data already exists. Use at deploy time "
                 "so the import seeds the DB exactly once and redeploys are no-ops.",
        )

    def handle(self, *args, **options):
        # Deploy-time one-time guard: once anything is loaded, never touch it again
        # (so redeploys can't wipe real prod data). Checked before the file read so
        # a missing file never breaks an already-seeded redeploy.
        if options["if_empty"] and Partner.objects.exists():
            self.stdout.write(
                "Ma'lumotlar allaqachon mavjud — import o'tkazib yuborildi (--if-empty)."
            )
            return

        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"Fayl topilmadi: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))

        if not options["noinput"]:
            self.stdout.write(self.style.WARNING(
                "DIQQAT: bu buyruq BARCHA biznes ma'lumotlarini o'chiradi va "
                f"{path.name} faylidagi ma'lumotlar bilan almashtiradi."
            ))
            if input("Davom etish uchun 'yes' deb yozing: ").strip().lower() != "yes":
                raise CommandError("Bekor qilindi.")

        with transaction.atomic():
            wipe_business_data()
            owner = ensure_owner()
            counts = self._load(data, owner)

        self.stdout.write(self.style.SUCCESS(
            "Import tayyor: {partners} hamkor, {contracts} kelishuv, "
            "{payments} to'lov, {shipments} yuk, {expenses} xarajat.".format(**counts)
        ))
        self._report_skipped(data)
        self.stdout.write(self.style.WARNING(
            f"Egasi: {OWNER_USERNAME} / {OWNER_PASSWORD} — prodda parolni o'zgartiring."
        ))

    def _report_skipped(self, data):
        """Warn about any non-empty section the importer did not load."""
        skipped = []
        for key, value in data.items():
            if key in IMPORTED_SECTIONS:
                continue
            count = _size(value)
            if not count:
                continue
            suffix = "" if key in KNOWN_UNIMPORTED else " — NOMA'LUM bo'lim"
            skipped.append(f"{key} ({count}){suffix}")
        if not skipped:
            return
        self.stdout.write(self.style.WARNING(
            "DIQQAT — quyidagi bo'limlar import QILINMADI: " + ", ".join(skipped) + ". "
            "Bu ma'lumotlar bazaga tushmadi."
        ))

    def _load(self, data, owner):
        partners = {}
        for row in data.get("partners", []):
            partners[row["id"]] = Partner.objects.create(
                name=row.get("name", ""), phone=row.get("phone", ""),
                city=row.get("city", ""), note=row.get("note", ""),
            )

        contracts = {}
        for row in data.get("contracts", []):
            partner = partners.get(row["partnerId"])
            if partner is None:
                raise CommandError(
                    f"Kelishuv #{row.get('id')}: hamkor topilmadi "
                    f"(partnerId={row.get('partnerId')})."
                )
            contracts[row["id"]] = Contract.objects.create(
                partner=partner, brand=row.get("brand", ""),
                kg=Decimal(str(row["kg"])), price=Decimal(str(row["price"])),
                created=_d(row.get("created")), deadline=_d(row.get("deadline")),
                created_by=owner,
            )

        payments = 0
        for row in data.get("payments", []):
            contract = contracts.get(row["contractId"])
            if contract is None:
                raise CommandError(
                    f"To'lov: kelishuv topilmadi (contractId={row.get('contractId')})."
                )
            SupplierPayment.objects.create(
                contract=contract, amount=Decimal(str(row["amount"])),
                date=_d(row.get("date")), method=_method(row.get("method")),
                created_by=owner,
            )
            payments += 1

        status_by_name = {_norm(s.name): s for s in ShipmentStatus.objects.all()}
        shipments = expenses = 0
        for row in data.get("shipments", []):
            contract = contracts.get(row["contractId"])
            if contract is None:
                raise CommandError(
                    f"Yuk: kelishuv topilmadi (contractId={row.get('contractId')})."
                )
            status = status_by_name.get(_norm(row.get("status")))
            if status is None:
                raise CommandError(
                    f"ShipmentStatus '{row.get('status')}' topilmadi — "
                    "migratsiyalar qo'llanganmi?"
                )
            logist = (row.get("logist") or "").strip()
            shipment = Shipment.objects.create(
                contract=contract, kg=Decimal(str(row["kg"])), status=status,
                sent=_d(row.get("sent")), eta=_d(row.get("eta")),
                arrived=_d(row.get("arrived")),
                transport=row.get("transport", ""), container=row.get("container", ""),
                note=f"Logist: {logist}" if logist else "", created_by=owner,
            )
            shipments += 1

            exp_date = shipment.arrived or shipment.sent or timezone.localdate()
            exp_note = (row.get("expenseNote") or "").strip()
            for src_key, category, fallback in EXPENSE_FIELDS:
                amount = row.get(src_key) or 0
                if not amount:
                    continue
                ShipmentExpense.objects.create(
                    shipment=shipment, category=category, amount=Decimal(str(amount)),
                    date=exp_date, note=exp_note or fallback, created_by=owner,
                )
                expenses += 1

        return {
            "partners": len(partners), "contracts": len(contracts),
            "payments": payments, "shipments": shipments, "expenses": expenses,
        }
