"""Import a prototype JSON export into the database (wipe-then-load).

The JS/localStorage prototype went through several generations, and a client's
browser may hold any of them. This command accepts all of them:

  gen A (`gl_*` keys)   : gl_partners / gl_agreements / gl_payments / gl_shipments
  gen B (`agreements`)  : agreements[].grade, payments[].type, shipments[].sentDate
                          with numeric transport/customs/other expenses
  gen C (`granulalog_demo`): contracts[].brand, payments[].method,
                          shipments[].sent + *Expense buckets + logist/container

Loaded as:
  partners  -> Partner            (joined by the export's `id`)
  contracts -> Contract           (joined by `id`; partnerId -> partner)
  payments  -> SupplierPayment    (contractId/agreementId -> contract)
  shipments -> Shipment           (+ one ShipmentExpense per non-zero cost)

Brands are NOT unique in real exports, so contracts are keyed by their own `id`,
never by brand.

DESTRUCTIVE. Prompts for confirmation unless --noinput. One atomic transaction,
so a failure leaves the DB untouched. Any section it cannot import is reported
rather than dropped silently.

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

# One logical section -> the key names used by the prototype generations.
SECTION_ALIASES = {
    "partners": ("partners", "gl_partners"),
    "contracts": ("contracts", "agreements", "gl_agreements"),
    "payments": ("payments", "gl_payments"),
    "shipments": ("shipments", "gl_shipments"),
}

# Sections the prototype exports that have no importer yet. Empty in every
# export seen so far; a non-empty one is reported instead of dropped.
KNOWN_UNIMPORTED = ("audit", "settings", "sales", "cashEntries", "debtPayments")

METHOD_MAP = {
    "Naqd": "cash",
    "Karta": "card",
    "Bank o'tkazmasi": "transfer",
}

# Status wording drifted between generations; map the strays onto real rows.
STATUS_ALIASES = {
    "bojxonada": "Bojxona",
    "yo'lda": "Yo'lda",
    "omborda": "Omborga yetib keldi",
}


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


def _first(row, *keys, default=None):
    """First present, non-empty value among alias keys."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _method(raw):
    key = _norm(raw)
    if key not in METHOD_MAP:
        raise CommandError(f"Noma'lum to'lov usuli: {raw!r}")
    return METHOD_MAP[key]


def _money(value):
    """Numeric expense value -> Decimal, or None when absent/zero."""
    if value in (None, "", 0):
        return None
    return Decimal(str(value))


def _size(value):
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
            counts, used_keys = self._load(data, owner)

        self.stdout.write(self.style.SUCCESS(
            "Import tayyor: {partners} hamkor, {contracts} kelishuv, "
            "{payments} to'lov, {shipments} yuk, {expenses} xarajat.".format(**counts)
        ))
        self._report_skipped(data, used_keys)
        self.stdout.write(self.style.WARNING(
            f"Egasi: {OWNER_USERNAME} / {OWNER_PASSWORD} — prodda parolni o'zgartiring."
        ))

    def _section(self, data, name, used_keys):
        """Rows for a logical section, whichever generation's key holds them."""
        for key in SECTION_ALIASES[name]:
            if key in data:
                used_keys.add(key)
                return data[key] or []
        return []

    def _report_skipped(self, data, used_keys):
        """Warn about any non-empty section the importer did not load."""
        skipped = []
        for key, value in data.items():
            if key in used_keys:
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

    def _resolve_status(self, raw, status_by_name):
        key = _norm(raw)
        status = status_by_name.get(key)
        if status is None:
            status = status_by_name.get(_norm(STATUS_ALIASES.get(key.lower(), "")))
        if status is None:
            raise CommandError(
                f"ShipmentStatus {raw!r} topilmadi. Mavjud holatlar: "
                + ", ".join(sorted(status_by_name)) + "."
            )
        return status

    def _load(self, data, owner):
        used_keys = set()

        partners = {}
        for row in self._section(data, "partners", used_keys):
            partners[row["id"]] = Partner.objects.create(
                name=row.get("name", ""), phone=row.get("phone", ""),
                city=row.get("city", ""), note=row.get("note", ""),
            )

        contracts = {}
        for row in self._section(data, "contracts", used_keys):
            partner = partners.get(row["partnerId"])
            if partner is None:
                raise CommandError(
                    f"Kelishuv #{row.get('id')}: hamkor topilmadi "
                    f"(partnerId={row.get('partnerId')})."
                )
            contracts[row["id"]] = Contract.objects.create(
                partner=partner,
                brand=_first(row, "brand", "grade", default=""),
                kg=Decimal(str(row["kg"])), price=Decimal(str(row["price"])),
                created=_d(_first(row, "created", "date")),
                deadline=_d(row.get("deadline")),
                note=row.get("note", ""),
                created_by=owner,
            )

        payments = 0
        for row in self._section(data, "payments", used_keys):
            link = _first(row, "contractId", "agreementId")
            contract = contracts.get(link)
            if contract is None:
                raise CommandError(f"To'lov: kelishuv topilmadi (id={link}).")
            SupplierPayment.objects.create(
                contract=contract, amount=Decimal(str(row["amount"])),
                date=_d(row.get("date")),
                method=_method(_first(row, "method", "type")),
                note=row.get("note", ""), created_by=owner,
            )
            payments += 1

        status_by_name = {_norm(s.name): s for s in ShipmentStatus.objects.all()}
        shipments = expenses = 0
        for row in self._section(data, "shipments", used_keys):
            link = _first(row, "contractId", "agreementId")
            contract = contracts.get(link)
            if contract is None:
                raise CommandError(f"Yuk: kelishuv topilmadi (id={link}).")

            # `transport` means the vehicle plate in gen C but a transport COST in
            # gen B — a string is a plate, a number is money.
            raw_transport = row.get("transport")
            plate = raw_transport if isinstance(raw_transport, str) else ""
            transport_cost = raw_transport if isinstance(raw_transport, (int, float)) else None

            logist = (row.get("logist") or "").strip()
            note_parts = [p for p in (f"Logist: {logist}" if logist else "",
                                      (row.get("note") or "").strip()) if p]

            shipment = Shipment.objects.create(
                contract=contract, kg=Decimal(str(row["kg"])),
                status=self._resolve_status(row.get("status"), status_by_name),
                sent=_d(_first(row, "sent", "sentDate", "date")),
                eta=_d(row.get("eta")),
                arrived=_d(_first(row, "arrived", "arrival")),
                transport=plate, container=row.get("container", ""),
                note=" · ".join(note_parts), created_by=owner,
            )
            shipments += 1

            exp_date = shipment.arrived or shipment.sent or timezone.localdate()
            exp_note = (row.get("expenseNote") or "").strip()
            buckets = (
                ("transport", _money(_first(row, "transportExpense") or transport_cost), ""),
                ("customs", _money(_first(row, "customsExpense", "customs")), ""),
                ("other", _money(_first(row, "handlingExpense")), "Yuk ortish-tushirish"),
                ("other", _money(_first(row, "otherExpense", "other")), ""),
            )
            for category, amount, fallback in buckets:
                if amount is None:
                    continue
                ShipmentExpense.objects.create(
                    shipment=shipment, category=category, amount=amount,
                    date=exp_date, note=exp_note or fallback, created_by=owner,
                )
                expenses += 1

        return {
            "partners": len(partners), "contracts": len(contracts),
            "payments": payments, "shipments": shipments, "expenses": expenses,
        }, used_keys
