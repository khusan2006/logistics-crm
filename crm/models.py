from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import DecimalField, Max, Sum
from django.utils import timezone
from django.utils.text import slugify

MONEY = DecimalField(max_digits=14, decimal_places=2)   # USD
QTY = DecimalField(max_digits=12, decimal_places=3)     # kg


def partner_code_slug(name):
    """The name half of a kelishuv code: "Ali Valiyev" → ali-valiyev, "G'ayrat" → gayrat.
    Cyrillic survives; a name that slugifies to nothing falls back so no code is a
    bare "-3"."""
    return slugify(name, allow_unicode=True) or "hamkor"


class PayMethod(models.TextChoices):
    CASH = "cash", "Naqd"
    CARD = "card", "Karta"
    TRANSFER = "transfer", "Bank o'tkazmasi"


class Currency(models.TextChoices):
    USD = "usd", "Dollar"
    UZS = "uzs", "So'm"


class AuditLog(models.Model):
    """Append-only trail of money- and status-relevant actions (client-crm pattern)."""

    class Action(models.TextChoices):
        CREATE = "create", "Qo'shildi"
        UPDATE = "update", "O'zgartirildi"
        DELETE = "delete", "O'chirildi"
        STATUS = "status", "Holat o'zgardi"
        PAYMENT = "payment", "To'lov"
        RETURN = "return", "Qaytarish"

    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="audit_logs", verbose_name="Kim",
    )
    action = models.CharField("Amal", max_length=10, choices=Action.choices)
    target_type = models.CharField("Obyekt", max_length=40)
    target_id = models.IntegerField("ID", null=True, blank=True)
    summary = models.CharField("Tafsilot", max_length=255)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit yozuvi"
        verbose_name_plural = "Audit jurnali"

    @classmethod
    def record(cls, user, action, target_type, target_id, summary):
        return cls.objects.create(user=user, action=action, target_type=target_type,
                                  target_id=target_id, summary=summary)

    def __str__(self):
        return f"{self.get_action_display()} · {self.target_type} · {self.summary}"


class Partner(models.Model):
    """Yetkazib beruvchi (supplier) in Iran or elsewhere."""

    name = models.CharField("Nomi", max_length=200)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    city = models.CharField("Shahar", max_length=100, blank=True)
    note = models.TextField("Izoh", blank=True)
    # Kelishuv codes are frozen once issued, so the high-water mark has to outlive the
    # rows themselves — deleting sobir-3 must not hand 3 out again. The counter only
    # ever climbs; the slug tracks the current name so a rename picks up the sequence.
    code_slug = models.CharField(max_length=120, db_index=True, editable=False)
    code_counter = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Hamkor"
        verbose_name_plural = "Hamkorlar"

    def save(self, *args, **kwargs):
        self.code_slug = partner_code_slug(self.name)
        if (fields := kwargs.get("update_fields")) is not None:
            kwargs["update_fields"] = {*fields, "code_slug"}
        elif self.pk:
            # Contract.save() bumps code_counter with a targeted UPDATE, so an
            # instance loaded before that bump still holds the old value. Writing it
            # back would reset the hamkor's numbering and mint a duplicate code.
            stored = Partner.objects.filter(pk=self.pk).values_list(
                "code_counter", flat=True).first()
            if stored is not None:
                self.code_counter = max(self.code_counter, stored)
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Customer(models.Model):
    """Mijoz (buyer) — purchases granula from us."""

    name = models.CharField("Ismi", max_length=200)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    address = models.CharField("Manzil", max_length=300, blank=True)
    note = models.TextField("Izoh", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Mijoz"
        verbose_name_plural = "Mijozlar"

    @property
    def sales_total(self):
        if not hasattr(self, "sales"):  # relation lands in a later Phase-2 task
            return Decimal("0")
        return sum((s.net_total for s in self.sales.all()), Decimal("0"))

    @property
    def paid_total(self):
        if not hasattr(self, "customer_payments"):  # relation lands in a later Phase-2 task
            return Decimal("0")
        return self.customer_payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def balance(self):
        """Positive = customer owes us (qarz); negative = advance (avans)."""
        return self.sales_total - self.paid_total

    def __str__(self):
        return self.name


class Contract(models.Model):
    """Kelishuv: an agreement with one partner covering one or more products.
    Each product — brand, kg, USD/kg — is a ContractLine; this model is the header
    (who, when, by when) and the sum of its lines."""

    partner = models.ForeignKey(Partner, on_delete=models.PROTECT,
                                related_name="contracts", verbose_name="Hamkor")
    # The code the client reads — sobir-3 — split so "next number for sobir" is a
    # Max() instead of parsing integers back out of strings (sobir-10 < sobir-9).
    code_slug = models.CharField(max_length=120, db_index=True, editable=False)
    code_number = models.PositiveIntegerField(editable=False)
    created = models.DateField("Kelishuv sanasi", default=timezone.localdate)
    # How many trucks the kelishuv is expected to take. Optional: it is often not
    # known when the agreement is signed, and old kelishuvlar never had it.
    planned_trucks = models.PositiveIntegerField(
        "Nechta mashina", null=True, blank=True,
        help_text="Kelishuv bo'yicha rejalashtirilgan mashinalar soni")
    note = models.TextField("Izoh", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="contracts",
                                   verbose_name="Kim ochdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]
        verbose_name = "Kelishuv"
        verbose_name_plural = "Kelishuvlar"
        constraints = [models.UniqueConstraint(fields=["code_slug", "code_number"],
                                               name="unique_contract_code")]

    @property
    def code(self):
        return f"{self.code_slug}-{self.code_number}"

    def _next_code_number(self, slug):
        """One past the highest number this hamkor — or anyone sharing their slug —
        has ever been issued. Counters live on Partner and never go down, so a
        deleted or moved-away kelishuv leaves a permanent gap instead of recycling.

        The slug half matters when two hamkorlar have different names that collapse
        to the same slug ("G'ayrat" and "Gayrat"): they share one number line, so
        neither can mint a code the other already used."""
        top = Partner.objects.filter(
            models.Q(code_slug=slug) | models.Q(pk=self.partner_id)
        ).aggregate(top=Max("code_counter"))["top"]
        return (top or 0) + 1

    def save(self, *args, **kwargs):
        # A code is stamped once and then frozen. It is re-issued only when the
        # kelishuv is deliberately moved to another hamkor — the old code retires.
        if self.pk:
            was = Contract.objects.filter(pk=self.pk).values_list("partner_id", flat=True).first()
            needs_code = was is not None and was != self.partner_id
        else:
            needs_code = True
        if not needs_code:
            return super().save(*args, **kwargs)

        # Two admins saving at once compute the same number; the unique constraint
        # rejects the loser, so recompute and retry rather than surfacing an error.
        for attempt in range(5):
            slug = partner_code_slug(self.partner.name)
            self.code_slug, self.code_number = slug, self._next_code_number(slug)
            try:
                with transaction.atomic():
                    result = super().save(*args, **kwargs)
                    Partner.objects.filter(pk=self.partner_id,
                                           code_counter__lt=self.code_number
                                           ).update(code_counter=self.code_number)
                    return result
            except IntegrityError:
                if attempt == 4:
                    raise
                # A retried INSERT must stay an INSERT: kwargs may carry the caller's
                # force_insert, and a failed insert leaves the pk unset either way.
                kwargs.pop("force_insert", None)

    # Every total below is the sum of the kelishuv's product lines. They are summed
    # in Python, not via aggregate(), so a prefetched list costs no query — the
    # kelishuvlar filters walk these over every row.
    @property
    def kg(self):
        return sum((ln.kg for ln in self.lines.all()), Decimal("0"))

    @property
    def total_value(self):
        return sum((ln.total_value for ln in self.lines.all()), Decimal("0"))

    @property
    def shipped_kg(self):
        return sum((ln.shipped_kg for ln in self.lines.all()), Decimal("0"))

    @property
    def remaining_kg(self):
        return self.kg - self.shipped_kg

    @property
    def brand_summary(self):
        """Every product, named in full — "2102 repak, ftor oq". Abbreviating to
        "2102 repak +1" hid exactly what the operator needs when picking a
        kelishuv from a dropdown."""
        return ", ".join(ln.brand for ln in self.lines.all())

    @property
    def paid_total(self):
        if not hasattr(self, "supplier_payments"):  # relation lands in Task 5
            return Decimal("0")
        return sum((p.amount for p in self.supplier_payments.all()), Decimal("0"))

    @property
    def shipped_value(self):
        """USD value of the goods actually sent (each truck line at its own unit
        price). The payable to the partner accrues per shipped truck, not on
        signing."""
        return sum((ln.shipped_value for ln in self.lines.all()), Decimal("0"))

    @property
    def debt(self):
        """What we owe the partner NOW: shipped value minus payments. Payments are
        capped at this in the form, so it never goes negative (no prepayments)."""
        return self.shipped_value - self.paid_total

    @property
    def truck_progress(self):
        """(sent, planned) for the Yuklar progress bar. `planned` is None when the
        kelishuv never set a target, so the bar shows a count without a total."""
        return self.shipments.count(), self.planned_trucks

    @property
    def expected_value(self):
        """The kelishuv's real cost — see ContractLine.expected_value. Equals
        total_value while every truck goes at the agreed narx."""
        return sum((ln.expected_value for ln in self.lines.all()), Decimal("0"))

    @property
    def payable_left(self):
        """How much more will be paid on this kelishuv. Paying before a yuk is sent
        is normal (avans), so the ceiling is the whole kelishuv rather than the
        goods shipped so far — but measured at what the goods really cost, so the
        figure on screen and the Qolgan/Yakunlangan filter can never disagree."""
        return self.expected_value - self.paid_total

    @property
    def is_settled(self):
        """Yopilgan: every kg has gone out AND nothing is left to pay. Anything
        else is still open business — goods owed to us, money owed to them, or
        both — which is what the default Kelishuvlar view shows.

        Uses payable_left rather than debt so it is the same number the Qolgan
        to'lov column shows; with every kg shipped the two are equal anyway."""
        return self.remaining_kg <= 0 and self.payable_left <= 0

    def __str__(self):
        # the hamkor is already in the code
        return f"{self.code} · {self.brand_summary}"


class ContractLine(models.Model):
    """One product on a kelishuv: a brand at an agreed kg and USD/kg price. The
    thing trucks are booked against — "qolgan kg" is tracked per product, not per
    kelishuv, so a kelishuv can be half-delivered on one brand and untouched on
    another."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE,
                                 related_name="lines", verbose_name="Kelishuv")
    brand = models.CharField("Granula markasi", max_length=100)
    kg = models.DecimalField("Kelishilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4)
    position = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Kelishuv mahsuloti"
        verbose_name_plural = "Kelishuv mahsulotlari"

    @property
    def total_value(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def shipped_kg(self):
        return sum((sl.kg for sl in self.shipment_lines.all()), Decimal("0"))

    @property
    def remaining_kg(self):
        return self.kg - self.shipped_kg

    @property
    def shipped_value(self):
        return sum((sl.goods_value for sl in self.shipment_lines.all()), Decimal("0"))

    @property
    def expected_value(self):
        """What this product will really cost: the trucks that went at the prices
        they actually went at, plus whatever is still to come at the agreed narx.
        The kelishuv's own total is only the estimate — a truck may be priced up
        or down against it."""
        left = self.remaining_kg if self.remaining_kg > 0 else Decimal("0")
        return (self.shipped_value + left * self.price).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.brand} · {self.kg} kg"


class ShipmentStatus(models.Model):
    """Admin-editable ordered status chain. Exactly one row is the arrival status —
    reaching it is what turns a shipment into a warehouse lot, so it is protected:
    saving another row as arrival demotes the rest, and the arrival row can't be
    deleted (guarded in the view)."""

    name = models.CharField("Nomi", max_length=100, unique=True)
    order = models.PositiveSmallIntegerField("Tartib", default=0)
    is_arrival = models.BooleanField("Omborga kelish holati", default=False)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Yuk holati"
        verbose_name_plural = "Yuk holatlari"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_arrival:
            ShipmentStatus.objects.exclude(pk=self.pk).update(is_arrival=False)

    @classmethod
    def arrival(cls):
        return cls.objects.filter(is_arrival=True).first()

    def __str__(self):
        return self.name


class SupplierPayment(models.Model):
    """To'lov to one supplier contract. `amount` is always USD; a so'm payment is
    converted at entry and keeps its original figure + rate. Overpaying a contract
    is blocked at the form layer (per-contract model, no supplier prepayments).

    The hamkor is not paid directly — a middleman passes the money on and keeps a
    percentage for the delivery. `amount` is what the hamkor RECEIVES (so it is what
    settles their qarz); the middleman's cut rides on top of it and leaves the kassa
    as an expense. Paying 10,000 at 2% therefore costs 10,200."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="supplier_payments", verbose_name="Kelishuv")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    commission_percent = models.DecimalField(
        "Vositachi foizi (%)", max_digits=5, decimal_places=2, default=0, blank=True,
        help_text="Vositachisiz to'lov uchun bo'sh qoldiring")
    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=0, blank=True)
    amount_original = models.DecimalField("Asl summa (valyutada)", max_digits=18,
                                          decimal_places=2, default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.TRANSFER)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="supplier_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Hamkor to'lovi"
        verbose_name_plural = "Hamkor to'lovlari"

    @property
    def commission_amount(self):
        """The middleman's cut, on top of what the hamkor receives."""
        return (self.amount * self.commission_percent / 100).quantize(Decimal("0.01"))

    @property
    def total_out(self):
        """What actually leaves the kassa: the hamkor's money plus the cut."""
        return self.amount + self.commission_amount

    def __str__(self):
        return f"{self.contract_id} · {self.amount}$ ({self.date})"


def commission_total(payments):
    """Summed per row so the total always matches the rows on screen — a single
    SQL expression would round once at the end and could drift by cents."""
    return sum((p.commission_amount for p in payments), Decimal("0"))


class Shipment(models.Model):
    """Yuk: one load moving under a contract. Once it reaches the arrival status
    (arrived date set) it doubles as a warehouse lot in Phase 2."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="shipments", verbose_name="Kelishuv")
    status = models.ForeignKey(ShipmentStatus, on_delete=models.PROTECT,
                               related_name="shipments", verbose_name="Holat")
    sent = models.DateField("Jo'natilgan sana", null=True, blank=True)
    eta = models.DateField("Taxminiy kelish", null=True, blank=True)
    arrived = models.DateField("Yetib kelgan sana", null=True, blank=True)
    transport = models.CharField("Transport raqami", max_length=50, blank=True)
    container = models.CharField("Konteyner raqami", max_length=50, blank=True)
    # Who on our side owns this load — free text rather than a user FK, since the
    # mas'ul shaxs is not always someone with an account (the prototype carried
    # them as a plain "Logist: <name>" note).
    responsible = models.CharField("Mas'ul shaxs", max_length=120, blank=True)
    # Who is actually driving it — often known before the plate, and the number the
    # logist calls when a load goes quiet.
    driver_name = models.CharField("Haydovchi", max_length=120, blank=True)
    driver_phone = models.CharField("Haydovchi telefoni", max_length=30, blank=True)
    # The run is always Eron → O'zbekiston, so the route is a constant rather than
    # something the operator picks. Intermediate stops live on ShipmentLeg.
    origin = models.CharField("Qayerdan (jo'natilish joyi)", max_length=120,
                              blank=True, default="Eron")
    destination = models.CharField("Qayerga (yetkazish joyi)", max_length=120,
                                   blank=True, default="O'zbekiston")
    note = models.TextField("Izoh", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Yuk"
        verbose_name_plural = "Yuklar"

    @property
    def is_overdue(self):
        return self.arrived is None and self.eta is not None and self.eta < timezone.localdate()

    @property
    def days_late(self):
        return (timezone.localdate() - self.eta).days if self.is_overdue else 0

    @property
    def days_left(self):
        if self.arrived or not self.eta:
            return None
        return (self.eta - timezone.localdate()).days

    @property
    def kg(self):
        """Everything on the truck, across all its products."""
        return sum((ln.kg for ln in self.lines.all()), Decimal("0"))

    @property
    def goods_value(self):
        """The USD value of the goods on this load at their unit prices (before
        road/customs expenses). Admin-only in the UI — never shown to translators."""
        return sum((ln.goods_value for ln in self.lines.all()), Decimal("0"))

    @property
    def current_transport(self):
        """The vehicle/driver on the load now: the active leg's, else the last leg's,
        falling back to the load's own transport field when there are no legs."""
        legs = list(self.legs.all())
        if legs:
            active = next((leg for leg in legs if leg.is_current), None)
            return (active or legs[-1]).transport or self.transport
        return self.transport

    @property
    def expenses_total(self):
        return self.expenses.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def expense_per_kg(self):
        """Road/customs spend spread evenly over every kg on the truck, whichever
        product it belongs to. Transport and customs are charged for the load, not
        per brand, so kg is the honest split — a cheap brand and an expensive one
        riding together carry the same share of the freight."""
        total_kg = self.kg
        return self.expenses_total / total_kg if total_kg else Decimal("0")

    @property
    def is_lot(self):
        return self.arrived is not None

    @property
    def sold_kg(self):
        return sum((ln.sold_kg for ln in self.lines.all()), Decimal("0"))

    @property
    def reserved_kg(self):
        return sum((ln.reserved_kg for ln in self.lines.all()), Decimal("0"))

    @property
    def available_kg(self):
        return sum((ln.available_kg for ln in self.lines.all()), Decimal("0"))

    @property
    def brand_summary(self):
        """Every product on the truck, named in full."""
        return ", ".join(ln.brand for ln in self.lines.all())

    def __str__(self):
        return f"Yuk #{self.pk} · {self.brand_summary} · {self.kg} kg"


class ShipmentLine(models.Model):
    """One product on one truck, and the unit the ombor actually deals in: a lot is
    a ShipmentLine of an arrived Shipment, so sotuv and bron attach here rather than
    to the truck. A truck carrying two brands is therefore two lots."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="lines", verbose_name="Yuk")
    contract_line = models.ForeignKey(ContractLine, on_delete=models.PROTECT,
                                      related_name="shipment_lines",
                                      verbose_name="Mahsulot")
    kg = models.DecimalField("Yuborilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4,
                                null=True, blank=True,
                                help_text="Bo'sh qoldirilsa kelishuv narxi olinadi")
    position = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Yuk mahsuloti"
        verbose_name_plural = "Yuk mahsulotlari"

    @property
    def brand(self):
        return self.contract_line.brand

    @property
    def arrived(self):
        return self.shipment.arrived

    @property
    def is_lot(self):
        return self.shipment.arrived is not None

    @property
    def unit_price(self):
        """This truck's own USD/kg for this product when set, else the agreed
        kelishuv price — each truck can carry a different price (per-truck
        pricing)."""
        return self.price if self.price is not None else self.contract_line.price

    @property
    def goods_value(self):
        return (self.kg * self.unit_price).quantize(Decimal("0.01"))

    @property
    def landed_cost_per_kg(self):
        """True cost of one kg of this product in this load: its unit price plus
        the truck's freight share. Snapshotted into sales so later expenses never
        retroactively change a past sale's profit."""
        return (self.unit_price + self.shipment.expense_per_kg).quantize(Decimal("0.0001"))

    @property
    def sold_kg(self):
        return sum((s.kg for s in self.sales.all()), Decimal("0"))

    @property
    def returned_kg(self):
        # kg flowed back into this lot by restocked returns on its sales
        total = Decimal("0")
        for s in self.sales.all():
            total += sum((r.kg for r in s.returns.all() if r.restock), Decimal("0"))
        return total

    @property
    def reserved_kg(self):
        return sum((r.kg for r in self.reservations.all() if r.status == "active"),
                   Decimal("0"))

    @property
    def available_kg(self):
        return self.kg - self.sold_kg - self.reserved_kg + self.returned_kg

    def __str__(self):
        return f"Lot #{self.pk} · {self.brand} · {self.kg} kg"


class Reservation(models.Model):
    """Bron: a customer reserves kg on a lot (arrived or still in-transit). While
    active it blocks that kg via `Shipment.reserved_kg`. Converting turns it into a
    Sale (only once the lot has arrived); cancelling frees the kg back up."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Faol"
        CONVERTED = "converted", "Sotuvga aylandi"
        CANCELLED = "cancelled", "Bekor qilindi"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="reservations", verbose_name="Mijoz")
    line = models.ForeignKey("ShipmentLine", on_delete=models.PROTECT,
                             related_name="reservations", verbose_name="Lot (mahsulot)")
    kg = models.DecimalField("Bron qilingan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4,
                                null=True, blank=True)
    status = models.CharField("Holat", max_length=10, choices=Status.choices,
                              default=Status.ACTIVE)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="reservations",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Bron"
        verbose_name_plural = "Bronlar"

    def __str__(self):
        return f"Bron #{self.pk} · {self.customer} · {self.kg} kg"


def arrived_lots():
    """Every lot in the ombor: the product lines of arrived trucks."""
    return (ShipmentLine.objects
            .filter(shipment__arrived__isnull=False)
            .select_related("contract_line", "shipment", "shipment__contract"))


def fifo_lots(brand):
    """Arrived lots of one brand that still have kg available, oldest arrival
    first (then id) — the FIFO consumption order for the ombor."""
    lots = arrived_lots().filter(contract_line__brand=brand).order_by(
        "shipment__arrived", "id")
    return [lot for lot in lots if lot.available_kg > 0]


def brand_stock():
    """[(brand, available kg)] across arrived lots, for the FIFO sale form."""
    totals = {}
    for lot in arrived_lots():
        avail = lot.available_kg
        if avail > 0:
            totals[lot.brand] = totals.get(lot.brand, Decimal("0")) + avail
    return sorted(totals.items())


class Sale(models.Model):
    """Sotuv: kg sold from one arrived lot at a sale price. A sale entered by brand
    is split FIFO across the oldest lots (one row per lot slice). Snapshots that
    lot's landed cost at sale time so later shipment expenses never retroactively
    change a past sale's profit."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="sales", verbose_name="Mijoz")
    line = models.ForeignKey("ShipmentLine", on_delete=models.PROTECT,
                             related_name="sales", verbose_name="Lot (mahsulot)")
    reservation = models.ForeignKey("Reservation", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="Bron")
    kg = models.DecimalField("Sotilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg sotuv narxi (USD)", max_digits=14, decimal_places=4)
    cost_price = models.DecimalField("1 kg tan narxi (USD)", max_digits=14, decimal_places=4)
    date = models.DateField("Sana", default=timezone.localdate)
    debt_deadline = models.DateField("To'lov muddati", null=True, blank=True)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="sales", verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Sotuv"
        verbose_name_plural = "Sotuvlar"

    @property
    def total(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def returned_amount(self):
        if not hasattr(self, "returns"):  # relation lands in Task 5
            return Decimal("0")
        return sum((r.amount for r in self.returns.all()), Decimal("0"))

    @property
    def net_total(self):
        return self.total - self.returned_amount

    @property
    def paid(self):
        if not hasattr(self, "allocations"):  # relation lands in Task 4
            return Decimal("0")
        return self.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def remaining(self):
        return self.net_total - self.paid

    @property
    def is_paid(self):
        return self.remaining <= 0

    @property
    def is_overdue(self):
        return (self.remaining > 0 and self.debt_deadline is not None
                and self.debt_deadline < timezone.localdate())

    @property
    def profit(self):
        return ((self.price - self.cost_price) * self.kg).quantize(Decimal("0.01")) - self._returned_profit

    @property
    def _returned_profit(self):
        if not hasattr(self, "returns"):  # relation lands in Task 5
            return Decimal("0")
        return sum(((r.price - self.cost_price) * r.kg for r in self.returns.all() if r.restock),
                  Decimal("0"))

    def __str__(self):
        return f"Sotuv #{self.pk} · {self.customer} · {self.kg} kg"


class Return(models.Model):
    """Qaytarish: goods coming back from a sale. Credits the customer's debt at the
    sale price (kg * price) regardless of restock; if restocked, the kg flows back
    into the lot via Shipment.returned_kg / available_kg."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE,
                             related_name="returns", verbose_name="Sotuv")
    kg = models.DecimalField("Qaytarilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4)
    date = models.DateField("Sana", default=timezone.localdate)
    restock = models.BooleanField("Omborga qaytarilsinmi", default=True)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="returns",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Qaytarish"
        verbose_name_plural = "Qaytarishlar"

    @property
    def amount(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    def __str__(self):
        return f"Qaytarish #{self.pk} · sotuv #{self.sale_id} · {self.kg} kg"


class CustomerPayment(models.Model):
    """To'lov received from a customer. `amount` is always USD; a so'm payment is
    converted at entry and keeps its original figure + rate. Not tied to one sale —
    it auto-allocates (FIFO or manual pick) via `allocate_customer_payment`; any
    leftover is the customer's advance (avans)."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="customer_payments", verbose_name="Mijoz")
    reservation = models.ForeignKey("Reservation", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="earmarked_payments", verbose_name="Bron uchun")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=0, blank=True)
    amount_original = models.DecimalField("Asl summa (valyutada)", max_digits=18,
                                          decimal_places=2, default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.TRANSFER)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="customer_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Mijoz to'lovi"
        verbose_name_plural = "Mijoz to'lovlari"

    def __str__(self):
        return f"{self.customer_id} · {self.amount}$ ({self.date})"


class PaymentAllocation(models.Model):
    """One slice of a CustomerPayment applied to one Sale. A payment can spread
    across many sales (FIFO or manual pick); a sale can be paid off by many
    payments (including advances applied later)."""

    payment = models.ForeignKey(CustomerPayment, on_delete=models.CASCADE,
                                related_name="allocations", verbose_name="To'lov")
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE,
                             related_name="allocations", verbose_name="Sotuv")
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "To'lov taqsimoti"
        verbose_name_plural = "To'lov taqsimotlari"

    def __str__(self):
        return f"{self.payment_id} → sotuv #{self.sale_id}: {self.amount}$"


def allocate_customer_payment(payment, picks=None):
    """Allocate a payment across the customer's outstanding sales. `picks` is an
    optional list of (sale_id, amount) chosen in the form; the rest (or all, if no
    picks) auto-fills oldest-first. Leftover stays unallocated = advance."""
    remaining = payment.amount - (payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0"))
    with transaction.atomic():
        if picks:
            for sale_id, amt in picks:
                # Fail safe: a stale or tampered pick id (deleted sale, or one
                # belonging to another customer) is skipped, not fatal.
                sale = Sale.objects.filter(pk=sale_id, customer=payment.customer).first()
                if sale is None:
                    continue
                amt = min(Decimal(amt), sale.remaining, remaining)
                if amt > 0:
                    PaymentAllocation.objects.create(payment=payment, sale=sale, amount=amt)
                    remaining -= amt
        # FIFO the leftover across still-outstanding sales
        for sale in payment.customer.sales.order_by("date", "id"):
            if remaining <= 0:
                break
            take = min(sale.remaining, remaining)
            if take > 0:
                PaymentAllocation.objects.create(payment=payment, sale=sale, amount=take)
                remaining -= take
    return remaining  # the advance left over


def apply_customer_advance(sale):
    """Pull this customer's unallocated payment money onto a new sale, oldest
    payment first, until the sale is covered or the advance runs out. If the sale
    came from a reservation (bron), money earmarked for that reservation
    (`CustomerPayment.reservation == sale.reservation`) applies FIRST, then the
    fallback is the customer's general oldest-first advances — same as before."""
    earmarked_pks = []
    with transaction.atomic():
        if sale.reservation_id:
            earmarked = sale.reservation.earmarked_payments.order_by("date", "id")
            for payment in earmarked:
                earmarked_pks.append(payment.pk)
                if sale.remaining <= 0:
                    break
                unallocated = (payment.amount
                              - (payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0")))
                take = min(unallocated, sale.remaining)
                if take > 0:
                    PaymentAllocation.objects.create(payment=payment, sale=sale, amount=take)
        # General advances: skip the earmarked payments already handled above, so
        # "earmark-first" is structural, not dependent on the fresh-query recompute.
        general = sale.customer.customer_payments.exclude(pk__in=earmarked_pks).order_by("date", "id")
        for payment in general:
            if sale.remaining <= 0:
                break
            unallocated = payment.amount - (payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0"))
            take = min(unallocated, sale.remaining)
            if take > 0:
                PaymentAllocation.objects.create(payment=payment, sale=sale, amount=take)


def trim_sale_allocations(sale):
    """After a return shrinks a sale's net_total, drop the now-excess allocation
    amount (newest allocation first) so Σ allocations ≤ net_total. The freed amount
    returns to its payment's spendable advance, reachable by apply_customer_advance —
    otherwise a return on a paid sale would strand money in a dead over-cap row."""
    over = (sale.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0")) - sale.net_total
    if over <= 0:
        return
    with transaction.atomic():
        for alloc in sale.allocations.order_by("-id"):
            if over <= 0:
                break
            if alloc.amount <= over:
                over -= alloc.amount
                alloc.delete()
            else:
                alloc.amount -= over
                alloc.save(update_fields=["amount"])
                over = Decimal("0")


class ShipmentExpense(models.Model):
    """Road/customs money spent on one load. Rolls into that load's landed cost:
    landed cost per kg = contract price + expenses ÷ kg (decision #3)."""

    class Category(models.TextChoices):
        CUSTOMS = "customs", "Bojxona"
        TRANSPORT = "transport", "Transport"
        ROAD = "road", "Yo'l xarajati"
        CERT = "cert", "Sertifikat"
        OTHER = "other", "Boshqa"

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="expenses", verbose_name="Yuk")
    date = models.DateField("Sana", default=timezone.localdate)
    category = models.CharField("Turkum", max_length=10, choices=Category.choices,
                                default=Category.OTHER)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=0, blank=True)
    amount_original = models.DecimalField("Asl summa (valyutada)", max_digits=18,
                                          decimal_places=2, default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipment_expenses",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Yuk xarajati"
        verbose_name_plural = "Yuk xarajatlari"

    def __str__(self):
        return f"{self.get_category_display()}: {self.amount}$ (yuk #{self.shipment_id})"


class ShipmentDelay(models.Model):
    """One ETA extension: the audit trail requirement — every push of the arrival
    date keeps its reason and author."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="delays", verbose_name="Yuk")
    old_eta = models.DateField("Avvalgi sana", null=True)
    new_eta = models.DateField("Yangi sana")
    reason = models.CharField("Kechikish sababi", max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipment_delays",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Yuk kechikishi"
        verbose_name_plural = "Yuk kechikishlari"

    def __str__(self):
        return f"{self.shipment_id}: {self.old_eta} → {self.new_eta}"


class ShipmentLeg(models.Model):
    """One segment of a load's journey: from one place to the next, driven by one
    vehicle. A load usually has a planned sequence of legs; an unplanned stop is just
    another leg inserted into the order. A driver hand-off = the next leg has a
    different `transport`. No money here — translators manage legs (they coordinate
    the drivers), same as they manage status and ETA."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="legs", verbose_name="Yuk")
    order = models.PositiveSmallIntegerField("Tartib", default=0)
    from_location = models.CharField("Qayerdan", max_length=120)
    to_location = models.CharField("Qayerga", max_length=120)
    transport = models.CharField("Haydovchi / transport", max_length=50, blank=True)
    container = models.CharField("Konteyner", max_length=50, blank=True)
    departed = models.DateField("Jo'natilgan sana", null=True, blank=True)
    arrived = models.DateField("Yetib kelgan sana", null=True, blank=True)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipment_legs",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Yo'nalish bosqichi"
        verbose_name_plural = "Yo'nalish bosqichlari"

    @property
    def is_current(self):
        """The active leg: departed but not yet arrived."""
        return self.departed is not None and self.arrived is None

    def __str__(self):
        return f"{self.from_location} → {self.to_location}"
