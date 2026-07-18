from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import DecimalField, Sum
from django.utils import timezone

MONEY = DecimalField(max_digits=14, decimal_places=2)   # USD
QTY = DecimalField(max_digits=12, decimal_places=3)     # kg


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Hamkor"
        verbose_name_plural = "Hamkorlar"

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
    """Kelishuv: one brand of granula from one partner at one USD/kg price."""

    partner = models.ForeignKey(Partner, on_delete=models.PROTECT,
                                related_name="contracts", verbose_name="Hamkor")
    brand = models.CharField("Granula markasi", max_length=100)
    kg = models.DecimalField("Kelishilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4)
    created = models.DateField("Kelishuv sanasi", default=timezone.localdate)
    deadline = models.DateField("Yetkazish muddati")
    note = models.TextField("Izoh", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="contracts",
                                   verbose_name="Kim ochdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]
        verbose_name = "Kelishuv"
        verbose_name_plural = "Kelishuvlar"

    @property
    def total_value(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def shipped_kg(self):
        if not hasattr(self, "shipments"):  # relation lands in Task 7
            return Decimal("0")
        return self.shipments.aggregate(s=Sum("kg"))["s"] or Decimal("0")

    @property
    def remaining_kg(self):
        return self.kg - self.shipped_kg

    @property
    def paid_total(self):
        if not hasattr(self, "supplier_payments"):  # relation lands in Task 5
            return Decimal("0")
        return self.supplier_payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def debt(self):
        return self.total_value - self.paid_total

    def __str__(self):
        return f"#{self.pk} · {self.brand} · {self.partner}"


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
    is blocked at the form layer (per-contract model, no supplier prepayments)."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="supplier_payments", verbose_name="Kelishuv")
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
                                   null=True, related_name="supplier_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Hamkor to'lovi"
        verbose_name_plural = "Hamkor to'lovlari"

    def __str__(self):
        return f"{self.contract_id} · {self.amount}$ ({self.date})"


class Shipment(models.Model):
    """Yuk: one load moving under a contract. Once it reaches the arrival status
    (arrived date set) it doubles as a warehouse lot in Phase 2."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="shipments", verbose_name="Kelishuv")
    kg = models.DecimalField("Yuborilgan kg", max_digits=12, decimal_places=3)
    status = models.ForeignKey(ShipmentStatus, on_delete=models.PROTECT,
                               related_name="shipments", verbose_name="Holat")
    sent = models.DateField("Jo'natilgan sana", null=True, blank=True)
    eta = models.DateField("Taxminiy kelish", null=True, blank=True)
    arrived = models.DateField("Yetib kelgan sana", null=True, blank=True)
    transport = models.CharField("Transport raqami", max_length=50, blank=True)
    container = models.CharField("Konteyner raqami", max_length=50, blank=True)
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
    def expenses_total(self):
        return self.expenses.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def landed_cost_per_kg(self):
        """True cost of one kg in this load: contract price plus this load's own
        road/customs spend spread over its kg. Phase 2 snapshots this into sales."""
        extra = self.expenses_total / self.kg if self.kg else Decimal("0")
        return (self.contract.price + extra).quantize(Decimal("0.0001"))

    @property
    def is_lot(self):
        return self.arrived is not None

    @property
    def sold_kg(self):
        if not hasattr(self, "sales"):  # relation lands in Task 3
            return Decimal("0")
        return sum((s.kg for s in self.sales.all()), Decimal("0"))

    @property
    def returned_kg(self):
        # kg flowed back into this lot by restocked returns on its sales
        if not hasattr(self, "sales"):  # relation lands in Task 3
            return Decimal("0")
        total = Decimal("0")
        for s in self.sales.all():
            if not hasattr(s, "returns"):  # relation lands in Task 5
                continue
            total += sum((r.kg for r in s.returns.all() if r.restock), Decimal("0"))
        return total

    @property
    def reserved_kg(self):
        if not hasattr(self, "reservations"):  # relation lands in Task 6
            return Decimal("0")
        return sum((r.kg for r in self.reservations.all() if r.status == "active"), Decimal("0"))

    @property
    def available_kg(self):
        return self.kg - self.sold_kg - self.reserved_kg + self.returned_kg

    def __str__(self):
        return f"Yuk #{self.pk} · {self.contract.brand} · {self.kg} kg"


class Sale(models.Model):
    """Sotuv: kg sold from one arrived lot at a sale price. Snapshots that lot's
    landed cost at sale time so later shipment expenses never retroactively
    change a past sale's profit."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="sales", verbose_name="Mijoz")
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT,
                                 related_name="sales", verbose_name="Lot (yuk)")
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
    """Pull this customer's unallocated payment money (advance) onto a new sale,
    oldest payment first, until the sale is covered or the advance runs out."""
    with transaction.atomic():
        for payment in sale.customer.customer_payments.order_by("date", "id"):
            if sale.remaining <= 0:
                break
            unallocated = payment.amount - (payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0"))
            take = min(unallocated, sale.remaining)
            if take > 0:
                PaymentAllocation.objects.create(payment=payment, sale=sale, amount=take)


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
