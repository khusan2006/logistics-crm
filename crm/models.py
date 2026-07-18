from decimal import Decimal

from django.conf import settings
from django.db import models
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
