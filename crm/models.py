from datetime import date as _date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
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


# The kurs used for money that was booked before dual-currency existed. Those rows
# recorded only a dollar figure, so their so'm value has to be assumed rather than
# recovered; 12,000 is the figure the operator gave for that back-fill.
LEGACY_RATE = Decimal("12000")


def convert_pair(typed, currency, rate, usd_places="0.01"):
    """(usd, uzs) for a figure the operator typed in `currency` at `rate` so'm/$.

    The typed side comes back untouched: round-tripping it through the rate would
    lose the exact figure that was actually agreed, and a price re-derived from its
    own conversion drifts by a tiyin every time it is edited. Only the other side is
    computed, once, and both are then stored — which is what lets a so'm total be a
    plain Sum() instead of a per-row CASE.

    Raises on a missing kurs: money with no rate has only one of its two values, and
    a row like that can never appear in a total of the other currency."""
    if not rate or rate <= Decimal("0"):
        raise ValueError("Valyutani hisoblash uchun dollar kursi kerak (rate > 0)")
    typed, rate = Decimal(typed), Decimal(rate)
    usd_q, uzs_q = Decimal(usd_places), Decimal("0.01")
    if currency == Currency.UZS:
        return ((typed / rate).quantize(usd_q, rounding=ROUND_HALF_UP),
                typed.quantize(uzs_q, rounding=ROUND_HALF_UP))
    return (typed.quantize(usd_q, rounding=ROUND_HALF_UP),
            (typed * rate).quantize(uzs_q, rounding=ROUND_HALF_UP))


def own_side(row, usd_value, uzs_value):
    """Whichever half of a stored pair is `row`'s OWN money — the side it was agreed
    in, or arrived in.

    Every qarz figure in the app goes through here. Reading the other half is a
    conversion neither party signed up to, derived at a kurs that has since moved: a
    so'm sotuv paid off in so'm still shows a dollar remainder, and a kelishuv settled
    in full never leaves the unfinished list. Costs are the one deliberate exception —
    see ShipmentLine.landed_cost_per_kg."""
    return uzs_value if row.is_som else usd_value


def _trucks_covered(paid, trucks):
    """How many yuklar `paid` covers, oldest truck first — the shared half of
    `Contract.trucks_paid_for` and its per-marka twin on ContractLine.

    `trucks` is (sort_key, value) pairs, each already priced in the money's own
    currency; the caller decides what a truck is worth, since a marka pays only
    for its share of a load carrying two.

    The answer carries the PART of the next truck the money reaches into —
    "0,2 / 2" rather than "0 / 2", which called a fifth of a truck nothing. It
    is only ever rounded DOWN, and to one decimal: a truck shown as a whole is a
    truck the hamkor is no longer owed for, so 1,96 must read as 1,9 and never
    as 2. Money running past the last truck is an avans and counts for no
    truck at all — the figure stops at `len(trucks)`."""
    covered = Decimal("0")
    for _key, value in sorted(trucks, key=lambda t: t[0]):
        if value <= 0:
            # Nothing is owed on it, so no payment is needed to cover it.
            covered += 1
            continue
        if paid <= 0:
            break
        if paid < value:
            covered += paid / value
            break
        paid -= value
        covered += 1
    return covered.quantize(Decimal("0.1"), rounding=ROUND_DOWN)


class MoneyEntry(models.Model):
    """Abstract: the two facts every money row needs on top of its value — which
    currency the operator typed in, and the kurs at that moment.

    The kurs is asked on dollar entries too. Without it a dollar row has no so'm
    value at all and can never join a so'm total, which is exactly the gap that made
    the old `exchange_rate = 0` dollar rows unreportable."""

    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=LEGACY_RATE)

    #: (dollar field, so'm field) on the concrete model — overridden by the price
    #: models, whose pair is price/price_uzs rather than amount/amount_uzs.
    money_fields = ("amount", "amount_uzs")

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        """Backstop: never store a row that has a dollar value but no so'm one.

        The forms always set both. Code that builds rows directly does not — the
        prototype importer, the seeders, a shell one-liner — and such a row would
        read as 0 so'm on every so'm screen, which looks like a real figure rather
        than a missing one. The gap is filled at the row's own kurs.

        Only a MISSING twin is filled; a stored value is never recomputed, so the
        exact figure the operator typed is safe."""
        usd_field, uzs_field = self.money_fields
        usd_value = getattr(self, usd_field, None)
        if usd_value and not getattr(self, uzs_field, None):
            setattr(self, uzs_field, self.in_som(usd_value))
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and usd_field in update_fields:
                kwargs["update_fields"] = list(update_fields) + [uzs_field]
        return super().save(*args, **kwargs)

    @property
    def is_som(self):
        """True when the operator typed this row in so'm. The screens read it to
        decide which of the two stored values to draw and which currency to ask the
        next figure about — a sotuv agreed in so'm is settled in so'm."""
        return self.currency == Currency.UZS

    def in_som(self, usd_value):
        """A figure derived from this row's money, in so'm at THIS row's kurs.

        Used for the computed values that have no stored so'm column of their own —
        a profit, a remaining balance. Rating them at the row's own entry-time kurs
        (rather than today's) is what keeps a past figure from moving after the
        fact, which is the same rule the stored pairs follow."""
        return (Decimal(usd_value or 0) * self.exchange_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)


class FeeBearer(models.TextChoices):
    """Who is out of pocket for the bank's cut on a perechisleniya.

    The bank takes its foiz either way — that is not a choice. What IS a choice is
    whose side of the ledger absorbs it: if we do, the other party is credited the
    whole figure and we are short by the fee; if they do, they are credited what
    actually reached them and the rest is still owed."""

    COMPANY = "company", "Kompaniyadan ushlansin"
    COUNTERPARTY = "counterparty", "Qarshi tomondan ushlansin"


class CashEntry(MoneyEntry):
    """Abstract: a row where money physically enters or leaves the kassa, so it can
    carry a bank foiz. Subclasses supply their own `amount` and `method` — the two
    differ in label and default per model, and Django forbids overriding an
    inherited field."""

    # The rows written in ONE go carry the same id: one to'lov paid half naqd and half
    # by perechisleniya is one settlement to the person making it and two movements of
    # money to the kassa. They stay two rows — the safe and the bank went down by
    # different figures, and only the transfer paid a bank foiz — but the id is what
    # lets a screen draw them as the single payment they are.
    #
    # Null on everything entered before the field existed, and on a to'lov that moved
    # one way, which is one row by nature. Same shape and the same reasoning as
    # `Sale.group`, which does this for the markalar bought in one trip.
    group = models.UUIDField("To'lov guruhi", null=True, blank=True,
                             editable=False, db_index=True)
    fee_percent = models.DecimalField(
        "Perechisleniya foizi (%)", max_digits=5, decimal_places=2, default=0, blank=True,
        help_text="Faqat perechisleniya uchun; naqd va kartada e'tiborga olinmaydi")
    # Blank means "as this kind of row has always behaved" — see `fee_on_company`.
    # Stored blank rather than back-filled so no existing row is restated: a to'lov
    # booked before the question was asked is left saying nothing about it.
    fee_bearer = models.CharField(
        "Komissiyani kim ko'taradi", max_length=12, choices=FeeBearer.choices,
        blank=True, default="")

    #: What a blank `fee_bearer` means for this model. Money coming IN has always
    #: been the sender's loss — 1000 sent at 2% paid off 980 — while money going OUT
    #: has always ridden on top, so the other side received the full figure.
    default_fee_bearer = FeeBearer.COMPANY

    #: The answers that describe the SETTLEMENT rather than the way the money moved:
    #: whose it was, what it was for, when. A split to'lov asks these once in its
    #: header and stamps every row with them, so they must agree across a `group` —
    #: two rows of one payment cannot have bought different markalar or landed on
    #: different days. Editing reopens a single row and shows the same boxes, which
    #: is where they could drift apart; `_sync_settlement` carries a correction back
    #: across the rest. Empty means the model is never entered as a split.
    settlement_fields = ()

    class Meta:
        abstract = True

    @property
    def settlement_rows(self):
        """The rows entered together with this one, in the order they were typed —
        itself alone when the money moved one way."""
        if self.group is None:
            return [self]
        return list(type(self).objects.filter(group=self.group).order_by("pk"))

    @property
    def fee_on_company(self):
        """True when WE are the ones short by the bank's cut."""
        return (self.fee_bearer or self.default_fee_bearer) == FeeBearer.COMPANY

    @property
    def fee_amount(self):
        """The bank's cut on a perechisleniya. Naqd and karta never charge one, so
        the foiz is ignored rather than trusted if the method later changes."""
        if self.method != PayMethod.TRANSFER or not self.fee_percent:
            return Decimal("0")
        return (self.amount * self.fee_percent / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def fee_amount_uzs(self):
        """The foiz in so'm, taken as a slice of the row's stored so'm value rather
        than reconverted — the cut was charged at the kurs of the day the money
        moved, so it cannot be allowed to drift from the amount it came out of."""
        if not self.amount:
            return Decimal("0")
        return (self.amount_uzs * self.fee_amount / self.amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)


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


class HeldFloat:
    """The money arithmetic shared by every top-up we send somebody who then spends
    it on our behalf — a logist funding drivers, a bojxonachi clearing loads.

    One rule in one place, because it is the rule that is easy to get wrong: the
    bank's foiz has to come off exactly ONE side. When we carry it the other party
    is funded the whole figure and the kassa is out that much more; when they carry
    it, it comes out of the same money and they are funded by that much less.
    Written twice, the two halves drifted and charged the same fee to both sides.

    Concrete classes are CashEntry subclasses (they supply `amount`, `amount_uzs`,
    `method` and the foiz) and name the currency their holder's balance is kept in."""

    #: The currency the holder's balance is kept in — what a top-up has to cross to
    #: land in it, and therefore the one case a kurs was really chosen.
    float_currency = Currency.USD

    @property
    def net_amount(self):
        """What actually reached them — what becomes theirs to spend, so what their
        balance grows by."""
        return self.amount if self.fee_on_company else self.amount - self.fee_amount

    @property
    def net_amount_uzs(self):
        return uzs_slice(self, self.net_amount)

    @property
    def total_out(self):
        """What the kassa loses: their money, plus the bank's cut when we carry it."""
        return self.amount + (self.fee_amount if self.fee_on_company else Decimal("0"))

    @property
    def total_out_uzs(self):
        fee = self.in_som(self.fee_amount) if self.fee_on_company else Decimal("0")
        return self.amount_uzs + fee

    @property
    def crosses_currency(self):
        """True when the money sent is not the money the balance is kept in — the
        one case the kurs on this row did real work."""
        return self.currency != self.float_currency


class Logist(models.Model):
    """Logist: the person who arranges transport and pays the drivers for us.

    We send them money in a lump; they hand a driver an advance when a yuk goes out.
    So they carry a balance — our money sitting in their pocket — and it is allowed
    to go negative, because a logist will sometimes pay a driver out of their own
    cash before we have topped them up. A negative balance is our debt to them."""

    name = models.CharField("Ismi", max_length=200)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    note = models.TextField("Izoh", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Logist"
        verbose_name_plural = "Logistlar"

    @property
    def received_total(self):
        """What we have sent them. `net_amount` rather than `amount`: a bank foiz
        never reached the logist, so it never became theirs to spend."""
        if not hasattr(self, "payments"):
            return Decimal("0")
        return sum((p.net_amount for p in self.payments.all()), Decimal("0"))

    @property
    def received_total_uzs(self):
        if not hasattr(self, "payments"):
            return Decimal("0")
        return sum((p.net_amount_uzs for p in self.payments.all()), Decimal("0"))

    @property
    def paid_total(self):
        """What they have handed to drivers on our loads."""
        if not hasattr(self, "driver_advances"):
            return Decimal("0")
        return sum((e.amount for e in self.driver_advances.all()), Decimal("0"))

    @property
    def paid_total_uzs(self):
        if not hasattr(self, "driver_advances"):
            return Decimal("0")
        return sum((e.amount_uzs for e in self.driver_advances.all()), Decimal("0"))

    @property
    def latest_rate(self):
        """The kurs from this logist's most recent top-up.

        A driver advance is handed over in dollars out of money we already sent, so
        the honest so'm value of it is the rate that money was converted at — not
        whatever today's rate happens to be. Falls back to the legacy rate for a
        logist who has not been funded yet, so an advance can still be recorded."""
        latest = self.payments.order_by("-date", "-created_at").first()
        return latest.exchange_rate if latest else LEGACY_RATE

    @property
    def balance(self):
        """Positive = our money still in their hands. Negative = they fronted it and
        we owe them.

        The blended figure, kept for `latest_rate` and the detail page's running
        column. What is out there per heap is `balance_by_currency`."""
        return self.received_total - self.paid_total

    @property
    def balance_uzs(self):
        return self.received_total_uzs - self.paid_total_uzs

    def received_by_currency(self):
        """[(currency, yuborilgan)] — what we have sent them, per heap.

        `net_amount` rather than `amount`: a bank foiz never reached the logist, so it
        never became theirs to spend."""
        if not hasattr(self, "payments"):
            return []
        return _by_currency((p.currency, own_side(p, p.net_amount, p.net_amount_uzs))
                            for p in self.payments.all())

    def paid_by_currency(self):
        """[(currency, berilgan)] — what they have handed to drivers on our loads, in
        the currency each advance was handed over in."""
        if not hasattr(self, "driver_advances"):
            return []
        return _by_currency((e.currency, own_side(e, e.amount, e.amount_uzs))
                            for e in self.driver_advances.all())

    def balance_by_currency(self):
        """[(currency, qoldiq)] — positive = our money still in their hands, negative =
        they fronted it themselves.

        Split the way a bojxonachi's hisob is. It used to be one dollar figure with a
        so'm restatement, on the assumption that every driver advance is paid in
        dollars — so money sent in so'm was converted into a dollar float. Now that
        logistlar are funded in so'm too, that assumption prints a figure nobody
        handed over, and it hid a real gap: a logist square in dollars but short in
        so'm dropped out of both kassa tiles altogether.

        Both signs can be on screen at once, and that is a real state rather than a
        contradiction: so'm left over while a dollar advance ran short is two facts,
        and netting them needs a kurs neither was moved at."""
        entries = list(self.received_by_currency())
        entries += [(currency, -amount) for currency, amount in self.paid_by_currency()]
        return _by_currency(entries)

    def held_by_currency(self):
        """Only the heaps that are ours to get back."""
        return [(c, a) for c, a in self.balance_by_currency() if a > 0]

    def owed_by_currency(self):
        """Only the heaps we owe them, positive so they read as an amount rather than
        as a deficit."""
        return [(c, -a) for c, a in self.balance_by_currency() if a < 0]

    def __str__(self):
        return self.name


class CustomsAgent(models.Model):
    """Bojxonachi: the person at customs we send money to so a yuk clears legally.

    The same shape as a Logist — an outside party carrying our money, allowed to go
    negative when they cover a load out of their own pocket — with the one
    difference that is why it is its own model: the money goes out PER LOAD, as an
    estimate, before anyone knows what clearing will actually cost. We send ~40 mln
    for a truck and it comes back 37, or 39, or exactly 40. Nobody finds out until
    afterwards, which is the fact this whole feature exists to record.

    What was not spent stays with them and funds the next load, so the balance is a
    running float exactly like a logist's; what is extra is that the gap is readable
    per load (Shipment.customs_diff_by_currency) instead of only in aggregate.

    Where it parts company with a logist is the currency. A logist's hisob is one
    heap: every driver advance is booked in dollars, so its so'm figure is that same
    money restated. Bojxona money is not — it goes out in so'm and sometimes in
    dollars, and a clearing is paid in whichever was sent. So it is TWO heaps, and
    read the way every other two-sided figure in the app is: bucketed by the
    currency each row actually moved in, never added across.

    Summing them is the specific mistake this is written to avoid. Adding each row's
    so'm column would take a $4 000 to'lov's derived twin — 50 mln at that day's
    kurs — and pile it on top of real so'm, so a bojxonachi holding 3 mln would read
    as holding 53 mln. That is the same defect kassa_cash_by_currency exists to
    undo."""

    name = models.CharField("Ismi", max_length=200)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    note = models.TextField("Izoh", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Bojxonachi"
        verbose_name_plural = "Bojxonachilar"

    def received_by_currency(self):
        """[(currency, yuborilgan)] — what we have sent them, per heap.

        `net_amount` rather than `amount`, for the same reason a logist's is: a bank
        foiz never reached them, so it never became theirs to spend."""
        if not hasattr(self, "payments"):
            return []
        return _by_currency(
            (p.currency, own_side(p, p.net_amount, p.net_amount_uzs))
            for p in self.payments.all())

    def spent_by_currency(self):
        """[(currency, sarflangan)] — what they have actually paid out at bojxona on
        our loads, in the currency each clearing was paid in."""
        if not hasattr(self, "expenses"):
            return []
        return _by_currency(
            (e.currency, own_side(e, e.amount, e.amount_uzs))
            for e in self.expenses.all())

    def balance_by_currency(self):
        """[(currency, qoldiq)] — positive = our money still in their hands,
        negative = they covered a load themselves and we owe them.

        Both signs can be on screen at once, and that is a real state rather than a
        contradiction: money left over from a so'm truck while a dollar clearing ran
        short is two facts, and netting them needs a kurs neither was moved at."""
        entries = [(currency, amount)
                   for currency, amount in self.received_by_currency()]
        entries += [(currency, -amount)
                    for currency, amount in self.spent_by_currency()]
        return _by_currency(entries)

    def held_by_currency(self):
        """Only the heaps that are ours to get back."""
        return [(c, a) for c, a in self.balance_by_currency() if a > 0]

    def owed_by_currency(self):
        """Only the heaps we owe them, positive so they read as an amount rather
        than as a deficit."""
        return [(c, -a) for c, a in self.balance_by_currency() if a < 0]

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
    def sales_total_uzs(self):
        if not hasattr(self, "sales"):
            return Decimal("0")
        return sum((s.net_total_uzs for s in self.sales.all()), Decimal("0"))

    @property
    def paid_total(self):
        """Summed net, per row: a perechisleniya foiz never reached us, so it settles
        nothing. This has to match what the allocations do — aggregating the gross
        `amount` in SQL would drop this mijoz's qarz by 1000 while the sale it paid
        only fell by 980, and the two screens would disagree by the fee."""
        if not hasattr(self, "customer_payments"):  # relation lands in a later Phase-2 task
            return Decimal("0")
        return sum((p.settled_amount for p in self.customer_payments.all()), Decimal("0"))

    @property
    def paid_total_uzs(self):
        if not hasattr(self, "customer_payments"):
            return Decimal("0")
        return sum((p.settled_amount_uzs for p in self.customer_payments.all()), Decimal("0"))

    @property
    def refunded_total(self):
        """Vazvrat money going back as CASH rather than staying as avans.

        It stops being an avans the moment we say it is leaving the kassa: the mijoz
        no longer holds credit to spend on their next sotuv, they hold a payout we
        owe them. Counted from the PROMISE and not from the payment — whether the
        cash has physically gone yet is the kassa's question, not this balance's, and
        a mijoz waiting on their money has no avans either way."""
        if not hasattr(self, "return_batches"):
            return Decimal("0")
        return sum((s.amount for b in self.return_batches.all()
                    for s in b.settlements.all() if s.is_cash), Decimal("0"))

    @property
    def refunded_total_uzs(self):
        if not hasattr(self, "return_batches"):
            return Decimal("0")
        return sum((s.amount_uzs for b in self.return_batches.all()
                    for s in b.settlements.all() if s.is_cash), Decimal("0"))

    @property
    def balance(self):
        """Positive = customer owes us (qarz); negative = advance (avans)."""
        return self.sales_total - self.paid_total + self.refunded_total

    @property
    def balance_uzs(self):
        return self.sales_total_uzs - self.paid_total_uzs + self.refunded_total_uzs

    def __str__(self):
        return self.name


class Contract(models.Model):
    """Kelishuv: an agreement with one partner covering one or more products.
    Each product — brand, kg, USD/kg — is a ContractLine; this model is the header
    (who, when, by when) and the sum of its lines."""

    partner = models.ForeignKey(Partner, on_delete=models.PROTECT,
                                related_name="contracts", verbose_name="Hamkor")
    # A kelishuv is struck in ONE currency and settled in that same one: the qarz,
    # the to'lov ceiling and the Yopilgan test all read this side and never the
    # converted twin. The kurs still rides on every row, but only so the goods can
    # be priced into a landed cost — it never moves what is owed.
    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    # The code the client reads — sobir-3 — split so "next number for sobir" is a
    # Max() instead of parsing integers back out of strings (sobir-10 < sobir-9).
    code_slug = models.CharField(max_length=120, db_index=True, editable=False)
    code_number = models.PositiveIntegerField(editable=False)
    created = models.DateField("Kelishuv sanasi", default=timezone.localdate)
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

    @property
    def is_som(self):
        """True when the kelishuv was struck in so'm. Same name the money rows carry,
        so a screen can ask the question of either without knowing which it holds."""
        return self.currency == Currency.UZS

    def _own(self, usd_value, uzs_value):
        """Whichever half of a stored pair is the kelishuv's OWN money — see
        `own_side`, which the sotuv and mijoz sides read through too."""
        return own_side(self, usd_value, uzs_value)

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
    def total_value_uzs(self):
        return sum((ln.total_value_uzs for ln in self.lines.all()), Decimal("0"))

    @property
    def expected_value_uzs(self):
        return sum((ln.expected_value_uzs for ln in self.lines.all()), Decimal("0"))

    @property
    def payable_left_uzs(self):
        return self.expected_value_uzs - self.paid_total_uzs

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
        """Gross, unlike the mijoz side: a hamkor is credited the whole `amount`
        because the vositachi cut and the bank foiz ride on top of it rather than
        coming out of it. Their qarz falls by what they receive."""
        if not hasattr(self, "supplier_payments"):  # relation lands in Task 5
            return Decimal("0")
        return sum((p.credited_amount for p in self.supplier_payments.all()), Decimal("0"))

    @property
    def paid_total_uzs(self):
        if not hasattr(self, "supplier_payments"):
            return Decimal("0")
        return sum((p.credited_amount_uzs for p in self.supplier_payments.all()),
                   Decimal("0"))

    @property
    def shipped_value_uzs(self):
        return sum((ln.shipped_value_uzs for ln in self.lines.all()), Decimal("0"))

    @property
    def debt_uzs(self):
        return self.shipped_value_uzs - self.paid_total_uzs

    @property
    def commission_accrued(self):
        """Total vositachi cut paid across this kelishuv's hamkor payments so far.
        Grows as installments are paid; this is real money already out of the kassa."""
        if not hasattr(self, "supplier_payments"):
            return Decimal("0")
        return sum((p.commission_amount for p in self.supplier_payments.all()), Decimal("0"))

    @property
    def commission_per_kg(self):
        """The vositachi cut spread over the kelishuv's WHOLE agreed kg, so every
        load carries the same commission/kg. Live: paying more (or editing a
        payment) re-prices every load on this kelishuv, sold ones included."""
        kg = self.kg
        if not kg:
            return Decimal("0")
        return (self.commission_accrued / kg).quantize(Decimal("0.0001"))

    @property
    def shipped_value(self):
        """USD value of the goods actually sent (each truck line at its own unit
        price). The payable to the partner accrues per shipped truck, not on
        signing."""
        return sum((ln.shipped_value for ln in self.lines.all()), Decimal("0"))

    @property
    def debt(self):
        """What we owe the partner NOW: shipped value minus payments.

        Goes NEGATIVE on purpose. Paying before a truck is sent is normal, so the
        form's ceiling is the whole kelishuv (`payable_left`) rather than this —
        anything paid ahead of the goods reads here as an avans we are owed, which
        is what `partner_positions()` splits back out."""
        return self.shipped_value - self.paid_total

    @property
    def planned_trucks(self):
        """How many trucks the kelishuv is expected to take — the sum of what its
        products each plan.

        The target used to be a field on the kelishuv, typed once for the whole
        agreement. It is asked per MAHSULOT now, because that is the unit trucks
        are actually booked against: a kelishuv for two markalar is two delivery
        schedules sharing a piece of paper, and one number across both could not
        say which of them still owed a truck.

        None, not 0, when no product sets one — the bar shows a count with no
        total rather than pretending to a target of nothing."""
        counts = [ln.planned_trucks for ln in self.lines.all() if ln.planned_trucks]
        return sum(counts) if counts else None

    @property
    def trucks_paid_for(self):
        """(covered, sent) — how many of the yuklar already sent the money paid so
        far covers.

        A to'lov is not booked against a particular truck, so "which one did this
        pay for" has no stored answer. It is read the way the debt is actually
        worked off: oldest truck first, each covered in full before the next
        starts, which is the order a hamkor settles in.

        `covered` is FRACTIONAL — money part-way through a truck shows as the
        part it is, never rounded up into a load the hamkor is still owed for.
        See `_trucks_covered`.

        In the kelishuv's own currency on both sides. Comparing a so'm to'lov
        against a dollar truck at today's kurs would move the count every time
        the market did."""
        # A truck with no sent date has not left yet, so it queues behind the
        # ones that have rather than sorting to the front as NULL.
        trucks = [((truck.sent is None, truck.sent, truck.pk),
                   own_side(self, truck.goods_value, truck.goods_value_uzs))
                  for truck in self.shipments.all()]
        return _trucks_covered(self.paid_total_own, trucks), len(trucks)

    @property
    def truck_progress(self):
        """(sent, planned) for the Yuklar progress bar. `planned` is None when the
        kelishuv never set a target, so the bar shows a count without a total.

        `sent` counts TRUCKS, so a truck carrying both markalar counts once here
        while counting under each product in `ContractLine.truck_progress` — it
        really did carry both. The two therefore need not add up, and the per-
        product figures are the ones to read when they disagree."""
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

    # The same five figures in the kelishuv's OWN currency. These are the ones every
    # qarz screen, the to'lov ceiling and the Yopilgan test read; the dollar/so'm
    # pairs above stay because a landed cost still has to mix the two.
    @property
    def total_value_own(self):
        return self._own(self.total_value, self.total_value_uzs)

    @property
    def expected_value_own(self):
        return self._own(self.expected_value, self.expected_value_uzs)

    @property
    def paid_total_own(self):
        return self._own(self.paid_total, self.paid_total_uzs)

    @property
    def shipped_value_own(self):
        return self._own(self.shipped_value, self.shipped_value_uzs)

    @property
    def unassigned_paid_own(self):
        """Money paid on this kelishuv that names no product, in its own currency.

        Every to'lov entered before a to'lov could name one lands here, so on a
        multi-marka kelishuv it is what stops the per-marka bars from reading as
        though nothing had been paid at all. It empties as the old to'lovlar are
        edited and given a marka."""
        assigned = sum((self._own(ln.paid_total, ln.paid_total_uzs)
                        for ln in self.lines.all()), Decimal("0"))
        return self.paid_total_own - assigned

    @property
    def payable_left_own(self):
        return self.expected_value_own - self.paid_total_own

    @property
    def debt_own(self):
        return self.shipped_value_own - self.paid_total_own

    @property
    def is_settled(self):
        """Yopilgan: every kg has gone out AND nothing is left to pay. Anything
        else is still open business — goods owed to us, money owed to them, or
        both — which is what the default Kelishuvlar view shows.

        Measured in the kelishuv's own currency: a so'm kelishuv settled in full
        would otherwise keep a dollar remainder for as long as the kurs has moved
        since it was struck, and could never leave the Tugallanmagan list.

        Uses payable_left rather than debt so it is the same number the Qolgan
        to'lov column shows; with every kg shipped the two are equal anyway."""
        return self.remaining_kg <= 0 and self.payable_left_own <= 0

    def __str__(self):
        # the hamkor is already in the code
        return f"{self.code} · {self.brand_summary}"


class ContractLine(MoneyEntry):
    """One product on a kelishuv: a brand at an agreed kg and per-kg price. The
    thing trucks are booked against — "qolgan kg" is tracked per product, not per
    kelishuv, so a kelishuv can be half-delivered on one brand and untouched on
    another.

    The narx is agreed in whichever currency the kelishuv was struck in — the line
    inherits it rather than choosing its own, so "what is still owed on this
    kelishuv" can be one figure in one currency. Both values are still kept at that
    day's kurs, because a landed cost has to mix currencies even when a qarz may
    not."""

    money_fields = ("price", "price_uzs")

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE,
                                 related_name="lines", verbose_name="Kelishuv")
    brand = models.CharField("Granula markasi", max_length=100)
    kg = models.DecimalField("Kelishilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4)
    price_uzs = models.DecimalField("1 kg narxi (so'm)", max_digits=18,
                                    decimal_places=2, default=0)
    # How many trucks THIS product is expected to take. Optional: it is often not
    # known when the agreement is signed, and old kelishuvlar never had it.
    planned_trucks = models.PositiveIntegerField(
        "Nechta mashina", null=True, blank=True,
        help_text="Shu mahsulot uchun rejalashtirilgan mashinalar soni")
    position = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Kelishuv mahsuloti"
        verbose_name_plural = "Kelishuv mahsulotlari"

    def save(self, *args, **kwargs):
        """Backstop: a product line is always in its kelishuv's currency.

        The form already sets it. Code that builds rows directly does not — the
        prototype importer, the seeders, a shell one-liner — and a line struck in
        the other currency would price its trucks into a `shipped_value_own` the
        kelishuv's qarz is not measured in."""
        if self.contract_id:
            self.currency = self.contract.currency
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = list(dict.fromkeys(
                    list(kwargs["update_fields"]) + ["currency"]))
        return super().save(*args, **kwargs)

    @property
    def total_value(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def total_value_uzs(self):
        return (self.kg * self.price_uzs).quantize(Decimal("0.01"))

    @property
    def shipped_kg(self):
        return sum((sl.kg for sl in self.shipment_lines.all()), Decimal("0"))

    @property
    def remaining_kg(self):
        return self.kg - self.shipped_kg

    @property
    def truck_progress(self):
        """(sent, planned) for this product alone.

        Trucks, not rows: a yuk that carries this marka twice is still one truck.
        And a truck carrying TWO markalar counts under each of them — it really
        did carry both — so these can add past the kelishuv's own truck count,
        the same way Yuk holatlari counts a two-marka yuk under both."""
        sent = len({sl.shipment_id for sl in self.shipment_lines.all()})
        return sent, self.planned_trucks

    @property
    def trucks_paid_for(self):
        """(covered, sent) for THIS product alone — the per-marka twin of
        `Contract.trucks_paid_for`, and it follows the same rule: oldest truck
        first, in the kelishuv's own currency, the part of a truck the money
        reaches into counted as the part it is.

        Both sides are narrowed to this marka. The money is what named it — the
        kelishuv's unassigned to'lovlar bought nobody's trucks and cannot settle
        one here. The trucks are this marka's SHARE of each yuk: a truck carrying
        two markalar is covered here once this one's slice of it is paid, whatever
        is still owed on the other. That is why these need not add up to the
        kelishuv's own count, the same way the truck counts don't."""
        paid = own_side(self.contract, self.paid_total, self.paid_total_uzs)
        shares = {}
        for sl in self.shipment_lines.all():
            value = own_side(self.contract, sl.goods_value, sl.goods_value_uzs)
            shares[sl.shipment] = shares.get(sl.shipment, Decimal("0")) + value
        trucks = [((truck.sent is None, truck.sent, truck.pk), value)
                  for truck, value in shares.items()]
        return _trucks_covered(paid, trucks), len(trucks)

    @property
    def paid_total(self):
        """What has actually landed on THIS product.

        Read off the SLICES, not off the to'lovlar that named this marka
        (`SupplierPaymentAllocation`). A to'lov is not one product's: 7 000 sent
        against a marka owing 5 000 bought this one and the next, and counting the
        whole of it here would say a product was paid twice what it costs while its
        neighbour looked untouched.

        Credited, not gross — the bank's foiz rides on top of what the hamkor
        receives, so it was never theirs to be credited with.

        A to'lov that no product could take counts here for nobody: it is the
        hamkor's avans (`partner_advance_total`), and guessing which marka it will
        end up buying would be inventing a fact."""
        return sum((s.amount for s in self.supplier_allocations.all()), Decimal("0"))

    @property
    def paid_total_uzs(self):
        return sum((s.amount_uzs for s in self.supplier_allocations.all()),
                   Decimal("0"))

    @property
    def shipped_value(self):
        return sum((sl.goods_value for sl in self.shipment_lines.all()), Decimal("0"))

    @property
    def shipped_value_uzs(self):
        return sum((sl.goods_value_uzs for sl in self.shipment_lines.all()), Decimal("0"))

    @property
    def expected_value(self):
        """What this product will really cost: the trucks that went at the prices
        they actually went at, plus whatever is still to come at the agreed narx.
        The kelishuv's own total is only the estimate — a truck may be priced up
        or down against it."""
        left = self.remaining_kg if self.remaining_kg > 0 else Decimal("0")
        return (self.shipped_value + left * self.price).quantize(Decimal("0.01"))

    @property
    def expected_value_uzs(self):
        left = self.remaining_kg if self.remaining_kg > 0 else Decimal("0")
        return (self.shipped_value_uzs + left * self.price_uzs).quantize(Decimal("0.01"))

    @property
    def payable_left(self):
        """How much more will be paid on THIS product — what it will cost, less what
        has actually landed on it.

        A figure that only became answerable once a to'lov was placed per product
        (`SupplierPaymentAllocation`). Before that the money sat on the kelishuv and
        no product could say what it was still owed, which is why the Kelishuvlar
        list carried one combined figure beside a per-marka Qolgan kg column.

        These need not add up to the kelishuv's own `payable_left`: money no product
        could take is the hamkor's avans and is owed to nobody here."""
        return self.expected_value - self.paid_total

    @property
    def payable_left_uzs(self):
        return self.expected_value_uzs - self.paid_total_uzs

    @property
    def payable_left_own(self):
        return own_side(self.contract, self.payable_left, self.payable_left_uzs)

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


class SupplierPayment(CashEntry):
    """To'lov to one supplier contract. `amount` is the dollar value and `amount_uzs`
    the so'm one; `currency` says which of the two the operator actually typed.
    Overpaying a contract is blocked at the form layer (per-contract model, no
    supplier prepayments).

    The hamkor is not paid directly — a middleman passes the money on and keeps a
    percentage for the delivery. `amount` is what the hamkor RECEIVES (so it is what
    settles their qarz); the middleman's cut rides on top of it and leaves the kassa
    as an expense. Paying 10,000 at 2% therefore costs 10,200.

    The perechisleniya foiz is a second, unrelated charge on top of that — a bank
    fee, not the middleman's cut — so the two percentages stay separate fields."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="supplier_payments", verbose_name="Kelishuv")
    # WHICH product of the kelishuv the money is against. A kelishuv covering two
    # markalar is two deliveries sharing a piece of paper, and "paid $96 400 of
    # $288 000" said nothing about which of them the money went to.
    #
    # Nullable, and it stays that way: every to'lov entered before this field
    # existed names no product, and guessing one for them would be inventing a
    # fact. Blank therefore means "nobody has said yet", and those rows surface on
    # the dashboard as `unassigned_paid` until someone opens them and does.
    #
    # Nothing NEW can be left blank, though — the form refuses a to'lov on a
    # multi-product kelishuv that names no marka, and fills the marka in by itself
    # when the kelishuv has only one. Blank is a fact about the past, not a choice
    # on offer.
    #
    # The qarz is still settled per KELISHUV, not per product: the ceiling this
    # form checks against, `payable_left_own`, and everything partner_positions()
    # reports are unchanged. This field records where the money went; it does not
    # yet split what is owed.
    contract_line = models.ForeignKey(
        ContractLine, on_delete=models.PROTECT, null=True, blank=True,
        related_name="supplier_payments", verbose_name="Mahsulot")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    commission_percent = models.DecimalField(
        "Vositachi foizi (%)", max_digits=5, decimal_places=2, default=0, blank=True,
        help_text="Vositachisiz to'lov uchun bo'sh qoldiring")
    # Naqd, like every other kind of to'lov in the app. A form that opens on
    # perechisleniya also opens the bank foiz box beside it, so the commonest to'lov
    # was the one that had to be corrected in two places every time.
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
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

    #: Which kelishuv, which of its markalar, and when — see CashEntry.
    settlement_fields = ("contract", "contract_line", "date")

    @property
    def credited_amount(self):
        """What the hamkor is credited — the figure their qarz falls by.

        The vositachi cut always rides on top, so it never shortens what they get.
        The bank foiz is the choice: we can carry it, and they are credited the whole
        `amount`, or they can, and they are credited what actually reached them."""
        return self.amount if self.fee_on_company else self.amount - self.fee_amount

    @property
    def credited_amount_uzs(self):
        return (self.amount_uzs if self.fee_on_company
                else self.amount_uzs - self.fee_amount_uzs)

    @property
    def total_out(self):
        """What actually leaves the kassa: the hamkor's money and the middleman's
        cut, plus the bank's foiz when WE are the ones carrying it. Carried by the
        hamkor instead, the bank takes its cut out of the same `amount` we sent, so
        the kassa is out that figure and no more."""
        fee = self.fee_amount if self.fee_on_company else Decimal("0")
        return self.amount + self.commission_amount + fee

    @property
    def commission_amount_uzs(self):
        return self.in_som(self.commission_amount)

    @property
    def fee_amount_uzs(self):
        return self.in_som(self.fee_amount)

    @property
    def total_out_uzs(self):
        fee = self.fee_amount_uzs if self.fee_on_company else Decimal("0")
        return self.amount_uzs + self.commission_amount_uzs + fee

    @property
    def crosses_currency(self):
        """True when the money is not the money the kelishuv was struck in — the one
        case `SupplierPaymentForm` asks for a kurs at all. Paying a so'm kelishuv in
        so'm settles it at face value and converts nothing."""
        return self.currency != self.contract.currency

    def save(self, *args, **kwargs):
        """Place the money as well as record it.

        Same reasoning as `Sale.save` calling `sync_lot`: every caller that just
        creates or edits a to'lov gets it spread across the kelishuv's products
        without having to know the slice table exists. A row saved from a script, a
        fixture or a future screen is placed exactly like one typed into the modal —
        and a to'lov that sits on no product at all is a figure that shows up in the
        kelishuv's total while every marka under it reads zero.

        Only THIS to'lov is placed. Lowering one frees room the others could use, so
        an edit or a delete has to re-answer the whole kelishuv —
        `reconcile_supplier_allocations`, which the views call."""
        result = super().save(*args, **kwargs)
        allocate_supplier_payment(self)
        return result

    def __str__(self):
        return f"{self.contract_id} · {self.amount}$ ({self.date})"


class SupplierPaymentAllocation(models.Model):
    """One slice of a hamkor to'lov applied to one product of a kelishuv.

    The mirror of `PaymentAllocation` on the outgoing side, and it exists for the
    same reason: money and the thing it pays for are not one to one. A to'lov aimed
    at a marka owing 5 000 but sent as 7 000 has bought two things, and a single
    `contract_line` column on the row could only name one of them — so the extra
    2 000 either overpaid a product it did not buy or vanished from the per-marka
    picture entirely.

    `contract_line` on the to'lov stays: it is what the operator SAID the money was
    for, the point the spread starts from. These rows are where it actually landed.
    """

    payment = models.ForeignKey(SupplierPayment, on_delete=models.CASCADE,
                                related_name="allocations", verbose_name="To'lov")
    line = models.ForeignKey(ContractLine, on_delete=models.CASCADE,
                             related_name="supplier_allocations",
                             verbose_name="Mahsulot")
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    # Stored rather than sliced off the parent, for the reason PaymentAllocation
    # gives: both columns have a reader that must be exact, and a so'm kelishuv paid
    # in so'm has to land on zero rather than a tiyin away from it.
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def in_currency(self, currency):
        """This slice as read by one side of it — the kelishuv asks in the currency
        it was struck in, the to'lov in the one the money left in."""
        return self.amount_uzs if currency == Currency.UZS else self.amount

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Hamkor to'lovi taqsimoti"
        verbose_name_plural = "Hamkor to'lovi taqsimotlari"

    def __str__(self):
        return f"{self.payment_id} → {self.line_id}: {self.amount}$"


class LogistPayment(HeldFloat, CashEntry):
    """Money we send a logist so they can pay drivers.

    THIS is the kassa outflow, not the driver advance that follows it. The cash
    leaves us here; what the logist later hands a driver moves their balance and
    prices the yuk, but spending it again in the kassa would bill us twice for the
    same money.

    A logist's hisob is kept in dollars whatever they hand a driver, so a so'm
    top-up is the crossing and the kurs on it was really chosen."""

    #: See HeldFloat.float_currency — every driver advance is booked in dollars
    #: (ShipmentForm.sync_driver_advance), so the balance is a dollar figure.
    float_currency = Currency.USD

    #: Which logist was topped up, and when — see CashEntry.
    settlement_fields = ("logist", "date")

    logist = models.ForeignKey(Logist, on_delete=models.PROTECT,
                               related_name="payments", verbose_name="Logist")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="logist_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Logistga to'lov"
        verbose_name_plural = "Logistga to'lovlar"

    def __str__(self):
        return f"{self.logist_id} · {self.amount}$ ({self.date})"


class CustomsPayment(HeldFloat, CashEntry):
    """Money we send a bojxonachi so a load can be cleared.

    THIS is the kassa outflow, the same rule the logist pair follows: the cash
    leaves us here, and what they later hand over at bojxona prices the yuk without
    leaving the kassa a second time.

    `shipment` is what a LogistPayment has no use for and this one turns on. Customs
    money is sent FOR a truck — 40 mln so THIS one clears — so the row remembers
    which, and what was sent for a load can be set against what clearing it actually
    cost. Left blank it is a plain top-up: money added to the float with no load
    named yet, which is how a round figure sent ahead of the week gets recorded."""

    agent = models.ForeignKey(CustomsAgent, on_delete=models.PROTECT,
                              related_name="payments", verbose_name="Bojxonachi")
    #: Which bojxonachi, against which yuk, and when — see CashEntry.
    settlement_fields = ("agent", "shipment", "date")

    # PROTECT rather than CASCADE: deleting a yuk must not silently swallow money
    # that really left the kassa for it.
    shipment = models.ForeignKey("Shipment", on_delete=models.PROTECT, null=True,
                                 blank=True, related_name="customs_payments",
                                 verbose_name="Qaysi yuk uchun")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="customs_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Bojxonaga to'lov"
        verbose_name_plural = "Bojxonaga to'lovlar"

    @property
    def crosses_currency(self):
        """Never — and that is what makes this different from a logist top-up.

        A logist's hisob is one dollar heap, so a so'm top-up has to be converted
        INTO it and the kurs decides how much they end up holding. A bojxonachi
        holds two heaps (see CustomsAgent), so a so'm to'lov lands in the so'm heap
        and a dollar one in the dollar heap. Nothing is converted, so no kurs was
        chosen and there is none worth printing beside the row.

        The stored so'm twin still exists — every money row carries both columns —
        but it is a restatement for the kassa's converted total, not a figure this
        to'lov was settled at."""
        return False

    def __str__(self):
        own = self.amount_uzs if self.is_som else self.amount
        return f"{self.agent_id} · {own} {self.currency} ({self.date})"


def logist_positions():
    """(held, owed) across logistlar — each a [(currency, amount)] with both sides
    positive.

    Two separations, exactly as `customs_positions` does them. Held is kept apart from
    owed because a logist we have overfunded and one who fronted their own cash are
    two different facts, and one net number hides both. And each side is per-currency
    now that logistlar are funded in so'm as well as dollars: a so'm heap plus a
    dollar heap restated in so'm is not a total of anything.

    It used to branch on the dollar balance alone, which lost a whole heap — a logist
    square in dollars but short in so'm appeared in neither tile."""
    held, owed = [], []
    for logist in Logist.objects.prefetch_related("payments", "driver_advances"):
        held += logist.held_by_currency()
        owed += logist.owed_by_currency()
    return _by_currency(held), _by_currency(owed)


def customs_positions():
    """(held, owed) across bojxonachilar — each a [(currency, amount)] with both
    sides positive.

    Two separations, for two different reasons. Held is kept apart from owed for the
    same reason the hamkor and logist pairs are: money left over from an
    overestimated load and money a bojxonachi fronted himself are different facts,
    and one net number hides both. And each side is per-currency because bojxona
    money moves in two of them, and a so'm heap plus a dollar heap restated in so'm
    is not a total of anything."""
    held, owed = [], []
    for agent in CustomsAgent.objects.prefetch_related("payments", "expenses"):
        held += agent.held_by_currency()
        owed += agent.owed_by_currency()
    return _by_currency(held), _by_currency(owed)


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
    # A QR kod is handed to SOME drivers as they leave Eron; the ones carrying it
    # clear the road faster and land earlier, so which trucks have one is worth
    # seeing at a glance. Two dates, not a date and a flag: `qr_date` is the day it
    # is meant to be handed over (known at dispatch, a plan), `qr_given` the day it
    # actually was. The date is what says it happened — the same rule `arrived`
    # follows, and it keeps "planned for Friday" from reading as "done".
    qr_date = models.DateField(
        "QR kod beriladigan kun", null=True, blank=True,
        help_text="QR kod olgan haydovchi tezroq yetib keladi. Bu yukka berilmasa — "
                  "bo'sh qoldiring.")
    qr_given = models.DateField("QR kod berilgan sana", null=True, blank=True)
    transport = models.CharField("Transport raqami", max_length=50, blank=True)
    container = models.CharField("Konteyner raqami", max_length=50, blank=True)
    # Who on our side owns this load — free text rather than a user FK, since the
    # mas'ul shaxs is not always someone with an account (the prototype carried
    # them as a plain "Logist: <name>" note).
    responsible = models.CharField("Mas'ul shaxs", max_length=120, blank=True)
    # Who arranges the transport and pays this load's driver. Separate from
    # `responsible`, which is our own person: the logist is an outside party who
    # holds our money. Optional — plenty of loads move without one.
    logist = models.ForeignKey("Logist", on_delete=models.PROTECT, null=True,
                               blank=True, related_name="shipments",
                               verbose_name="Logist")
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
    def order_in_contract(self):
        """Which truck of its kelishuv this is — the 2 in "2-yuk".

        Counted, not stored: a yuk carries no number of its own, and the operator
        names them by the order they went out. Ordered by `sent` with the id behind
        it, so two trucks dispatched the same day still come out in a stable order
        and one not yet sent sorts last rather than first."""
        rows = list(Shipment.objects
                    .filter(contract_id=self.contract_id)
                    .values_list("pk", "sent"))
        rows.sort(key=lambda r: (r[1] is None, r[1], r[0]))
        return [pk for pk, _ in rows].index(self.pk) + 1

    @property
    def label(self):
        """How a yuk is named out loud: "vazifadon-3 · 2-yuk". A bare "#17" says
        nothing about whose truck it was — which is the only thing that helps when
        the question is why a sotuv sits on this lot rather than that one."""
        return f"{self.contract.code} · {self.order_in_contract}-yuk"

    @property
    def has_qr(self):
        """Whether this load's driver is carrying a QR kod. Green in the yuklar
        table; every load without one is yellow, since "no QR" is itself the fact
        worth reading — that truck takes the slow road."""
        return self.qr_given is not None

    @property
    def qr_overdue(self):
        """The day the kod was meant to be handed over has passed and it still has
        not been.

        `qr_date` is a plan and nothing enforces it, so this is the gap between what
        was promised for a load and what happened to it. Worth saying out loud
        because the row is otherwise indistinguishable from a truck that was never
        meant to get a kod at all: both are simply "berilmagan", while only this one
        means someone expected a kod today and the driver is still waiting on the
        slow road. Loads with no `qr_date` are not late — nothing was planned."""
        return (self.qr_given is None and self.qr_date is not None
                and self.qr_date < timezone.localdate())

    @property
    def qr_days_late(self):
        return (timezone.localdate() - self.qr_date).days if self.qr_overdue else 0

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
    def goods_value_uzs(self):
        return sum((ln.goods_value_uzs for ln in self.lines.all()), Decimal("0"))

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
        # Summed in Python, not with aggregate(): every screen that reaches this
        # prefetches `expenses`, and aggregate() ignores a prefetch cache — it fires
        # a fresh query per yuk. Landed cost reads it once per lot, so on Ombor and
        # the kassa that was one query per row for a figure already in memory.
        return sum((e.amount for e in self.expenses.all()), Decimal("0"))

    @property
    def expenses_total_uzs(self):
        return sum((e.amount_uzs for e in self.expenses.all()), Decimal("0"))

    @property
    def expense_per_kg(self):
        """Road/customs spend spread evenly over every kg on the truck, whichever
        product it belongs to. Transport and customs are charged for the load, not
        per brand, so kg is the honest split — a cheap brand and an expensive one
        riding together carry the same share of the freight."""
        total_kg = self.kg
        return self.expenses_total / total_kg if total_kg else Decimal("0")

    # ── Bojxona: what was sent for this load against what it really cost ──────────
    #
    # The estimate goes out first — ~40 mln so this truck clears — and the true
    # figure only lands afterwards. These three read that gap off the rows already
    # being stored: the to'lovlar naming this yuk, against the xarajatlar the
    # bojxonachi paid on it. Nothing new is entered to make the comparison work,
    # which is what keeps it true rather than a second set of numbers to maintain.

    def customs_sent_by_currency(self):
        """[(currency, yuborilgan)] to a bojxonachi FOR THIS LOAD.

        `net_amount`, not `amount`: a bank foiz never reached them, so it was never
        money that could clear anything. Bucketed by the currency each to'lov was
        made in, the same rule every other total in the app follows — 40 mln so'm
        and $500 sent for one truck are two figures, and the kurs that would join
        them is not one either side agreed to."""
        return _by_currency(
            (p.currency, own_side(p, p.net_amount, p.net_amount_uzs))
            for p in self.customs_payments.all())

    def customs_spent_by_currency(self):
        """[(currency, sarflangan)] — what clearing it actually cost out of that
        money.

        Only the rows a bojxonachi paid. A bojxona xarajat settled straight from the
        kassa is a real cost of the yuk, but it is not money out of this float, and
        counting it here would read as an overspend that never happened."""
        return _by_currency(
            (e.currency, own_side(e, e.amount, e.amount_uzs))
            for e in self.expenses.all() if e.customs_agent_id)

    def customs_diff_by_currency(self):
        """[(currency, farq)] — positive: sent for this load and not spent on it,
        still with the bojxonachi and funding the next truck. Negative: clearing
        cost more than we sent and they covered the rest.

        THE figure this feature exists for. Per currency because that is the only
        way it can be checked against anything: an operator asking "we sent 40, what
        came back?" is holding a so'm number, and an answer part-derived from a
        dollar row at some day's kurs is not one they can verify."""
        entries = list(self.customs_sent_by_currency())
        entries += [(currency, -amount)
                    for currency, amount in self.customs_spent_by_currency()]
        return _by_currency(entries)

    @property
    def customs_is_open(self):
        """Still unaccounted for: money was involved and some heap does not balance.

        A yuk nobody has sent customs money for is not "settled", it is simply not
        part of this ledger — so it answers False rather than True and stays off the
        reconciliation screen entirely."""
        if not self.customs_sent_by_currency() and not self.customs_spent_by_currency():
            return False
        return bool(self.customs_diff_by_currency())

    @property
    def is_lot(self):
        return self.arrived is not None

    @property
    def sold_kg(self):
        return sum((ln.sold_kg for ln in self.lines.all()), Decimal("0"))

    @property
    def available_kg(self):
        return sum((ln.available_kg for ln in self.lines.all()), Decimal("0"))

    @property
    def brand_summary(self):
        """Every product on the truck, named in full."""
        return ", ".join(ln.brand for ln in self.lines.all())

    def __str__(self):
        return f"Yuk #{self.pk} · {self.brand_summary} · {self.kg} kg"


class ShipmentLine(MoneyEntry):
    """One product on one truck, and the unit the ombor actually deals in: a lot is
    a ShipmentLine of an arrived Shipment, so sotuv and bron attach here rather than
    to the truck. A truck carrying two brands is therefore two lots."""

    money_fields = ("price", "price_uzs")

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="lines", verbose_name="Yuk")
    contract_line = models.ForeignKey(ContractLine, on_delete=models.PROTECT,
                                      related_name="shipment_lines",
                                      verbose_name="Mahsulot")
    kg = models.DecimalField("Yuborilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4,
                                null=True, blank=True,
                                help_text="Bo'sh qoldirilsa kelishuv narxi olinadi")
    price_uzs = models.DecimalField("1 kg narxi (so'm)", max_digits=18,
                                    decimal_places=2, null=True, blank=True)
    position = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Yuk mahsuloti"
        verbose_name_plural = "Yuk mahsulotlari"

    def save(self, *args, **kwargs):
        """Backstop, same rule as the kelishuv line: a truck's narx is quoted in the
        kelishuv's currency. A truck priced in the other one would land in
        `shipped_value_own` as a figure the qarz was never measured in."""
        if self.contract_line_id:
            self.currency = self.contract_line.contract.currency
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = list(dict.fromkeys(
                    list(kwargs["update_fields"]) + ["currency"]))
        return super().save(*args, **kwargs)

    @property
    def brand(self):
        return self.contract_line.brand

    @property
    def label(self):
        """The lot named the way the ombor talks about it — "vazifadon-3 · 2-yuk"
        — rather than by its row id."""
        return self.shipment.label

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
    def unit_price_uzs(self):
        """The so'm twin of unit_price, falling back the same way so an unpriced
        truck line reports the kelishuv's so'm narx rather than nothing."""
        if self.price is not None:
            return self.price_uzs or Decimal("0")
        return self.contract_line.price_uzs

    @property
    def unit_currency(self):
        """Which currency `unit_price` should be READ in — this line's own when it
        set a price, else the kelishuv's, following the same fallback as the figure
        itself. Without it a truck line inheriting a so'm kelishuv narx would print
        that narx with a dollar sign in front of it."""
        if self.price is not None:
            return self.currency
        return self.contract_line.currency

    @property
    def goods_value(self):
        return (self.kg * self.unit_price).quantize(Decimal("0.01"))

    @property
    def goods_value_uzs(self):
        return (self.kg * self.unit_price_uzs).quantize(Decimal("0.01"))

    @property
    def landed_cost_per_kg(self):
        """True cost of one kg of this product in this load: its unit price, the
        truck's freight share, and the kelishuv's vositachi cut per kg. Fully live —
        add a freight expense or pay the hamkor and every load re-prices at once,
        including stock already sold (profit is computed off this, not a snapshot).

        The one place currencies are DELIBERATELY blended. A qarz is measured only in
        the currency it was agreed in (see `Contract._own`), but a kg has one cost and
        the money behind it arrives in both at once — mol in dollars, transport in
        so'm. Each part is folded in at its own entry-day kurs, never today's, so the
        figure still cannot drift. Pinned by tests/test_cost_blends_currencies.py."""
        return (self.unit_price + self.shipment.expense_per_kg
                + self.contract_line.contract.commission_per_kg).quantize(Decimal("0.0001"))

    @property
    def landed_cost_per_kg_uzs(self):
        """Tannarx in so'm at this lot's own kurs. Freight and the vositachi cut are
        blended in from other rows booked at their own kursi, so this is the lot's
        cost restated rather than a sum of stored so'm columns — the one place the
        two currencies cannot be kept independently exact."""
        return self.in_som(self.landed_cost_per_kg)

    @property
    def sold_kg(self):
        """Read off the slices rather than off `sales`: a sotuv that reached across
        a lot boundary leaves only PART of its kg here, and counting the whole sotuv
        against its first lot would empty that lot twice over."""
        return sum((sl.kg for sl in self.sale_lots.all()), Decimal("0"))

    @property
    def returned_kg(self):
        """kg flowed back into this lot by restocked returns.

        A qaytarish is recorded against the sotuv, not against a lot, so when the
        sotuv spans two lots the kg go back in the proportion they came out. Exact
        whenever a sotuv sits on one lot, which is the ordinary case."""
        total = Decimal("0")
        for sl in self.sale_lots.all():
            sale = sl.sale
            restocked = sum((r.kg for r in sale.returns.all() if r.restock),
                            Decimal("0"))
            if restocked and sale.kg:
                total += restocked * sl.kg / sale.kg
        return total

    @property
    def available_kg(self):
        """PHYSICAL kg left on this lot: what arrived, minus what has been sold,
        plus what came back.

        Brons are deliberately absent, and not only because a bron is a claim on a
        MARKA rather than on one truck: a bron holds nothing back at all. It is a
        note of who asked first, not a lock on stock — see `bron_queue`."""
        return self.kg - self.sold_kg + self.returned_kg

    def __str__(self):
        return f"Lot #{self.pk} · {self.brand} · {self.kg} kg"


class Reservation(MoneyEntry):
    """Bron: a mijoz reserves kg of a MARKA, not of one lot.

    The claim is on the product across every kelishuv: whichever truck lands first
    with that marka fills the bron, whoever it came from. That is why there is no
    lot here — pinning a bron to a lot would mean the mijoz waits for one specific
    truck while the same granula sits in the ombor from another kelishuv.

    A bron reserves nothing physically. It does not hold kg back from an ordinary
    sotuv and it does not block another bron: the granula goes to whoever the
    operator hands it to. The `created_at` order is shown as a queue position so it
    is visible who asked first, but it is a note, not a rule — the operator decides.

    Handing over is partial in both directions: a 20 000 kg bron against a 12 000 kg
    arrival takes the 12 000 now, and the operator may also give less than that on
    purpose. Either way the bron stays open for the rest.

    The agreed narx carries its currency into the sotuv, so a bron struck in so'm
    becomes a so'm sotuv rather than silently turning into dollars."""

    money_fields = ("price", "price_uzs")

    class Status(models.TextChoices):
        ACTIVE = "active", "Faol"
        CONVERTED = "converted", "Sotuvga aylandi"
        # Served in part and closed by agreement: the mijoz took what they took and
        # does not want the rest. Distinct from CANCELLED, which is a bron that
        # never happened — the difference matters when reading back why a promise
        # ended, and CONVERTED would claim the whole of it turned into a sotuv.
        CLOSED = "closed", "Tugatildi"
        CANCELLED = "cancelled", "Bekor qilindi"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="reservations", verbose_name="Mijoz")
    brand = models.CharField("Marka", max_length=120, db_index=True)
    kg = models.DecimalField("Bron qilingan kg", max_digits=12, decimal_places=3)
    fulfilled_kg = models.DecimalField(
        "Berilgan kg", max_digits=12, decimal_places=3, default=0,
        help_text="Sotuvga aylantirilgan qismi — qolgani navbatda turadi")
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4,
                                null=True, blank=True)
    price_uzs = models.DecimalField("1 kg narxi (so'm)", max_digits=18,
                                    decimal_places=2, null=True, blank=True)
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

    @property
    def remaining_kg(self):
        """Still owed to this mijoz. A partly filled bron stays in the queue for the
        rest rather than closing, so this — not `kg` — is what the next hand-over
        draws against and what the ombor reports as promised."""
        return self.kg - self.fulfilled_kg

    @property
    def is_open(self):
        return self.status == self.Status.ACTIVE and self.remaining_kg > 0

    @property
    def price_own(self):
        """The agreed narx in the currency it was agreed in — what the sotuv form
        has to be handed when this bron is served, because the narx box there is
        read as whichever currency the Valyuta picker says.

        None while the narx is still open, same as `total`."""
        if self.price is None:
            return None
        return own_side(self, self.price, self.price_uzs)

    @property
    def total(self):
        """What the bron is worth at the agreed narx — None while the narx is still
        open, which is a real state here: a bron may be struck before the price is.
        None rather than 0 so the screen can say "kelishilmagan" instead of showing
        a free reservation."""
        if self.price is None:
            return None
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def total_uzs(self):
        if self.price_uzs is None:
            return None
        return (self.kg * self.price_uzs).quantize(Decimal("0.01"))

    def __str__(self):
        return f"Bron #{self.pk} · {self.customer} · {self.brand} · {self.kg} kg"


# ── Pozitsiya: what the cash figure means ────────────────────────────────────────
#
# The kassa on its own answers "how much money moved", which is not the same as
# "how are we doing". These four put the cash figure in context: money we are
# holding but have not earned, money owed to us, goods we own, and the two sides
# of what we owe hamkorlar. Each returns a (dollar, so'm) pair, and each so'm
# figure is summed from rows booked at their OWN kurs rather than re-rated today —
# same rule as everything else that shows both currencies.


def _by_currency(entries):
    """[(currency, total)] from (currency, amount) pairs — dollars first, zeros dropped.

    The shape every per-currency figure takes, so a hamkor row, a mijoz row and a
    kassa plitka all read the same way round. A side that nets to zero is left out
    rather than printed: "0 so'm" beside a real dollar figure reads as a second,
    empty debt, and a business that has never taken a so'm has no so'm side."""
    totals = {}
    for currency, amount in entries:
        totals[currency] = totals.get(currency, Decimal("0")) + amount
    return [(currency, totals[currency]) for currency in (Currency.USD, Currency.UZS)
            if totals.get(currency)]


def customer_balance_by_currency(customer):
    """[(currency, balance)] for one mijoz — positive is qarz, negative is avans.

    Split the way `payable_by_currency` splits a hamkor, and for the same reason: a
    sotuv agreed in so'm is owed in so'm, a to'lov that arrived in dollars sits as a
    dollar avans until it is put on a sotuv, and netting the two needs a kurs neither
    was struck at. A mijoz who deals in both reads as two figures side by side.

    Only non-zero sides are returned, so the common single-currency mijoz still reads
    as one figure."""
    entries = [(sale.currency, sale.remaining_own) for sale in customer.sales.all()]
    entries += [(payment.currency, -unspent_payment_amount(payment))
                for payment in customer.customer_payments.all()]
    # No vazvrat term here, deliberately. This figure is built from what each sotuv
    # still owes and what each to'lov has left UNSPENT, and a cash refund has
    # already come off that pool as a `RefundAllocation`. Adding it again would
    # count one refund twice. Its gross twin `Customer.balance` does need the term,
    # because that one is built from totals the pool never touches.
    return _by_currency(entries)


def pending_refunds():
    """Vazvrat money promised to a mijoz and not yet handed over — our qarz to them.

    Deliberately NOT a kassa chiqim: the till still physically holds it. But it is
    spoken for, and "we have it" and "we may spend it" are different facts — which is
    why the kassa prints it beside the cash figure rather than inside it. The same
    list is what puts a mijoz on Qarzlar as the side WE owe."""
    return list(ReturnSettlement.objects
                .filter(route=ReturnSettlement.Route.CASH, paid_date__isnull=True)
                .select_related("batch__customer")
                .order_by("due_date", "pk"))


def pending_refunds_by_currency(rows=None):
    """[(currency, summa)] still owed to mijozlar, each heap in its own money."""
    if rows is None:
        rows = pending_refunds()
    return _by_currency((s.currency, s.amount_own) for s in rows)


def customer_advance_total():
    """Money mijozlar have handed over that sits on no sotuv yet.

    It IS in the kassa — the cash arrived — but it is not ours: cancel the bron or
    the order and it goes back. Showing it beside the till figure is the difference
    between "we have this" and "we are holding this"."""
    total = total_uzs = Decimal("0")
    for payment in CustomerPayment.objects.prefetch_related("allocations"):
        # Whether there IS an avans is asked in the currency the money arrived in —
        # that is the pot being drawn down. What it is worth is then read off both
        # columns, because the till holds cash in both at once.
        if unspent_payment_amount(payment) > 0:
            usd, uzs = unspent_payment_pair(payment)
            total += usd
            total_uzs += uzs
    return total, total_uzs


def customer_receivable_total():
    """What mijozlar still owe us — positive balances only, and how many of them.

    A mijoz sitting in avans does not net off another mijoz's qarz: the money is
    not fungible across customers, and treating it as one figure would understate
    both what is owed and what is held."""
    total = total_uzs = Decimal("0")
    count = 0
    customers = Customer.objects.prefetch_related(
        "sales__returns", "sales__allocations", "customer_payments__allocations")
    for customer in customers:
        if customer.balance > 0:
            total += customer.balance
            total_uzs += customer.balance_uzs
            count += 1
    return total, total_uzs, count


def kassa_row_sets():
    """Every row that has moved the till, read out of the database ONCE.

    The kassa asks the same rows three different questions on every load — the heaps
    per currency, the same heaps split by usul, and the converted pair the page still
    carries — and each used to walk the five money tables itself. Fifteen scans for
    one screen, all of them returning the same rows.

    Handed round as plain lists rather than querysets on purpose: a queryset narrowed
    again (`.filter(method=…)`) goes back to the database, which is exactly the trip
    being avoided, so a caller that wants a slice takes it in Python."""
    return {
        "incoming": list(CustomerPayment.objects.all()),
        # Signed by direction, so it joins the inflow side whichever way it went — a
        # ta'sischi taking money out is a negative kirim, not a fifth kind of chiqim.
        "kapital": list(Kapital.objects.all()),
        # `select_related("shipment")` on the xarajatlar: `total_out` now asks each
        # one whether its truck has landed (see `is_pending`), which without it is a
        # query per expense on every screen that counts the till.
        # A vazvrat refund counts from the day the money LEFT, so only the settled
        # ones are here: a payout we have promised but not made has not moved the
        # till, and putting it here would spend it twice — once when promised and
        # again when handed over. The unpaid ones surface as `pending_refunds`.
        "outgoing": [*SupplierPayment.objects.all(),
                     *ShipmentExpense.objects.select_related("shipment"),
                     *LogistPayment.objects.all(), *CustomsPayment.objects.all(),
                     *OtherExpense.objects.all(),
                     *ReturnSettlement.objects.filter(
                         route=ReturnSettlement.Route.CASH,
                         paid_date__isnull=False).select_related("batch__customer")],
        # Its own side, deliberately neither of the other two: a konvertatsiya is not
        # money arriving or leaving, so nothing that counts kirim or chiqim may pick
        # it up. It only ever moves one heap into another.
        "exchange": list(Konvertatsiya.objects.all()),
    }


def kassa_cash_by_currency(rows=None):
    """What is physically in the till, each pile in its OWN currency.

    Kirim minus chiqim, counted on one side only: a to'lov that arrived in so'm put
    so'm in the till and no dollars at all. The pair this sits beside sums BOTH
    stored columns of every row, so each dollar row also contributes its converted
    so'm twin — a till holding 87.8 mln real so'm reported 9 313 mln, the dollar
    side counted a second time in so'm clothing.

    Unlike a qarz, the two piles are not two obligations — they are two heaps of
    cash, and nothing stops them being exchanged. What is refused is doing it
    silently, at a kurs nobody chose, in the one figure the operator checks against
    what is actually in the safe.

    `rows` is `kassa_row_sets()` already loaded by a caller that needs the same rows
    for something else; left out, this reads them itself."""
    if rows is None:
        rows = kassa_row_sets()
    entries = []
    for payment in rows["incoming"]:
        entries.append((payment.currency,
                        own_side(payment, payment.net_amount, payment.net_amount_uzs)))
    for entry in rows["kapital"]:
        entries.append((entry.currency,
                        own_side(entry, entry.signed_amount, entry.signed_amount_uzs)))
    for row in rows["outgoing"]:
        entries.append((row.currency,
                        -own_side(row, row.total_out, row.total_out_uzs)))
    # One heap emptying into another: a minus and a plus that cancel in a total and
    # move the piles apart, which is the whole point of recording it. Dollars sold
    # for so'm leave the dollar heap in full and land in the so'm one in full — the
    # kurs is already spent in the two figures and is never applied again here.
    for row in rows.get("exchange", ()):
        entries.append((row.from_currency, -row.from_amount))
        entries.append((row.to_currency, row.to_amount))
    return _by_currency(entries)


def kassa_cash_by_method(rows=None):
    """The till split by WHERE it is held: naqd, kartada, bank o'tkazmasida — each in
    its own currency.

    "Kassada" as one figure answers "how much have we got"; it does not answer "how
    much of it can I hand over right now", and those are different questions with
    different answers. Cash in the safe, money on a card and a bank balance are three
    separate heaps: the first is spendable this minute, the last can take a day and
    carries a foiz on the way out.

    Same arithmetic as `kassa_cash_by_currency` (kirim minus chiqim, each row counted
    on its own side only) — this only keeps the method a row moved by. Every method is
    returned even when empty, because an operator checking the safe against the screen
    needs to see the zero rather than wonder where the line went.

    Takes the same already-loaded `rows` as its neighbour above, so the pair of them
    is one pass over the money tables rather than two."""
    if rows is None:
        rows = kassa_row_sets()
    totals = {}

    def add(method, currency, amount):
        totals.setdefault(method, []).append((currency, amount))

    for payment in rows["incoming"]:
        add(payment.method, payment.currency,
            own_side(payment, payment.net_amount, payment.net_amount_uzs))
    # Already signed by direction, so it joins the inflow side whichever way it went.
    for entry in rows["kapital"]:
        add(entry.method, entry.currency,
            own_side(entry, entry.signed_amount, entry.signed_amount_uzs))
    for row in rows["outgoing"]:
        add(row.method, row.currency,
            -own_side(row, row.total_out, row.total_out_uzs))
    # The row this split exists for. Naqd dollar sold for so'm on a karta is the one
    # movement that changes NOTHING about how much the business has and everything
    # about which of these three cards it is sitting on — counted on both ends here,
    # and on neither in any kirim/chiqim figure.
    for row in rows.get("exchange", ()):
        add(row.from_method, row.from_currency, -row.from_amount)
        add(row.to_method, row.to_currency, row.to_amount)

    rows = []
    for code, label in PayMethod.choices:
        split = _by_currency(totals.get(code, []))
        held = dict(split)
        rows.append({
            "code": code, "label": label, "split": split,
            # Both currencies always drawn, an empty side spelled out as a zero: the
            # figure is checked against what is actually in the safe, and a missing
            # line reads as "not counted" rather than as "none".
            "split_full": [(currency, held.get(currency, Decimal("0")))
                           for currency in (Currency.USD, Currency.UZS)],
        })
    return rows


def kapital_total_by_currency():
    """[(currency, sof kapital)] — what the ta'sischi has put in less what they have
    taken out, each side in the money it actually moved in.

    Read as a meta line under Kassada rather than as a tile of its own: kapital is
    not a place money is sitting, it is where some of the money in the till came
    from, and the tiles answer the first question, not the second."""
    return _by_currency(
        (entry.currency, own_side(entry, entry.signed_amount, entry.signed_amount_uzs))
        for entry in Kapital.objects.all())


def customer_advance_by_currency():
    """Mijoz money in the till that sits on no sotuv yet, per currency.

    The side the money arrived in is the side it goes back out in when a bron or an
    order is cancelled, so that is the side an avans is held in."""
    return _by_currency(
        (payment.currency, unspent_payment_amount(payment))
        for payment in CustomerPayment.objects.prefetch_related("allocations"))


def customer_receivable_by_currency():
    """([(currency, qarz)], how many mijoz) — what customers still owe, per currency.

    Only positive sides count, for the same reason `customer_receivable_total` takes
    only positive balances: a mijoz sitting in avans does not pay another mijoz's
    qarz. The count is of people, not of sides — somebody who owes in both currencies
    is one debtor, not two."""
    entries = []
    debtors = 0
    customers = Customer.objects.prefetch_related(
        "sales__returns", "sales__allocations", "customer_payments__allocations")
    for customer in customers:
        owed = [(currency, amount)
                for currency, amount in customer_balance_by_currency(customer)
                if amount > 0]
        if owed:
            entries += owed
            debtors += 1
    return _by_currency(entries), debtors


def payable_by_currency(contracts):
    """[(currency, qolgan to'lov)] over a set of kelishuvlar, each in its OWN currency.

    Never summed across currencies. A hamkor owed dollars on one kelishuv and so'm on
    another is owed two different debts; adding them needs a kurs neither side agreed
    on, and the total would move on its own as the market did. So the row carries both
    figures side by side and the reader is told which is which.

    Ordered dollars-first so a hamkor's row reads the same way every time rather than
    following whichever kelishuv happened to be created first."""
    totals = {}
    for contract in contracts:
        left = contract.payable_left_own
        if left > 0:
            totals[contract.currency] = totals.get(contract.currency, Decimal("0")) + left
    return [(currency, totals[currency])
            for currency in (Currency.USD, Currency.UZS) if currency in totals]


def partner_positions():
    """Both directions of the hamkor relationship, kept apart.

    A kelishuv paid beyond the goods sent is an avans WE are owed, not a smaller
    qarz — netting the two into one number hid $203 030.5 of prepayment behind a
    $50 480 payable at the end of July. Returns (owed, owed_uzs, prepaid,
    prepaid_uzs), both sides positive."""
    owed = owed_uzs = prepaid = prepaid_uzs = Decimal("0")
    owed_partners, prepaid_contracts = set(), 0
    contracts = Contract.objects.select_related("partner").prefetch_related(
        "lines__shipment_lines", "supplier_payments")
    for contract in contracts:
        debt, debt_uzs = contract.debt, contract.debt_uzs
        if debt > 0:
            owed += debt
            owed_uzs += debt_uzs
            owed_partners.add(contract.partner_id)
        elif debt < 0:
            prepaid -= debt
            prepaid_uzs -= debt_uzs
            prepaid_contracts += 1
    return {"owed": owed, "owed_uzs": owed_uzs, "partners": len(owed_partners),
            "prepaid": prepaid, "prepaid_uzs": prepaid_uzs,
            "contracts": prepaid_contracts}


def contract_value_by_currency(contracts):
    """[(currency, jami)] over a set of kelishuvlar, each at the value it was agreed
    at and in the money it was agreed in.

    The companion to `payable_by_currency` at the head of a report: one says what the
    whole business with a hamkor is worth, the other what is still owed on it. Both
    refuse to add across currencies, for the same reason."""
    return _by_currency((c.currency, c.total_value_own) for c in contracts)


def supplier_paid_by_currency(payments):
    """[(currency, hamkorga to'langan)] over a set of hamkor to'lovlari.

    Read in the currency each to'lov was MADE in — the same bucketing the To'lovlar
    list uses, so the report header and the rows behind it cannot disagree."""
    return _by_currency(
        (payment.currency, own_side(payment, payment.amount, payment.amount_uzs))
        for payment in payments)


def customer_sales_by_currency(sales):
    """[(currency, sotildi)] — turnover in the currency each sotuv was agreed in."""
    return _by_currency((sale.currency, sale.net_total_own) for sale in sales)


def customer_paid_by_currency(payments):
    """[(currency, to'landi)] — what mijozlar handed over, in the currency it
    arrived in. `net_amount`, so a bank foiz that never reached us is not counted
    as money received."""
    return _by_currency(
        (payment.currency, own_side(payment, payment.net_amount, payment.net_amount_uzs))
        for payment in payments)


def partner_positions_by_currency():
    """Both sides of the hamkor relationship, per currency and still kept apart.

    Same split as `partner_positions`, read in the currency the kelishuv was struck
    in: a kelishuv is agreed in one currency and what is owed on it is owed in that
    one. The counts are of hamkorlar and kelishuvlar, not of currency sides."""
    owed, prepaid = [], []
    owed_partners, prepaid_contracts = set(), 0
    contracts = Contract.objects.select_related("partner").prefetch_related(
        "lines__shipment_lines", "supplier_payments")
    for contract in contracts:
        debt = contract.debt_own
        if debt > 0:
            owed.append((contract.currency, debt))
            owed_partners.add(contract.partner_id)
        elif debt < 0:
            prepaid.append((contract.currency, -debt))
            prepaid_contracts += 1
    return {"owed": _by_currency(owed), "partners": len(owed_partners),
            "prepaid": _by_currency(prepaid), "contracts": prepaid_contracts}


#: What `stock_value` and `stock_value_by_currency` both need loaded before they ask
#: a lot what it cost — landed cost reaches into the truck's expenses and the
#: kelishuv's to'lovlar, so without this it is a handful of queries per lot.
STOCK_COST_PREFETCH = (
    # Through the slices, not through `sales`: a sotuv that reached across a lot
    # boundary leaves only part of its kg here, which is what `ShipmentLine.sold_kg`
    # and `returned_kg` now read.
    "sale_lots__sale__returns",
    # Freight per kg divides the truck's xarajatlar by the truck's WHOLE kg, so the
    # other products riding with it are part of the sum (`Shipment.expense_per_kg`).
    "shipment__expenses", "shipment__lines",
    # The vositachi cut per kg divides what the kelishuv has accrued by its whole
    # agreed kg (`Contract.commission_per_kg`).
    "contract_line__contract__supplier_payments",
    "contract_line__contract__lines",
)

#: The hamkor behind a lot, reached BOTH ways, for screens that name them.
#: `arrived_lots` joins as far as the truck's kelishuv but stops before its partner,
#: and a lot's own kelishuv hangs off `contract_line` instead — each one left out is
#: a query per row.
STOCK_LOT_RELATED = ("shipment__contract__partner", "contract_line__contract__partner")


def stock_value():
    """Granula sitting in the ombor, at its landed cost — goods we paid for and
    still own. Costed rather than priced: what it will sell for is not knowable
    yet, what it cost us is.

    The converted pair, for the screens that need one figure per kg however the
    money arrived (reports, profit). The kassa board reads
    `stock_value_by_currency` instead."""
    total = total_uzs = kg = Decimal("0")
    for lot in arrived_lots().prefetch_related(*STOCK_COST_PREFETCH):
        left = lot.kg - lot.sold_kg + lot.returned_kg
        if left > 0:
            kg += left
            total += (left * lot.landed_cost_per_kg).quantize(Decimal("0.01"))
            total_uzs += (left * lot.landed_cost_per_kg_uzs).quantize(Decimal("0.01"))
    return total, total_uzs, kg


def stock_value_by_currency():
    """([(currency, value)], kg) — the same stock, each lot counted in the currency
    its KELISHUV was agreed in.

    Split the way transit is: the mol on a so'm kelishuv was bought in so'm, and
    restating it in dollars publishes a figure nobody agreed to. As one blended dollar
    total the ombor was the last place on the kassa that did that.

    What still cannot be split is INSIDE a lot. A so'm transport bill on a dollar
    kelishuv folds into the same tannarx at its own entry-day kurs, because a kg has
    exactly one cost — a sotuv is priced against it and the profit is computed from it,
    and "1.3 $ + 200 so'm per kg" is not a price anybody can sell at. So a lot lands
    wholly on its kelishuv's side, carrying that blended share with it."""
    entries = []
    kg = Decimal("0")
    for lot in arrived_lots().prefetch_related(*STOCK_COST_PREFETCH):
        left = lot.kg - lot.sold_kg + lot.returned_kg
        if left > 0:
            kg += left
            entries.append((lot.currency, own_side(
                lot,
                (left * lot.landed_cost_per_kg).quantize(Decimal("0.01")),
                (left * lot.landed_cost_per_kg_uzs).quantize(Decimal("0.01")))))
    return _by_currency(entries), kg


def transit_value():
    """Goods already sent by the hamkor but not yet arrived, at the agreed narx.

    At the kelishuv price, not landed cost: the freight and bojxona on a truck
    still moving have mostly not been paid, so a landed figure here would be a
    guess dressed as a cost."""
    total = total_uzs = kg = Decimal("0")
    loads = set()
    lines = (ShipmentLine.objects
             .filter(shipment__arrived__isnull=True)
             .select_related("shipment", "contract_line"))
    for line in lines:
        total += line.goods_value
        total_uzs += line.goods_value_uzs
        kg += line.kg
        loads.add(line.shipment_id)
    return total, total_uzs, kg, len(loads)


def transit_value_by_currency():
    """([(currency, value)], kg, loads) — goods sent and not yet arrived, per currency.

    Valued at the agreed kelishuv narx, and a kelishuv has exactly one currency, so
    this side of the board splits cleanly. The ombor beside it splits by the same
    rule (`stock_value_by_currency`) with one caveat it cannot avoid: a landed cost
    mixes a dollar mol with a so'm transport bill on purpose (see
    ShipmentLine.landed_cost_per_kg), so a lot carries that blended share onto its
    kelishuv's side."""
    entries = []
    kg = Decimal("0")
    loads = set()
    lines = (ShipmentLine.objects
             .filter(shipment__arrived__isnull=True)
             .select_related("shipment", "contract_line"))
    for line in lines:
        entries.append((line.currency,
                        own_side(line, line.goods_value, line.goods_value_uzs)))
        kg += line.kg
        loads.add(line.shipment_id)
    return _by_currency(entries), kg, len(loads)


def arrived_lots():
    """Every lot in the ombor: the product lines of arrived trucks."""
    return (ShipmentLine.objects
            .filter(shipment__arrived__isnull=False)
            .select_related("contract_line", "shipment", "shipment__contract"))


def bron_queue(brand=None):
    """Open brons, oldest first — who asked for this marka, in the order they asked.

    The order is shown, not enforced. Nothing here stops the second bron being
    filled before the first, or an ordinary sotuv taking the same kg: it exists so
    the operator can SEE who booked first and decide, which is how the hand-over is
    actually agreed on the phone.

    One list, not one per marka, so the caller can see the whole board; pass a
    marka to narrow it. Ordering is `created_at` then pk: two brons taken in the
    same second still have a defined order, and it is the one entered first."""
    qs = (Reservation.objects
          .filter(status=Reservation.Status.ACTIVE)
          .select_related("customer")
          .order_by("created_at", "pk"))
    if brand is not None:
        qs = qs.filter(brand=brand)
    return [r for r in qs if r.remaining_kg > 0]


def brand_reserved_kg(brand):
    """Kg of this marka already promised to somebody. Counted on `remaining_kg`, so
    a bron half filled from an earlier truck only reports the half still owed.

    Reported, never enforced: this figure tells the operator who is waiting on the
    granula, and nothing more. It is not subtracted from what a sotuv may take —
    see `brand_on_hand_kg`."""
    return sum((r.remaining_kg for r in bron_queue(brand)), Decimal("0"))


def brand_on_hand_kg(brand):
    """Physical kg of this marka in the ombor — what arrived, minus what has been
    sold, plus what came back. This is the only ceiling a sotuv has: bronned kg are
    still sellable to whoever walks in.

    A bron used to come off this figure, so a marka promised to one mijoz could not
    be sold to another at all. Real trade does not work that way — a mijoz turns up
    with cash for granula that was promised to somebody who is not collecting, and
    that sotuv has to be enterable. Who gets the kg is settled between the operator
    and the mijozlar; the screen reports the promise and lets it be overridden."""
    return sum((lot.kg - lot.sold_kg + lot.returned_kg
                for lot in arrived_lots().filter(contract_line__brand=brand)),
               Decimal("0"))


def bron_brands():
    """Markalar a bron may be taken against — every place the granula could come
    from, in the order a kg travels:

      * still to be sent on a kelishuv   (nothing shipped yet — booking ahead)
      * sent and on the road             (fully shipped, not yet landed)
      * sitting in the ombor             (landed, unsold)

    The middle case is easy to miss and is not an edge case: a kelishuv whose trucks
    have all left has `remaining_kg` of zero and no arrived lot, so leaving it out
    would make a marka unbookable for exactly the weeks it is in transit."""
    brands = {line.brand for line in ContractLine.objects
              .select_related("contract").prefetch_related("shipment_lines")
              if line.remaining_kg > 0}
    brands |= {line.contract_line.brand for line in ShipmentLine.objects
               .filter(shipment__arrived__isnull=True)
               .select_related("contract_line")}
    brands |= {lot.brand for lot in arrived_lots()}
    return sorted(brands)


def fifo_lots(brand):
    """Arrived lots of one brand that still have kg available, oldest arrival
    first (then id) — the FIFO consumption order for the ombor."""
    lots = arrived_lots().filter(contract_line__brand=brand).order_by(
        "shipment__arrived", "id")
    return [lot for lot in lots if lot.available_kg > 0]


def draw_down_bron(sale):
    """Take a sotuv out of its OWN mijoz's bron for that marka. Returns the kg drawn.

    A bron is a promise of kg of one marka to one mijoz. When that mijoz is served,
    the promise is smaller — however the granula reached them. Only the Brondan
    sotuv button used to say so, so an ordinary sotuv to the bron's own holder left
    the bron sitting at its full kg forever.

    That is not a cosmetic drift. A bron blocks the shelf, so bron #1 (74 400 kg of
    2102 campaund) went on blocking 74 400 kg against 41 640 kg on hand while its
    holder bought 24 000 kg of it over the counter — the marka read as unsellable
    for everybody, its own holder included.

    Their own brons only, oldest first: somebody else's bron is somebody else's
    promise and this sotuv does not settle it. A sotuv that already came FROM a
    bron is left alone — `reservation_convert` has booked it."""
    if sale.reservation_id:
        return Decimal("0")
    remaining, drawn = sale.kg, Decimal("0")
    for bron in bron_queue(sale.line.brand):
        if remaining <= 0:
            break
        if bron.customer_id != sale.customer_id:
            continue
        take = min(bron.remaining_kg, remaining)
        bron.fulfilled_kg += take
        if bron.remaining_kg <= 0:
            bron.status = Reservation.Status.CONVERTED
        bron.save(update_fields=["fulfilled_kg", "status"])
        # Linked to the FIRST bron it touched, so the sotuv can say which promise it
        # went against and `release_bron` knows where to put the kg back.
        if sale.reservation_id is None:
            sale.reservation = bron
            sale.save(update_fields=["reservation"])
        remaining -= take
        drawn += take
    return drawn


def release_bron(sale):
    """Give a sotuv's kg back to the bron it was drawn from — for an edit or a
    delete. Returns the kg released.

    A bron closed by the sotuv reopens: the promise is unkept again, and a bron
    that stayed CONVERTED would go on reading as served while the mijoz waits."""
    bron = sale.reservation
    if bron is None:
        return Decimal("0")
    give = min(sale.kg, bron.fulfilled_kg)
    if give <= 0:
        return Decimal("0")
    bron.fulfilled_kg -= give
    if bron.status == Reservation.Status.CONVERTED and bron.remaining_kg > 0:
        bron.status = Reservation.Status.ACTIVE
    bron.save(update_fields=["fulfilled_kg", "status"])
    return give


def brand_stock_costed():
    """Per marka in the ombor: what is on the shelf, how much of it somebody has
    bronned, the blended landed cost, and which kelishuvlar it came from.

    `reserved` rides beside `on_hand` rather than being taken off it: what is
    physically there is what may be sold, and the promise is a note next to it. A
    brand's lots can carry different landed costs and come from different
    kelishuvlar, so the cost is kg-weighted and the codes are the full set."""
    on_hand, cost, codes = {}, {}, {}
    for lot in arrived_lots():
        a = lot.available_kg
        if a > 0:
            b = lot.brand
            on_hand[b] = on_hand.get(b, Decimal("0")) + a
            cost[b] = cost.get(b, Decimal("0")) + a * lot.landed_cost_per_kg
            codes.setdefault(b, set()).add(lot.contract_line.contract.code)
    rows = []
    for b in sorted(on_hand):
        reserved = brand_reserved_kg(b)
        rows.append({
            "brand": b,
            "on_hand": on_hand[b],
            "reserved": reserved,
            "cost": (cost[b] / on_hand[b]).quantize(Decimal("0.0001")),
            "codes": sorted(codes[b]),
        })
    return rows


def brand_stock_costed_uzs():
    """The so'm twin of brand_stock_costed's blended tannarx, keyed by brand.

    Kept as a separate lookup rather than another key on those rows so a caller that
    only needs the dollar cost does not pay for the so'm walk as well."""
    totals, avail = {}, {}
    for lot in arrived_lots():
        a = lot.available_kg
        if a > 0:
            b = lot.brand
            avail[b] = avail.get(b, Decimal("0")) + a
            totals[b] = totals.get(b, Decimal("0")) + a * lot.landed_cost_per_kg_uzs
    return {b: (totals[b] / avail[b]).quantize(Decimal("0.01")) for b in avail}


class Sale(MoneyEntry):
    """Sotuv: kg sold from one arrived lot at a sale price. A sale entered by brand
    is split FIFO across the oldest lots (one row per lot slice). Cost of goods is
    NOT snapshotted — `cost_price` reads the lot's live tannarx, so adding a freight
    expense or paying the hamkor's vositachi cut re-prices this sale too.

    A sale agreed in so'm is entered in so'm: `currency` records which side was
    typed and `price`/`price_uzs` hold both at that day's kurs."""

    money_fields = ("price", "price_uzs")

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="sales", verbose_name="Mijoz")
    line = models.ForeignKey("ShipmentLine", on_delete=models.PROTECT,
                             related_name="sales", verbose_name="Lot (mahsulot)")
    reservation = models.ForeignKey("Reservation", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="Bron")
    # The rows entered in ONE go carry the same id: several markalar typed into one
    # modal, and each marka's kg split FIFO across lots. Without it Sotuvlar shows a
    # single trip to the counter as N unrelated rows and nothing on screen says the
    # mijoz took them together. Null on the sotuvlar entered before the field
    # existed and on a sale made from one chosen lot, which is one row by nature.
    group = models.UUIDField("Sotuv guruhi", null=True, blank=True,
                             editable=False, db_index=True)
    kg = models.DecimalField("Sotilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg sotuv narxi (USD)", max_digits=14, decimal_places=4)
    price_uzs = models.DecimalField("1 kg sotuv narxi (so'm)", max_digits=18,
                                    decimal_places=2, default=0)
    # The cost that was frozen into this sale before tannarx went fully live. Kept
    # read-only for audit/reconciliation; null on sales made after the switch. The
    # live cost is the `cost_price` property below.
    cost_price_snapshot = models.DecimalField(
        "1 kg tan narxi (tarixiy)", max_digits=14, decimal_places=4, null=True, blank=True)
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
    def group_sales(self):
        """The sotuvlar entered together with this one, in the order they were typed
        — itself alone when it was entered on its own."""
        if self.group is None:
            return [self]
        return list(Sale.objects
                    .filter(group=self.group)
                    .select_related("line__contract_line", "line__shipment")
                    .order_by("pk"))

    @property
    def cost_price(self):
        """1 kg tan narxi — the live landed cost (goods + freight + vositachi) of the
        lots this sotuv drew from, read fresh every time rather than frozen at sale,
        so cost of goods always reflects the latest expenses and hamkor payments.

        kg-weighted when the sotuv reached across a lot boundary: 5 000 kg off a
        cheap truck and 1 000 off a dearer one cost what they cost, and billing the
        whole sotuv at either lot's narx would misstate the foyda in one direction
        or the other. One slice — the ordinary case — reduces to that lot's tannarx."""
        slices = list(self.lots.all())
        kg = sum((sl.kg for sl in slices), Decimal("0"))
        if kg <= 0:
            # No slice yet: an unsaved sotuv, or one being read mid-construction.
            return self.line.landed_cost_per_kg
        if len(slices) == 1:
            return slices[0].line.landed_cost_per_kg
        total = sum((sl.line.landed_cost_per_kg * sl.kg for sl in slices),
                    Decimal("0"))
        return (total / kg).quantize(Decimal("0.0001"))

    @property
    def cost_price_uzs(self):
        """Tannarx in so'm at THIS sale's kurs, not the lot's: it is the cost as it
        stood against this sale's price, and comparing the two at different kursi
        would make the margin on screen disagree with `profit`."""
        return self.in_som(self.cost_price)

    @property
    def total(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def total_uzs(self):
        return (self.kg * self.price_uzs).quantize(Decimal("0.01"))

    @property
    def returned_amount(self):
        if not hasattr(self, "returns"):  # relation lands in Task 5
            return Decimal("0")
        return sum((r.amount for r in self.returns.all()), Decimal("0"))

    @property
    def returned_amount_uzs(self):
        if not hasattr(self, "returns"):
            return Decimal("0")
        return sum((r.amount_uzs for r in self.returns.all()), Decimal("0"))

    @property
    def returned_kg(self):
        """kg of this sotuv that have already come back."""
        if not hasattr(self, "returns"):
            return Decimal("0")
        return sum((r.kg for r in self.returns.all()), Decimal("0"))

    @property
    def net_kg(self):
        """kg that STAYED sold — what the mijoz actually took away and kept.

        `kg` is what left the shelf on the day and never moves; this is what the
        sotuv amounts to now. Every screen that reads a sotuv as a fact about a
        mijoz wants this one, which is why Sotuvlar prints it: a list still showing
        2 010 kg after 10 of them came back is telling the operator the vazvrat did
        not happen."""
        return self.kg - self.returned_kg

    @property
    def returnable_kg(self):
        """The ceiling a vazvrat may take back — the same figure as `net_kg`, named
        for the question the vazvrat form asks of it: a sotuv already returned in
        full offers nothing rather than offering its original kg a second time."""
        return self.net_kg

    @property
    def price_own(self):
        """1 kg narxi in the money this sotuv was agreed in — what a vazvrat line
        off this sotuv is worth per kg, and never a converted twin."""
        return own_side(self, self.price, self.price_uzs)

    @property
    def net_total(self):
        return self.total - self.returned_amount

    @property
    def net_total_uzs(self):
        return self.total_uzs - self.returned_amount_uzs

    @property
    def paid_uzs(self):
        """Summed off the allocations' own so'm column, not converted from the dollar
        one. A so'm sotuv settled by a so'm to'lov has to land on exactly zero, and a
        figure re-derived at this sotuv's kurs lands a tiyin off it — which is what
        kept a settled so'm sotuv sitting on Qarzlar."""
        if not hasattr(self, "allocations"):
            return Decimal("0")
        return sum((a.amount_uzs for a in self.allocations.all()), Decimal("0"))

    @property
    def remaining_uzs(self):
        return self.net_total_uzs - self.paid_uzs

    @property
    def profit_uzs(self):
        return self.in_som(self.profit)

    @property
    def paid(self):
        """Summed in Python, like its so'm twin above: aggregate() always goes back to
        the database and ignores a prefetched `allocations`, so every screen that asks
        a sotuv whether it is paid — Qarzlar, Sotuvlar, the muddat badge — paid a query
        per row for a figure already in memory."""
        if not hasattr(self, "allocations"):  # relation lands in Task 4
            return Decimal("0")
        return sum((a.amount for a in self.allocations.all()), Decimal("0"))

    @property
    def remaining(self):
        return self.net_total - self.paid

    # --- the sotuv in the currency it was agreed in ---------------------------
    # These are what a qarz is measured by. The dollar pair above stays for the
    # blended figures — tannarx, foyda, the kassa — that have to mix the two.
    @property
    def net_total_own(self):
        return own_side(self, self.net_total, self.net_total_uzs)

    @property
    def paid_own(self):
        return own_side(self, self.paid, self.paid_uzs)

    @property
    def remaining_own(self):
        return own_side(self, self.remaining, self.remaining_uzs)

    @property
    def is_paid(self):
        """Measured in the sotuv's own currency: a so'm sotuv settled in so'm still
        shows a dollar remainder whenever the kurs moved between the sale and the
        to'lov, and would sit on Qarzlar for ever."""
        return self.remaining_own <= 0

    def save(self, *args, **kwargs):
        """A sotuv with no muddat is due the day it was sold.

        Leaving it null used to mean the sotuv could never be overdue and never
        counted as due, so an unpaid sotuv the operator simply had not put a date on
        sat outside every Qarzlar signal. "No date" in practice means "naqd, pay
        now", which is what the sale date says."""
        if self.debt_deadline is None:
            self.debt_deadline = self.date
        result = super().save(*args, **kwargs)
        self.sync_lot()
        return result

    def sync_lot(self):
        """Keep the single-slice case in step with `line` and `kg`, so every caller
        that just creates or edits a Sale gets a correct slice without knowing the
        table exists.

        A sotuv already spanning two lots is left alone: those slices are a replay's
        work and only a replay may rewrite them. Its kg no longer add up until the
        replay runs, which is why the edit flow replays before it saves."""
        slices = list(self.lots.all())
        if len(slices) > 1:
            return
        if not slices:
            SaleLot.objects.create(sale=self, line_id=self.line_id, kg=self.kg)
            return
        one = slices[0]
        if one.line_id != self.line_id or one.kg != self.kg:
            one.line_id, one.kg = self.line_id, self.kg
            one.save(update_fields=["line", "kg"])

    @property
    def is_overdue(self):
        """Unpaid IN ITS OWN CURRENCY and past its muddat — the same measure
        `is_paid` uses, and for the same reason: a so'm sotuv settled in so'm keeps
        a tiyin of dollar remainder for as long as the kurs has moved since, so the
        converted column called a paid-off sotuv late for ever."""
        return (self.remaining_own > 0 and self.debt_deadline is not None
                and self.debt_deadline < timezone.localdate())

    @property
    def is_due(self):
        """The muddat has ARRIVED — today or earlier — and the sotuv is unpaid. The
        superset of `is_overdue`, which is only the days already past: a sotuv due
        today is not late yet, but it is what the operator has to chase today."""
        return (self.remaining_own > 0 and self.debt_deadline is not None
                and self.debt_deadline <= timezone.localdate())

    @property
    def profit(self):
        """What this sotuv earned after vazvratlar — revenue less the cost of the
        goods that stayed sold.

        Written as net revenue minus net cost rather than as a margin with the
        returned margin taken back off it, because the two sides do NOT move
        together. A vazvrat always cancels its revenue; it only cancels the cost when
        the granula came back on the shelf to be sold again. Scrapped goods were
        still bought and paid for, so their tannarx stays — which is what makes a
        write-off show up here as the loss it is.

        The margin form could not say that: subtracting `(narx − tannarx) × kg` for
        restocked rows only meant a scrapped vazvrat credited the mijoz in full and
        left the foyda untouched at the whole original margin."""
        return (self.net_total - self.net_cost).quantize(Decimal("0.01"))

    @property
    def net_cost(self):
        """Tannarx of the kg that are still out there — what was bought, less what
        came back onto the shelf."""
        return ((self.cost_price * self.kg).quantize(Decimal("0.01"))
                - self._restocked_cost)

    @property
    def _restocked_cost(self):
        """Tannarx of the vazvrat kg that went back into the lot. Only restocked
        rows: goods written off relieve nobody of what they cost."""
        if not hasattr(self, "returns"):
            return Decimal("0")
        return sum(((self.cost_price * r.kg).quantize(Decimal("0.01"))
                    for r in self.returns.all() if r.restock), Decimal("0"))

    def __str__(self):
        return f"Sotuv #{self.pk} · {self.customer} · {self.kg} kg"


class SaleLot(models.Model):
    """One slice of a sotuv against one lot: how many of its kg are costed to which
    truck. Usually one row; two when the sotuv had to reach across a lot boundary.

    Split out of `Sale` because a sotuv is two facts glued together. What the mijoz
    took — kg, narx, qarz — is history and never moves. Which lot it is billed to is
    FIFO's ANSWER, and that answer changes the moment an earlier sotuv is corrected:
    the lot empties sooner and everything behind it shifts down the chain.

    Re-costing therefore has to rewrite rows, and it must not be these:
    `PaymentAllocation` and `Return` both hang off a `Sale`, and the allocation's
    summa is stored rather than sliced precisely so a so'm sotuv settled by a so'm
    to'lov lands on zero exactly. Splitting a paid Sale would re-derive those figures
    on every replay and reintroduce the tiyin the stored column exists to prevent.
    So a shift rewrites SaleLot and leaves Sale alone, and to'lovlar cannot be
    disturbed by a correction they have nothing to do with.

    `Sale.line` stays as the first slice's lot — every existing query, form and
    template reads it — and the two are kept in step by whatever writes the slices.
    """

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="lots",
                             verbose_name="Sotuv")
    line = models.ForeignKey(ShipmentLine, on_delete=models.PROTECT,
                             related_name="sale_lots", verbose_name="Lot")
    kg = models.DecimalField("Kg", max_digits=12, decimal_places=3)
    # A lot the operator CHOSE — the Sotish button inside a marka in the ombor picks
    # the dearer lot on purpose (see `sale_create_lot`), and a replay that moved it
    # would silently bill the sotuv to a lot nobody asked for. FIFO's own slices are
    # free to move; these are not.
    pinned = models.BooleanField("Lot qo'lda tanlangan", default=False)

    class Meta:
        ordering = ["sale_id", "id"]
        verbose_name = "Sotuv loti"
        verbose_name_plural = "Sotuv lotlari"

    @property
    def landed_cost_per_kg(self):
        return self.line.landed_cost_per_kg

    def __str__(self):
        return f"Sotuv #{self.sale_id} · lot #{self.line_id} · {self.kg} kg"


class ReturnBatch(models.Model):
    """Vazvrat: one visit, one document — everything a mijoz brought back at once.

    A qaytarish used to hang off ONE sotuv and was reached from that sotuv's page.
    But a mijoz does not come back with "the 12-iyul sotuv"; they come back with
    goods, which may have gone out on four different days at three different narx.
    The batch is that visit; its `lines` are the sotuvlar the goods are traced to.

    The lines still point at a SOTUV rather than at a marka, and that is the whole
    reason this model exists rather than a marka box on a form: the same marka is
    routinely sold to the same mijoz at several narx — one mijoz carries 23 sotuv of
    2102 campaund across four prices, from $1.4167 to $1.65. "5 000 kg qaytdi"
    therefore has no single value; only the sotuv it came off does. The operator
    picks rows and the narx is read off the row, never typed."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="return_batches", verbose_name="Mijoz")
    date = models.DateField("Sana", default=timezone.localdate)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="return_batches",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Vazvrat"
        verbose_name_plural = "Vazvratlar"

    def __str__(self):
        return f"Vazvrat #{self.pk} · {self.customer.name}"

    @property
    def total_kg(self):
        return sum((r.kg for r in self.lines.all()), Decimal("0"))

    def total_by_currency(self):
        """[(currency, qiymat)] — what came back, each sotuv counted in its own money."""
        return _by_currency((r.currency, r.amount_own) for r in self.lines.all())

    def refund_by_currency(self):
        """[(currency, summa)] the mijoz gets back — the part their qarz did not soak up."""
        return _by_currency((s.currency, s.amount_own) for s in self.settlements.all())

    def to_debt_by_currency(self):
        """[(currency, summa)] that cancelled open qarz and moved no money.

        Summed off the LINES, where it was written down at the time. It used to be
        derived as "everything that was not refunded in cash", which quietly counted
        an avans as a paid-off qarz — a vazvrat that handed a mijoz credit was
        reported as one that had settled a debt."""
        return _by_currency((r.currency, r.to_debt_own) for r in self.lines.all())

    def advance_by_currency(self):
        """[(currency, summa)] left sitting as the mijoz's avans.

        What the qarz did not soak up and no cash row took away — the third of the
        three places a vazvrat's value can land, and the one that has no row of its
        own precisely because nothing moved."""
        refunds = dict(self.refund_by_currency())
        to_debt = dict(self.to_debt_by_currency())
        return _by_currency(
            (currency, amount - to_debt.get(currency, Decimal("0"))
             - refunds.get(currency, Decimal("0")))
            for currency, amount in self.total_by_currency())

    @property
    def pending_settlements(self):
        """The rows still owed to the mijoz — cash we said would go out and has not."""
        return [s for s in self.settlements.all() if s.is_pending]

    @property
    def is_pending(self):
        return bool(self.pending_settlements)


class Return(MoneyEntry):
    """Qaytarish: goods coming back from a sale. Credits the customer's debt at the
    sale price (kg * price) regardless of restock; if restocked, the kg flows back
    into the lot via Shipment.returned_kg / available_kg.

    Defaults to the sale's own currency and kurs — crediting a so'm sale back in
    dollars at a moved rate would hand the mijoz a refund they never paid for."""

    money_fields = ("price", "price_uzs")

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE,
                             related_name="returns", verbose_name="Sotuv")
    # The visit this line was part of. Nullable because a qaytarish entered before
    # vazvratlar became a document of their own belongs to no visit, and inventing
    # one for it would be inventing a fact; blank therefore means "single-sotuv
    # qaytarish, entered the old way" rather than "unknown".
    batch = models.ForeignKey(ReturnBatch, on_delete=models.CASCADE, null=True,
                              blank=True, related_name="lines", verbose_name="Vazvrat")
    kg = models.DecimalField("Qaytarilgan kg", max_digits=12, decimal_places=3)
    # How much of this line's value went straight onto the qarz. STORED, not
    # derived: it depends on what the sotuv owed AT THE MOMENT the goods came back,
    # and that is history — the next to'lov moves the qoldiq and the answer with it.
    # The rest of the value is money the mijoz had already paid; whether it stayed
    # as avans or left the kassa is the `ReturnSettlement`'s business, not this row's.
    to_debt = models.DecimalField("Qarzdan ayirildi (USD)", max_digits=14,
                                  decimal_places=2, default=0)
    to_debt_uzs = models.DecimalField("Qarzdan ayirildi (so'm)", max_digits=18,
                                      decimal_places=2, default=0)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4)
    price_uzs = models.DecimalField("1 kg narxi (so'm)", max_digits=18,
                                    decimal_places=2, default=0)
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

    @property
    def amount_uzs(self):
        return (self.kg * self.price_uzs).quantize(Decimal("0.01"))

    @property
    def amount_own(self):
        """The value of this line in the money its sotuv was agreed in — the only
        side a vazvrat may be settled on."""
        return own_side(self, self.amount, self.amount_uzs)

    @property
    def to_debt_own(self):
        return own_side(self, self.to_debt, self.to_debt_uzs)

    @property
    def to_customer_own(self):
        """The part that did NOT cancel qarz — money the mijoz had already paid, so
        it is theirs again. It left as cash or stayed as avans; this row only knows
        that it was not a qarz."""
        return self.amount_own - self.to_debt_own

    def __str__(self):
        return f"Qaytarish #{self.pk} · sotuv #{self.sale_id} · {self.kg} kg"


class ReturnSettlement(CashEntry):
    """The money side of a vazvrat: what the mijoz gets back once the qarz on the
    returned goods has been cancelled.

    A vazvrat cancels open qarz first, and that part moves no money — there is
    nothing to record here for it. Only the EXCESS lands in this table: goods the
    mijoz had already paid for, so the money became theirs again the moment the
    goods came back.

    One row per CURRENCY, not one per vazvrat. A single visit can hand back a dollar
    sotuv and a so'm sotuv together, and the two never blend: refunding a so'm sotuv
    in dollars at today's kurs gives back a sum nobody ever paid.

    Three outcomes, two fields:

    * `ADVANCE` — parked as avans and spent on their next sotuv. No cash moves.
    * `CASH` with `paid_date` — handed over that day; the kassa loses it then.
    * `CASH` without `paid_date` — WE owe them, and `due_date` is the day we
      promised. The kassa has not lost it yet, but it is money that must go out.

    The third is not a third kind of row: it is the second one before it has
    happened, which is why "To'landi" only fills a date in rather than creating
    anything new. It is also why the kassa reads `paid_date` and not `route` — money
    promised is not money gone."""

    class Route(models.TextChoices):
        ADVANCE = "advance", "Mijoz avansiga"
        CASH = "cash", "Kassadan qaytarish"

    batch = models.ForeignKey(ReturnBatch, on_delete=models.CASCADE,
                              related_name="settlements", verbose_name="Vazvrat")
    route = models.CharField("Qaytarish yo'li", max_length=8, choices=Route.choices,
                             default=Route.ADVANCE)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    # The day we said the money would go out. Set only on a CASH row that was not
    # paid on the spot — the promise IS the row, so a promise with no day is not one.
    due_date = models.DateField("Qaytarish sanasi", null=True, blank=True)
    # The day it actually left. Empty on a CASH row means we still owe it.
    paid_date = models.DateField("To'langan sana", null=True, blank=True)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="return_settlements",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Vazvrat hisob-kitobi"
        verbose_name_plural = "Vazvrat hisob-kitoblari"

    def __str__(self):
        return f"Vazvrat #{self.batch_id} · {self.get_route_display()}"

    @property
    def is_cash(self):
        return self.route == self.Route.CASH

    @property
    def is_pending(self):
        """Money we have said is going out of the kassa and has not gone yet — our
        qarz to the mijoz."""
        return self.is_cash and self.paid_date is None

    @property
    def is_overdue(self):
        return (self.is_pending and self.due_date is not None
                and self.due_date < timezone.localdate())

    @property
    def amount_own(self):
        return own_side(self, self.amount, self.amount_uzs)

    @property
    def date(self):
        """The day this row belongs to on a ledger: when the money left, or — while
        it has not — the day we promised it. The kassa only ever counts the paid
        ones, so this never dates an outflow that has not happened."""
        return self.paid_date or self.due_date

    @property
    def total_out(self):
        """What the kassa loses: the refund, plus the bank's cut when we carry it."""
        return self.amount + (self.fee_amount if self.fee_on_company else Decimal("0"))

    @property
    def total_out_uzs(self):
        fee = self.in_som(self.fee_amount) if self.fee_on_company else Decimal("0")
        return self.amount_uzs + fee


class RefundAllocation(models.Model):
    """Which to'lov a vazvrat refund was taken OUT of — the outgoing twin of
    `PaymentAllocation`.

    A refund hands back money the mijoz had already paid. Cancelling the sotuv's
    qarz frees that money into their avans pool (see `trim_sale_allocations`), and
    without this row it would STAY there: the next sotuv's sweep would quietly spend
    money we had already counted out over the counter, and the mijoz would be paid
    for one vazvrat twice — once in cash, once in goods.

    Only CASH refunds are allocated. An avans refund is money that is MEANT to stay
    in the pool; that is the whole difference between the two routes."""

    settlement = models.ForeignKey(ReturnSettlement, on_delete=models.CASCADE,
                                   related_name="allocations",
                                   verbose_name="Vazvrat hisob-kitobi")
    # Named as a string: `CustomerPayment` is declared further down the file, and a
    # vazvrat has to sit beside the rest of the return models rather than be moved
    # away from them to satisfy an import order.
    payment = models.ForeignKey("CustomerPayment", on_delete=models.PROTECT,
                                related_name="refund_allocations",
                                verbose_name="Mijoz to'lovi")
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Vazvrat taqsimoti"
        verbose_name_plural = "Vazvrat taqsimotlari"

    def __str__(self):
        return f"Vazvrat #{self.settlement.batch_id} ← to'lov #{self.payment_id}"


class KapitalKind(models.TextChoices):
    """Which way the ta'sischi's own money moved.

    Two directions rather than two models because they are the same fact read from
    either end — money crossing the line between the owner's pocket and the
    business's till — and a Kapital row means nothing without saying which way it
    went."""

    IN = "in", "Kiritildi"
    OUT = "out", "Olindi"


class Kapital(CashEntry):
    """Ta'sischi's own money entering or leaving the kassa.

    The kassa had four outflow models and one inflow (`CustomerPayment`), so the
    money that FUNDED the business had nowhere to be recorded and "Kassadagi pul"
    read as how much had been sunk into it rather than what is on hand. This is the
    row that says where that money came from.

    Not a hamkor qarz and not a mijoz avans: nobody is owed anything on either side,
    which is why it settles nothing and allocates to nothing. It moves the till and
    stops there."""

    kind = models.CharField("Yo'nalish", max_length=3, choices=KapitalKind.choices,
                            default=KapitalKind.IN)
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    # Naqd, the same as every to'lov form in the app.
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="kapital_entries",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Kapital"
        verbose_name_plural = "Kapital"

    #: Same physics as a mijoz to'lov: the bank takes its cut on the way and only the
    #: rest lands. Whose loss that is answers to nothing here — the ta'sischi and the
    #: business are one pocket — so the form never asks, and the row nets.
    default_fee_bearer = FeeBearer.COUNTERPARTY

    #: Which way the ta'sischi's money moved, and when — see CashEntry.
    settlement_fields = ("kind", "date")

    @property
    def is_out(self):
        return self.kind == KapitalKind.OUT

    @property
    def net_amount(self):
        """What actually crossed, after the bank's foiz."""
        return self.amount - self.fee_amount

    @property
    def net_amount_uzs(self):
        """Taken off the stored so'm value rather than reconverted, so the pair keeps
        this row's own kurs — the rule every derived pair here follows."""
        return uzs_slice(self, self.net_amount)

    @property
    def signed_amount(self):
        """What the kassa MOVES BY: positive when the ta'sischi put money in, negative
        when they took some out.

        The one place the direction is applied, so every total that touches Kapital
        gets it right by summing rather than by remembering to branch."""
        return -self.net_amount if self.is_out else self.net_amount

    @property
    def signed_amount_uzs(self):
        return -self.net_amount_uzs if self.is_out else self.net_amount_uzs

    @property
    def crosses_currency(self):
        """Never: a Kapital row settles no qarz agreed in another currency, so its
        kurs was inherited rather than chosen and printing it would tell the reader
        nothing. Named so the ledgers can ask every row the same question."""
        return False

    def __str__(self):
        return f"Kapital {self.get_kind_display().lower()} · {self.amount}$ ({self.date})"


class OtherExpense(CashEntry):
    """Money out of the kassa that belongs to no yuk, hamkor, logist or bojxonachi —
    the "proche chiqim" of the business itself: ijara, ish haqi, kommunal, soliq,
    whatever else the month brings.

    Every other outflow in the app is tied to something and priced INTO something: a
    SupplierPayment settles a kelishuv, a ShipmentExpense lands in a yuk's tannarx, a
    LogistPayment funds a float. Money that runs the office is none of those, and
    until this row existed it had nowhere to go — so it was either left out of the
    kassa entirely, which made "Kassada" disagree with the safe, or hung off whatever
    yuk happened to be open, which quietly inflated that load's tannarx and every
    foyda computed from it. This row moves the till and stops there.

    It is a peer of `Kapital`, not of `ShipmentExpense`: a cost with no cost object.
    Deliberately NOT part of any foyda figure — "Sotuvdan foyda" is a gross margin
    (sotuv less tannarx) and quietly netting the office rent into it would change
    what that number has always meant on every screen it appears on.

    No turkum by request: the izoh is the whole description, which is why it is the
    one field that must be filled. A row saying only "$400 left on the 9th" is not a
    record of anything, and a note is all that stands between this table and that."""

    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    #: Required, unlike every other `note` in the app — see the class docstring.
    note = models.CharField("Izoh", max_length=255,
                            help_text="Nima uchun chiqdi — bu yagona izoh")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="other_expenses",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Boshqa chiqim"
        verbose_name_plural = "Boshqa chiqimlar"

    #: Money going out, like every other chiqim: the bank's cut rides on TOP, so the
    #: payee receives the full figure and the till is short by the foiz as well.
    default_fee_bearer = FeeBearer.COMPANY

    #: What the row is FOR and when — stamped across a split entered in one go.
    settlement_fields = ("date", "note")

    @property
    def total_out(self):
        """What the till loses: the sum plus any bank foiz we absorbed. Same name and
        same shape as the other four outflows, so `kassa_row_sets` can sum the lot
        without asking which kind each row is."""
        return self.amount + (self.fee_amount if self.fee_on_company else Decimal("0"))

    @property
    def total_out_uzs(self):
        fee = self.in_som(self.fee_amount) if self.fee_on_company else Decimal("0")
        return self.amount_uzs + fee

    @property
    def crosses_currency(self):
        """Never: this row settles no qarz agreed in another currency, so its kurs was
        inherited rather than chosen and printing it would tell the reader nothing.
        Named so the daftar can ask every row the same question — see Kapital."""
        return False

    def __str__(self):
        return f"Boshqa chiqim · {self.amount}$ ({self.date})"


class Konvertatsiya(models.Model):
    """Money moving from one heap of the kassa into another — naqd so'm sold for naqd
    dollars, cash walked into the bank, a card balance drawn out over the counter.

    Neither a kirim nor a chiqim: nothing entered the business and nothing left it,
    which is why this row stays out of both daftar and out of every kirim/chiqim
    total. What it does change is WHERE the money is — and the till is drawn as three
    usul in two valyuta, so without a row like this the screen and the safe stop
    agreeing the first time somebody changes money.

    BOTH sides are typed, never one derived from the other. An exchange happens at a
    kurs somebody negotiated that morning, and even a same-currency move can lose a
    foiz on the way (naqd walked onto a karta). Recording what LEFT and what ARRIVED
    is the deal that actually happened; the kurs is read back off the pair.

    Universal on purpose — any usul to any usul, any valyuta to any valyuta. The one
    combination refused is the one that moves nothing: same usul AND same valyuta.
    """

    date = models.DateField("Sana", default=timezone.localdate)
    from_method = models.CharField("Qayerdan", max_length=8, choices=PayMethod.choices,
                                   default=PayMethod.CASH)
    from_currency = models.CharField("Qaysi valyutadan", max_length=3,
                                     choices=Currency.choices, default=Currency.UZS)
    from_amount = models.DecimalField("Chiqdi", max_digits=18, decimal_places=2)
    to_method = models.CharField("Qayerga", max_length=8, choices=PayMethod.choices,
                                 default=PayMethod.CASH)
    to_currency = models.CharField("Qaysi valyutaga", max_length=3,
                                   choices=Currency.choices, default=Currency.USD)
    to_amount = models.DecimalField("Tushdi", max_digits=18, decimal_places=2)
    # Filled in by save() from the two sides whenever they are in different money —
    # the kurs of THIS deal, not the day's. On a same-currency move there is no cross
    # rate to read, so the last one anybody typed stands in: it is only used to give
    # the row's twin column a value, never to decide what the operator handed over.
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=LEGACY_RATE)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="konvertatsiyalar",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Konvertatsiya"
        verbose_name_plural = "Konvertatsiyalar"

    @property
    def crosses_currency(self):
        """Whether the money changed shape as well as place — which is exactly when
        the kurs column is worth printing."""
        return self.from_currency != self.to_currency

    @property
    def deal_rate(self):
        """So'm per dollar as THIS exchange struck it — the so'm side over the dollar
        side, or None when both ends are the same money and there is no rate to read.

        Never the day's kurs: what the operator got for their dollars is the only
        rate this row can honestly print."""
        if not self.crosses_currency:
            return None
        soms, dollars = ((self.from_amount, self.to_amount)
                         if self.from_currency == Currency.UZS
                         else (self.to_amount, self.from_amount))
        if not dollars:
            return None
        return (soms / dollars).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _pair(self, amount, currency):
        """One side as the (dollar, so'm) pair every stored figure in the app carries.

        A crossing row IS its own pair — one side was handed over in dollars and the
        other in so'm, so both columns are figures somebody actually counted, and the
        blended totals see the exchange as the non-event it is. Only a same-currency
        move has a twin to derive, and it derives it at the stored kurs."""
        if self.crosses_currency:
            return ((self.from_amount, self.to_amount)
                    if self.from_currency == Currency.USD
                    else (self.to_amount, self.from_amount))
        return convert_pair(amount, currency, self.exchange_rate)

    @property
    def from_pair(self):
        return self._pair(self.from_amount, self.from_currency)

    @property
    def to_pair(self):
        return self._pair(self.to_amount, self.to_currency)

    @property
    def net_pair(self):
        """(dollar, so'm) the till's BLENDED pair moves by — zero whenever the money
        only changed shape, and the cost of the operation when it did not.

        A million so'm that arrives on a karta as 990 000 lost 10 000 on the way; the
        heaps below show both halves of the move in full, and this is the only figure
        that says the business is 10 000 poorer for it."""
        (from_usd, from_uzs), (to_usd, to_uzs) = self.from_pair, self.to_pair
        return to_usd - from_usd, to_uzs - from_uzs

    def save(self, *args, **kwargs):
        # The kurs is the deal's own whenever there is one to read, so a row written
        # by hand in a shell or a test carries the same rate the form would have
        # stored. A same-currency row keeps whatever it was given.
        rate = self.deal_rate
        if rate:
            self.exchange_rate = rate
        super().save(*args, **kwargs)

    def __str__(self):
        return (f"{self.get_from_method_display()} "
                f"{self.get_from_currency_display()} → "
                f"{self.get_to_method_display()} "
                f"{self.get_to_currency_display()} ({self.date})")


class CustomerPayment(CashEntry):
    """To'lov received from a customer. `amount` is what the mijoz SENT; `net_amount`
    is what arrived after the bank's foiz, and that net is the figure that counts
    everywhere — it is what lands in the kassa and what settles the qarz. A mijoz who
    sends 1000 by perechisleniya at 2% has paid off 980 of their debt; the 20 is
    their loss, not ours.

    Not tied to one sale — it auto-allocates (FIFO or manual pick) via
    `allocate_customer_payment`; any leftover is the customer's advance (avans)."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT,
                                 related_name="customer_payments", verbose_name="Mijoz")
    reservation = models.ForeignKey("Reservation", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="earmarked_payments", verbose_name="Bron uchun")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    # Naqd, the same as the hamkor side and as every other kind of to'lov.
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    # WHICH qarz this settlement is aimed at, when the mijoz owes in both currencies
    # at once. A dollar qarz and a so'm qarz are two separate debts — the operator
    # knows which one they are collecting, and without somewhere to say so the money
    # went oldest-first across both and settled a debt nobody was paying.
    #
    # Blank means "wherever it fits, oldest first" — the behaviour every row written
    # before this field existed was booked under, and still the right answer for a
    # mijoz who only owes in one currency.
    target_currency = models.CharField(
        "Qaysi qarzga", max_length=3, choices=Currency.choices, blank=True, default="",
        help_text="Mijozning qaysi valyutadagi qarzi to'lanmoqda")
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="customer_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Mijoz to'lovi"
        verbose_name_plural = "Mijoz to'lovlari"

    #: Money coming IN has always been the sender's loss: 1000 sent by
    #: perechisleniya at 2% paid off 980 of their qarz.
    default_fee_bearer = FeeBearer.COUNTERPARTY

    #: Whose money, when, and which of their qarzlar it is aimed at — see CashEntry.
    settlement_fields = ("customer", "date", "target_currency")

    @property
    def net_amount(self):
        """What actually reached us, after the bank's foiz — the figure the kassa
        gains. Always `amount` less the cut, because that is physics rather than a
        choice; who is out of pocket for it is `settled_amount`."""
        return self.amount - self.fee_amount

    @property
    def net_amount_uzs(self):
        """The so'm twin of net_amount, taken off the stored so'm value rather than
        reconverted, so the pair stays consistent with the row's own kurs."""
        if not self.amount:
            return Decimal("0")
        return (self.amount_uzs * self.net_amount / self.amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def net_amount_own(self):
        """What reached us, in the currency it actually arrived in."""
        return own_side(self, self.net_amount, self.net_amount_uzs)

    @property
    def settled_amount(self):
        """What the mijoz is CREDITED — the figure their qarz falls by, and the pool
        an allocation is drawn from.

        Apart from `net_amount` only when we carry the bank's cut: then they are
        credited everything they sent and we are short the fee, which the kassa
        already shows because only the net arrived."""
        return self.amount if self.fee_on_company else self.net_amount

    @property
    def settled_amount_uzs(self):
        return self.amount_uzs if self.fee_on_company else self.net_amount_uzs

    @property
    def settled_amount_own(self):
        """The credited figure in the currency it arrived in — what this to'lov has
        to be spent down to zero in."""
        return own_side(self, self.settled_amount, self.settled_amount_uzs)

    @property
    def allocated_pair(self):
        """(usd, uzs) of this to'lov that is sitting on sotuvlar rather than waiting
        as an avans. Read as the parent minus its unspent remainder so both sides
        stay exact — the rule every derived pair here follows.

        The parent is `settled_amount`, the pot an allocation is actually drawn from,
        and not `net_amount`. The two are the same figure until WE carry the bank's
        cut; then the mijoz is credited everything they sent while only the net
        arrived, and mixing the two pools reported a to'lov that cleared 1 000 of
        qarz as having cleared 980 — the 20 counted against a mijoz who had already
        been credited it."""
        unspent, unspent_uzs = unspent_payment_pair(self)
        return self.settled_amount - unspent, self.settled_amount_uzs - unspent_uzs

    @property
    def allocated_own(self):
        """How much of this to'lov actually came off a qarz, in the currency it
        arrived in — what the Kirim ledger calls Qarzga ta'sir.

        Not the same question as how much money arrived: a to'lov bigger than the
        mijoz's qarz settles what there is and the rest stays an avans, money we are
        holding rather than money that cleared anything."""
        return own_side(self, *self.allocated_pair)

    @property
    def unspent_own(self):
        """The other half of `allocated_own` — what is still an avans, in the money
        it arrived in. Named on the row rather than left as a subtraction the reader
        has to do, because "500 000 came in, 340 000 cleared a qarz" leaves the
        obvious next question unanswered."""
        return unspent_payment_amount(self)

    @property
    def crosses_currency(self):
        """True when this to'lov paid down a sotuv agreed in the OTHER currency.

        That is the only case where the kurs on this row was chosen by the operator
        rather than inherited from the last one anybody typed (see
        `latest_exchange_rate`), so it is the only case where printing it beside the
        row tells the reader something they did not already know."""
        return any(alloc.sale.currency != self.currency
                   for alloc in self.allocations.all())

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
    # Stored rather than sliced off the parent, because this figure has two readers
    # that must BOTH be exact: it is how much of the sotuv's qarz was cleared (read in
    # the sotuv's currency) and how much of the to'lov was spent (read in the to'lov's
    # currency). A so'm sotuv settled by a so'm to'lov has to land on zero exactly, and
    # a figure re-derived through the dollar column lands a tiyin off.
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def in_currency(self, currency):
        """This slice as read by one side of it. The sotuv asks in the currency it was
        agreed in, the to'lov in the one it arrived in — and when those differ the two
        answers are both true, at the kurs the money actually moved at."""
        return self.amount_uzs if currency == Currency.UZS else self.amount

    @property
    def currency(self):
        """The to'lov's currency: an allocation is not money of its own, it is a part
        of one payment that arrived in one currency."""
        return self.payment.currency

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "To'lov taqsimoti"
        verbose_name_plural = "To'lov taqsimotlari"

    def __str__(self):
        return f"{self.payment_id} → sotuv #{self.sale_id}: {self.amount}$"


def unspent_payment_amount(payment):
    """The part of a to'lov that is not sitting on any sotuv yet — the mijoz's avans.

    Read in the currency the money ARRIVED in, which is the currency it is an avans
    in: 6 500 000 so'm handed over is 6 500 000 so'm of credit until it is put on a
    sotuv, whatever the kurs does next. The same slice read from the sotuv's side
    answers a different question, in that sotuv's currency — see
    PaymentAllocation.in_currency.

    The pool is net_amount, not amount: a perechisleniya foiz never reached us, so it
    cannot pay down anybody's sale.

    Summed in Python rather than with aggregate() so a prefetched `allocations` is
    actually used — aggregate() always goes back to the database, which turned the
    kassa's avans figure into one query per to'lov."""
    return own_side(payment, *unspent_payment_pair(payment))


def unspent_payment_pair(payment):
    """(usd, uzs) of a to'lov that is not sitting on any sotuv yet.

    Both columns taken straight off the parent minus its slices, so each side is
    exact. The kassa needs the pair — it holds cash in both currencies at once and
    says so — while a qarz needs only the side the money arrived in."""
    allocated = allocated_uzs = Decimal("0")
    for alloc in payment.allocations.all():
        allocated += alloc.amount
        allocated_uzs += alloc.amount_uzs
    # Money handed back over the counter is spent just as surely as money put on a
    # sotuv — see `RefundAllocation`. Counted here rather than only in
    # `Customer.balance` so the SWEEP cannot spend it a second time.
    for refund in payment.refund_allocations.all():
        allocated += refund.amount
        allocated_uzs += refund.amount_uzs
    return (payment.settled_amount - allocated,
            payment.settled_amount_uzs - allocated_uzs)


def allocation_pair(payment, spent_own):
    """Both columns of a slice worth `spent_own` of `payment`, in the payment's own
    currency.

    Taken as a FRACTION of the parent rather than converted at its kurs: the slices of
    one to'lov then add up to exactly that to'lov on both sides, so a to'lov spent in
    full leaves nothing behind and a sotuv covered in full lands on zero. Converting
    each slice separately leaves a tiyin adrift on one side or the other."""
    total_own = payment.settled_amount_own
    if not total_own:
        return Decimal("0"), Decimal("0")
    share = Decimal(spent_own) / total_own
    return ((payment.settled_amount * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            (payment.settled_amount_uzs * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _place(payment, sale, take_own, pool_own=None):
    """Put `take_own` of a to'lov — measured in the SOTUV's currency — onto that sotuv.

    The figure the operator and the sotuv care about is how much qarz was cleared, so
    that is what is asked for; the slice is then sized in the to'lov's own currency so
    it can be taken as a fraction of it.

    `pool_own` is what this to'lov still has, in ITS OWN currency, and caps the slice.
    Crossing a currency rounds twice — once into the sotuv's money to decide what to
    take, once back into the to'lov's to size the slice — and when both rounds go the
    same way the round trip returns MORE than was there: 5 000 000 so'm put against a
    dollar sotuv comes back as 5 000 040. Booking that as spent left the to'lov 40
    so'm overdrawn, and an overdrawn to'lov reads as a so'm qarz the mijoz never ran
    up. Capped, the excess is simply never spent — it stays where it already was, as
    the mijoz's avans."""
    if take_own <= 0:
        return Decimal("0")
    spent_own = _in_payment_currency(payment, sale, take_own)
    if pool_own is not None:
        spent_own = min(spent_own, Decimal(pool_own))
    if spent_own <= 0:
        return Decimal("0")
    amount, amount_uzs = allocation_pair(payment, spent_own)
    if pool_own is not None:
        spent_own, amount, amount_uzs = _absorb_tail(
            payment, sale, spent_own, Decimal(pool_own), amount, amount_uzs)
    PaymentAllocation.objects.create(payment=payment, sale=sale,
                                     amount=amount, amount_uzs=amount_uzs)
    return spent_own


def _absorb_tail(payment, sale, spent_own, pool_own, amount, amount_uzs):
    """Let a slice swallow the tail of its to'lov that nothing could ever place.

    The mirror of the cap in `_place`, for the round trip that lands SHORT. Crossing
    a currency rounds twice and the pair above only guards the direction that comes
    back with MORE than was there; coming back with LESS strands a tail worth under
    one tiyin in the sotuv's money. 97 999 959.20 so'm against a dollar qarz is taken
    as $8 166.66 and returns as 97 999 920 — 39.20 so'm left behind. No later sweep
    can reach it, because in the sotuv's money it converts to 0.00, so it sits
    forever as an avans the mijoz never made, printed beside the qarz they really do
    have.

    Swallowed only when doing so does not move the SOTUV's column. The tail is worth
    nothing on that side by definition, so the slice grows in the to'lov's own money
    alone and the sotuv can never be credited a tiyin it was not owed. On a pair
    where the wider share would round the sotuv's side up anyway, the tail is left
    alone — a crumb on the mijoz's card is the cheaper of the two wrongs."""
    tail = pool_own - spent_own
    if tail <= 0 or _in_sale_currency(payment, sale, tail) > 0:
        return spent_own, amount, amount_uzs
    whole = allocation_pair(payment, pool_own)
    # The sotuv's side is whichever column is NOT the to'lov's own currency, and the
    # two differ here by construction: a same-currency tail returns above, unconverted
    # and non-zero.
    sale_side = 0 if payment.is_som else 1
    if whole[sale_side] != (amount, amount_uzs)[sale_side]:
        return spent_own, amount, amount_uzs
    return pool_own, whole[0], whole[1]


def _in_payment_currency(payment, sale, amount_own):
    """A figure in the sotuv's currency, restated in the to'lov's.

    At the TO'LOV's kurs, not the sotuv's: this is that money changing hands, and the
    rate it changed hands at is the one recorded on the to'lov."""
    if sale.currency == payment.currency:
        return Decimal(amount_own)
    if payment.is_som:                       # dollar sotuv, so'm to'lov
        return (Decimal(amount_own) * payment.exchange_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (Decimal(amount_own) / payment.exchange_rate).quantize(   # so'm sotuv, dollar to'lov
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def _in_sale_currency(payment, sale, amount_own):
    """The mirror of `_in_payment_currency`: what a to'lov's remaining pool is worth
    against one sotuv's qarz."""
    if sale.currency == payment.currency:
        return Decimal(amount_own)
    if payment.is_som:                       # so'm in hand, dollar qarz to clear
        return (Decimal(amount_own) / payment.exchange_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (Decimal(amount_own) * payment.exchange_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def uzs_slice(row, part):
    """The so'm worth of `part` — some fraction of `row`'s amount — at that row's own
    kurs.

    Taken as a share of the stored so'm value rather than reconverted, so a derived
    figure can never disagree with its parent about what the kurs was that day. The
    same rule every stored pair follows."""
    if not row.amount:
        return Decimal("0")
    return (row.amount_uzs * part / row.amount).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def payment_targets(payment, sale):
    """Whether this to'lov is allowed to land on this sotuv.

    A to'lov that names a `target_currency` is collecting ONE of the mijoz's debts —
    the dollar one or the so'm one — and must not wander onto the other. Without a
    target it goes wherever it fits, oldest first, which is what every row written
    before the field existed was booked under.

    Applied in the automatic paths only. A pick the operator typed against a named
    sotuv is already an explicit instruction about where that money goes, and second-
    guessing it here would silently drop money the form said to place."""
    return not payment.target_currency or sale.currency == payment.target_currency


def allocate_customer_payment(payment, picks=None):
    """Allocate a payment across the customer's outstanding sales. `picks` is an
    optional list of (sale_id, amount) chosen in the form; the rest (or all, if no
    picks) auto-fills oldest-first among the sotuvlar this to'lov is aimed at.
    Leftover stays unallocated = advance."""
    # Carried in the TO'LOV's currency — it is that pot being spent down. Each sotuv
    # is measured in its own, and `_place` bridges the two at the to'lov's kurs.
    remaining = unspent_payment_amount(payment)
    with transaction.atomic():
        if picks:
            for sale_id, amt in picks:
                # Fail safe: a stale or tampered pick id (deleted sale, or one
                # belonging to another customer) is skipped, not fatal.
                sale = Sale.objects.filter(pk=sale_id, customer=payment.customer).first()
                if sale is None:
                    continue
                # A pick is typed against the sotuv, so it arrives in the sotuv's
                # currency; the pool is what this to'lov can still reach it with.
                take = min(Decimal(amt), sale.remaining_own,
                           _in_sale_currency(payment, sale, remaining))
                remaining -= _place(payment, sale, take, remaining)
        # FIFO the leftover across still-outstanding sales
        for sale in payment.customer.sales.order_by("date", "id"):
            if remaining <= 0:
                break
            if not payment_targets(payment, sale):
                continue
            take = min(sale.remaining_own, _in_sale_currency(payment, sale, remaining))
            remaining -= _place(payment, sale, take, remaining)
    return remaining  # the advance left over


def reconcile_customer_allocations(customer):
    """Put every unspent so'm of this mijoz's to'lovlar onto their oldest outstanding
    sotuv, oldest to'lov first.

    Non-destructive and idempotent: allocations that already exist — including ones
    the operator picked by hand — are never moved or dropped, only money that is
    sitting unspent gets placed. So it is safe to call after any change to a to'lov,
    a sotuv or a qaytarish.

    This exists because the two narrower helpers each only ever look at one row.
    `allocate_customer_payment` re-spreads the to'lov being edited and
    `apply_customer_advance` fills the sotuv being created; neither notices when
    editing one to'lov leaves an OLDER sotuv short while another to'lov's remainder
    sits unlinked. Those two are the SAME money seen from both sides — the mijoz
    neither owes it nor is owed it — so they cancel in `Customer.balance` and the
    mijoz drops off Qarzlar while the sotuv still shows a qoldiq they have paid."""
    with transaction.atomic():
        outstanding = [s for s in customer.sales.order_by("date", "id")
                       if s.remaining_own > 0]
        if not outstanding:
            return
        for payment in customer.customer_payments.order_by("date", "id"):
            unspent = unspent_payment_amount(payment)
            if unspent <= 0:
                continue
            for sale in outstanding:
                # The sweep obeys each to'lov's own target. Without this the choice
                # made on the modal would survive exactly until the next sweep, which
                # runs after every sotuv, to'lov and qaytarish — so a dollar to'lov
                # aimed at the dollar qarz would quietly end up on the so'm one.
                if not payment_targets(payment, sale):
                    continue
                # `remaining_own` re-reads the allocations, so each sotuv is measured
                # against what earlier to'lovlar in this same sweep already put on it.
                take = min(_in_sale_currency(payment, sale, unspent), sale.remaining_own)
                unspent -= _place(payment, sale, take, unspent)
                if unspent <= 0:
                    break


# ── Hamkor to'lovining taqsimoti ────────────────────────────────────────────────
#
# The outgoing mirror of everything above. One kelishuv covers two or three markalar
# and the money does not arrive one marka at a time: a to'lov aimed at a product
# owing 5 000 is sent as 7 000, and a zaklad is handed over before anybody knows
# which product it will end up buying.
#
# Everything is measured in the KELISHUV's own currency. Unlike the mijoz side there
# is no per-row target to reconcile — a kelishuv is struck in one currency and every
# one of its products is priced in it, so the pool converts once and the rest is
# arithmetic in a single money.


def supplier_allocation_pair(payment, spent_own):
    """Both columns of a slice worth `spent_own` of a hamkor to'lov.

    A FRACTION of the parent rather than a conversion, for the reason
    `allocation_pair` gives on the incoming side: the slices of one to'lov then add
    up to exactly that to'lov on both sides, so one spent in full leaves nothing
    behind and a product covered in full lands on zero."""
    contract = payment.contract
    total_own = own_side(contract, payment.credited_amount, payment.credited_amount_uzs)
    if not total_own:
        return Decimal("0"), Decimal("0")
    share = Decimal(spent_own) / total_own
    return ((payment.credited_amount * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            (payment.credited_amount_uzs * share).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _line_capacity(line, contract, taken):
    """What this product can still absorb, in the kelishuv's currency.

    Measured against `expected_value` — what the product will really cost, trucks
    already sent at the price they went at plus the rest at the agreed narx — and
    NOT against what has shipped so far. Paying a hamkor before their truck leaves is
    ordinary here, and a ceiling of "goods delivered" would refuse the zaklad this
    whole arrangement exists to record."""
    total = own_side(contract, line.expected_value, line.expected_value_uzs)
    return total - taken.get(line.pk, Decimal("0"))


def _supplier_taken(contract, exclude_payment=None):
    """What every product of a kelishuv already has on it, by line id — the other
    to'lovlar's slices, in the kelishuv's currency."""
    taken = {}
    slices = (SupplierPaymentAllocation.objects
              .filter(line__contract=contract)
              .select_related("payment"))
    if exclude_payment is not None:
        slices = slices.exclude(payment_id=exclude_payment.pk)
    for row in slices:
        taken[row.line_id] = (taken.get(row.line_id, Decimal("0"))
                              + row.in_currency(contract.currency))
    return taken


def _truck_weights(lines):
    """Each product's share of a zaklad, by MASHINA count.

    The kelishuv owner's rule: 10 000 handed over on a kelishuv running five trucks
    of one marka and five of another splits 5 000 / 5 000, and an uneven plan splits
    in that same proportion. The plan is what counts where one was set and the trucks
    actually sent where it was not — the same "plan or sent" reading the doska's
    counters use, so the split does not change shape depending on a field nobody
    filled in.

    Returns None when there is nothing to weigh by (no plan, no trucks): then the
    money simply runs the kelishuv in order, which is what it did before."""
    weights = {}
    for line in lines:
        sent, planned = line.truck_progress
        weights[line.pk] = Decimal(planned or sent or 0)
    return weights if sum(weights.values()) > 0 else None


def allocate_supplier_payment(payment):
    """Spread one hamkor to'lov across the kelishuv's products; hand back what no
    product could take.

    Two ways in, one way out:

    * The to'lov NAMES a marka — it starts there and runs FORWARD through the
      kelishuv in its own order, then round to the ones before it. Pay 7 000 against
      a marka owing 5 000 and the extra 2 000 lands on the next product rather than
      overpaying the one that was named. Wrapping matters: money must find every
      product that is still owed before it is called an avans.

    * The to'lov names none — a zaklad — and it splits by mashina count
      (`_truck_weights`). A product that cannot take its whole share (already
      covered) passes the rest on, and the leftovers then run the kelishuv in order
      rather than being lost.

    Whatever is left over stays unallocated. That is the hamkor's avans and it
    belongs to the HAMKOR, not to this kelishuv — they can spend it on another one
    (`partner_advance_total`), which is how the incoming side treats a mijoz's avans
    too.

    Re-runnable: it clears this to'lov's own slices first, so calling it again after
    an edit, a new truck or another to'lov re-answers the question from scratch
    rather than stacking a second set on top."""
    contract = payment.contract
    with transaction.atomic():
        payment.allocations.all().delete()
        pool = own_side(contract, payment.credited_amount, payment.credited_amount_uzs)
        if pool <= 0:
            return Decimal("0")
        lines = list(contract.lines.all())
        if not lines:
            return pool
        taken = _supplier_taken(contract, exclude_payment=payment)
        # Accumulated, then written once per product at the end: a share that lands in
        # two passes — its slice of the zaklad and again from the leftovers — is still
        # one answer about one product, and two rows saying so read as two payments.
        placed = {}

        def place(line, want):
            """Put `want` on one product, capped by what it can still absorb."""
            nonlocal pool
            take = min(want, _line_capacity(line, contract, taken), pool)
            if take <= 0:
                return
            placed[line] = placed.get(line, Decimal("0")) + take
            taken[line.pk] = taken.get(line.pk, Decimal("0")) + take
            pool -= take

        named = payment.contract_line
        if named is None:
            weights = _truck_weights(lines)
            if weights:
                # Sized off the pool as it arrived, not as it drains: each product's
                # share is its share of the whole zaklad, and what one cannot take
                # falls through to the run below rather than inflating the next.
                whole = pool
                for line in lines:
                    share = (whole * weights[line.pk]
                             / sum(weights.values())).quantize(Decimal("0.01"),
                                                               rounding=ROUND_HALF_UP)
                    place(line, share)
            order = lines
        else:
            start = next((i for i, ln in enumerate(lines) if ln.pk == named.pk), 0)
            order = lines[start:] + lines[:start]

        for line in order:
            if pool <= 0:
                break
            place(line, pool)

        for line, amount in placed.items():
            slice_usd, slice_uzs = supplier_allocation_pair(payment, amount)
            SupplierPaymentAllocation.objects.create(
                payment=payment, line=line, amount=slice_usd, amount_uzs=slice_uzs)
        return pool


def unspent_supplier_payment_pair(payment):
    """(usd, uzs) of a hamkor to'lov that sits on no product yet.

    Both columns straight off the parent minus its slices, so each side is exact —
    the same rule `unspent_payment_pair` follows on the incoming side. The pool is
    `credited_amount`: the bank's foiz rides on top of what the hamkor receives, so
    it never was theirs to be credited with."""
    slices = list(payment.allocations.all())
    return (payment.credited_amount - sum((s.amount for s in slices), Decimal("0")),
            payment.credited_amount_uzs - sum((s.amount_uzs for s in slices), Decimal("0")))


def unspent_supplier_payment_amount(payment):
    """The avans part of one hamkor to'lov, in the kelishuv's own currency."""
    return own_side(payment.contract, *unspent_supplier_payment_pair(payment))


def partner_advance_total(partner):
    """(usd, uzs) a hamkor is holding of ours that no product has claimed yet.

    Read across ALL of their kelishuvlar, because that is whose money it is. A zaklad
    handed over on one kelishuv is credit with that hamkor, and they carry it to the
    next one — the same way a mijoz's avans is the mijoz's rather than one sotuv's."""
    total = total_uzs = Decimal("0")
    payments = (SupplierPayment.objects.filter(contract__partner=partner)
                .select_related("contract").prefetch_related("allocations"))
    for payment in payments:
        usd, uzs = unspent_supplier_payment_pair(payment)
        if own_side(payment.contract, usd, uzs) > 0:
            total += usd
            total_uzs += uzs
    return total, total_uzs


def reconcile_supplier_allocations(contract):
    """Re-answer the whole kelishuv: every to'lov placed again, oldest first.

    Needed because a slice is an answer to a question that keeps moving. A new truck
    raises what a product costs, an edited to'lov changes what there is to spread,
    and a deleted one frees the products it was sitting on — after any of those the
    stored slices are yesterday's answer. Oldest first so the result does not depend
    on the order the rows happen to come back in."""
    with transaction.atomic():
        for payment in (contract.supplier_payments
                        .select_related("contract", "contract_line")
                        .order_by("date", "pk")):
            allocate_supplier_payment(payment)


def apply_customer_advance(sale):
    """Cover a fresh sale from money the mijoz has already handed over. Money
    earmarked for this sale's bron (`CustomerPayment.reservation == sale.reservation`)
    applies FIRST — it was put down for this lot specifically — and the general avans
    then goes through `reconcile_customer_allocations`.

    Deliberately NOT "fill this sale from the avans": an avans that sits unspent
    while an older sotuv is unpaid belongs to that older sotuv. Filling the new sale
    first let money skip a queue it was already in, leaving the older sotuv showing
    a qoldiq the mijoz had in fact already covered."""
    with transaction.atomic():
        if sale.reservation_id:
            for payment in sale.reservation.earmarked_payments.order_by("date", "id"):
                if sale.remaining_own <= 0:
                    break
                unspent = unspent_payment_amount(payment)
                pool = _in_sale_currency(payment, sale, unspent)
                _place(payment, sale, min(pool, sale.remaining_own), unspent)
        reconcile_customer_allocations(sale.customer)


def trim_sale_allocations(sale):
    """After a return shrinks a sale's net_total, drop the now-excess allocation
    amount (newest allocation first) so Σ allocations ≤ net_total. The freed amount
    returns to its payment's spendable advance, reachable by apply_customer_advance —
    otherwise a return on a paid sale would strand money in a dead over-cap row."""
    placed = sum((a.in_currency(sale.currency) for a in sale.allocations.all()),
                 Decimal("0"))
    over = placed - sale.net_total_own
    if over <= 0:
        return
    with transaction.atomic():
        for alloc in sale.allocations.order_by("-id"):
            if over <= 0:
                break
            own = alloc.in_currency(sale.currency)
            if own <= over:
                over -= own
                alloc.delete()
            else:
                # Shrunk on BOTH sides by the same fraction, so the slice stays a
                # true part of its to'lov — trimming one column alone would free
                # money on the sotuv's side that the to'lov still counts as spent.
                share = (own - over) / own
                alloc.amount = (alloc.amount * share).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                alloc.amount_uzs = (alloc.amount_uzs * share).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                alloc.save(update_fields=["amount", "amount_uzs"])
                over = Decimal("0")


def allocate_refund(settlement):
    """Take a cash vazvrat refund out of the to'lovlar it is giving back, oldest
    first. Returns whatever could not be placed.

    The mirror image of `reconcile_customer_allocations`: that one spends a mijoz's
    unspent money on their qarz, this one spends it out of the door. Both draw on
    the same pool, which is exactly why this has to exist — money handed back that
    is not booked as spent stays spendable, and the next sotuv takes it.

    A `settlement` is duck-compatible with a `sale` for the two currency helpers
    below: both are asked only for `currency`, so the conversion rules a qarz gets
    are the rules a refund gets, and neither invents a kurs of its own.

    The remainder is normally zero — `trim_sale_allocations` freed exactly this much
    a moment ago. It can be a tiyin or two when the refund and the to'lov are in
    different currencies, and that residue is simply left in the pool: it is the
    mijoz's money either way, and rounding it out of existence is worse than leaving
    it where it already sat."""
    customer = settlement.batch.customer
    left_own = settlement.amount_own
    for payment in customer.customer_payments.order_by("date", "id"):
        if left_own <= 0:
            break
        pool_own = unspent_payment_amount(payment)
        if pool_own <= 0:
            continue
        take_own = min(_in_sale_currency(payment, settlement, pool_own), left_own)
        if take_own <= 0:
            continue
        # Capped at the pool for the same reason `_place` caps it: crossing a
        # currency rounds twice, and both rounds going the same way would take out
        # more than the to'lov ever held.
        spent_own = min(_in_payment_currency(payment, settlement, take_own), pool_own)
        if spent_own <= 0:
            continue
        amount, amount_uzs = allocation_pair(payment, spent_own)
        RefundAllocation.objects.create(settlement=settlement, payment=payment,
                                        amount=amount, amount_uzs=amount_uzs)
        left_own -= take_own
    return left_own


class ShipmentExpense(CashEntry):
    """Road/customs money spent on one load. Rolls into that load's landed cost:
    landed cost per kg = contract price + expenses ÷ kg (decision #3).

    Money going out, so a perechisleniya foiz rides on top: `total_out` is what the
    kassa really loses, while `amount` stays the cost of the thing bought."""

    class Category(models.TextChoices):
        # Ordered the way the money is spent on a load: cleared, hauled, unloaded,
        # then the rest. Deklarant sits by Bojxona and Gruzchi by Transport because
        # that is the pair each is asked about.
        CUSTOMS = "customs", "Bojxona"
        DECLARANT = "declarant", "Deklarant"
        TRANSPORT = "transport", "Transport"
        LOADER = "loader", "Gruzchi"
        ROAD = "road", "Yo'l xarajati"
        CERT = "cert", "Sertifikat"
        OTHER = "other", "Boshqa"

    #: The turkumlar the kassa settles when the truck is UNLOADED rather than on the
    #: day the bill is written down.
    #:
    #: Transport and gruzchi are paid at the ombor gate — the driver is settled with
    #: when he delivers and the loaders when they have carried it in. Booking them out
    #: of the till the moment the row was entered showed money gone that was still in
    #: the safe, sometimes for weeks while the load was on the road.
    #:
    #: Bojxona and deklarant deliberately stay immediate: they are paid AT the border,
    #: long before the warehouse, so waiting for arrival would misstate the till in the
    #: other direction and for longer.
    ARRIVAL_CATEGORIES = frozenset({Category.TRANSPORT, Category.LOADER})

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="expenses", verbose_name="Yuk")
    date = models.DateField("Sana", default=timezone.localdate)
    category = models.CharField("Turkum", max_length=10, choices=Category.choices,
                                default=Category.OTHER)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    amount_uzs = models.DecimalField("Summa (so'm)", max_digits=18, decimal_places=2,
                                     default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    note = models.CharField("Izoh", max_length=255, blank=True)
    # Set when a logist paid this out of the balance we already funded. The cost
    # still belongs to the yuk, but the cash left the kassa when we topped the
    # logist up — charging it again here would bill us twice for one payment.
    logist = models.ForeignKey("Logist", on_delete=models.PROTECT, null=True,
                               blank=True, related_name="driver_advances",
                               verbose_name="Logist to'ladi")
    # The same arrangement one desk over: set when the bojxonachi paid this out of
    # money we already sent them for the load. This is the row that answers "we sent
    # 40 — what did it really cost?", so it is what customs_spent_by_currency adds
    # up. Mutually exclusive with `logist`: one payment has one payer (see clean()).
    customs_agent = models.ForeignKey("CustomsAgent", on_delete=models.PROTECT,
                                      null=True, blank=True, related_name="expenses",
                                      verbose_name="Bojxonachi to'ladi")
    # The advance the logist hands the driver as the truck leaves. Flagged rather
    # than inferred from `logist` alone, because a logist may pay a yuk's bojxona
    # too — and this is the one row the yuk form owns and rewrites on edit.
    is_driver_advance = models.BooleanField("Haydovchi avansi", default=False,
                                            editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipment_expenses",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Yuk xarajati"
        verbose_name_plural = "Yuk xarajatlari"
        constraints = [
            # clean() says this too, but clean() is only reached through a form. The
            # prototype importer, the seeders and a shell one-liner all build rows
            # directly, and a row claiming two payers would quietly debit two
            # people's balances for one payment — the kind of thing that is found
            # months later by a balance nobody can explain.
            models.CheckConstraint(
                condition=models.Q(logist__isnull=True)
                | models.Q(customs_agent__isnull=True),
                name="expense_has_one_payer"),
        ]

    def clean(self):
        """One payment has one payer. Both set would count the same money out of two
        people's balances, and the kassa — which only asks whether ANY holder paid —
        would look right while both accounts drifted."""
        super().clean()
        if self.logist_id and self.customs_agent_id:
            raise ValidationError(
                "Xarajatni logist yoki bojxonachi to'laydi — ikkalasi emas")

    @property
    def from_kassa(self):
        """False when somebody who already holds our money funded it — a logist out
        of their float, a bojxonachi out of what we sent for this load."""
        return self.logist_id is None and self.customs_agent_id is None

    @property
    def paid_by(self):
        """The holder whose balance this came out of, or None for the kassa. One
        accessor rather than two `{% if %}`s on every screen that prints the payer."""
        return self.logist or self.customs_agent

    @property
    def waits_for_arrival(self):
        """True for a row the kassa settles at the ombor gate rather than on the day
        it was written down — see `ARRIVAL_CATEGORIES`.

        Only ever true of a kassa-funded row. One a logist or a bojxonachi paid left
        the till when we funded THEM, which already happened and cannot be waited
        for; `total_out` was zero for those long before this existed."""
        return self.from_kassa and self.category in self.ARRIVAL_CATEGORIES

    @property
    def is_pending(self):
        """Recorded and owed, but still in the till: the yuk has not landed yet.

        Read off `shipment.arrived` rather than off the holat itself. The two say the
        same thing — the date is set the moment a load is moved to the arrival holat
        and cleared when it is moved back off one (see `shipment_set_status`) — and
        this one carries the DAY as well, which is what `cash_date` needs."""
        return self.waits_for_arrival and self.shipment.arrived is None

    @property
    def cash_date(self):
        """The day the till actually loses this row — None while it is pending.

        For a row that waits, the LATER of the two dates. A gruzchi bill written down
        while the truck was still on the road is paid when it lands; one written down
        a week after it landed is paid that day, because money cannot leave before
        anybody has recorded that it is owed. That pair is what lets a xarajat be
        entered at any point in a load's life and still reach the till honestly.

        Everything else moves on the day it says it did.

        Both sides go through `_as_day` before being compared: Django converts a
        DateField only on save or refresh, so a row still in memory carries whatever
        it was handed, and `"2026-07-28" > date(2026, 7, 20)` raises rather than
        sorting. The importer, the seeders and any shell one-liner build rows exactly
        that way — the same reason `expense_has_one_payer` is a DB constraint and not
        only a clean().

        `cash_date_expression()` is this same rule in SQL, for the screens that narrow
        to a period; tests/test_expense_deferral.py pins the two equal row by row."""
        written = _as_day(self.date)
        if not self.waits_for_arrival:
            return written
        arrived = _as_day(self.shipment.arrived)
        if arrived is None:
            return None
        return max(written, arrived)

    @property
    def total_out(self):
        """What the kassa loses: the expense plus any bank foiz — and nothing at all
        when a holder paid it, because that money already left as the LogistPayment
        or CustomsPayment that funded them, or while it is still pending, because a
        transport bill on a truck that has not arrived has not been paid yet.

        The landed cost deliberately keeps using `amount` either way, and so does
        every other figure about what the goods COST. A tannarx is what the load is
        worth to us, which is settled the moment the obligation exists — when the
        cash happens to leave the safe changes the till, not the price of a kg. That
        separation is why deferring this moved no cost figure anywhere in the app.

        A transfer fee is likewise the cost of moving money, not of the goods, and
        who handed the cash over does not change what the granula cost."""
        if not self.from_kassa or self.is_pending:
            return Decimal("0")
        return self.amount + (self.fee_amount if self.fee_on_company else Decimal("0"))

    @property
    def total_out_uzs(self):
        if not self.from_kassa or self.is_pending:
            return Decimal("0")
        fee = self.in_som(self.fee_amount) if self.fee_on_company else Decimal("0")
        return self.amount_uzs + fee

    @property
    def pending_out(self):
        """What this row WILL cost the till, while it is still waiting — zero once it
        has gone. The twin of `total_out`: exactly one of the two is ever non-zero, so
        "in the safe" and "already spent" can never double-count the same bill."""
        if not self.is_pending:
            return Decimal("0")
        return self.amount + (self.fee_amount if self.fee_on_company else Decimal("0"))

    @property
    def pending_out_uzs(self):
        if not self.is_pending:
            return Decimal("0")
        fee = self.in_som(self.fee_amount) if self.fee_on_company else Decimal("0")
        return self.amount_uzs + fee

    @property
    def crosses_currency(self):
        """True when the xarajat is not in the kelishuv's money. The kurs then does
        real work — it is what folds a so'm transport bill into the landed cost of a
        kg bought in dollars — so it is worth printing beside the row."""
        return self.currency != self.shipment.contract.currency

    def __str__(self):
        return f"{self.get_category_display()}: {self.amount}$ (yuk #{self.shipment_id})"


def _as_day(value):
    """A DateField's value as a real date, whatever the instance is holding.

    Django converts on save or refresh, not on assignment, so a row built in memory
    still carries the string it was handed — and two of those cannot be compared with
    a date. `cash_date` compares exactly that pair."""
    return _date.fromisoformat(value) if isinstance(value, str) else value


def cash_date_expression():
    """`ShipmentExpense.cash_date` as SQL, for the screens that narrow xarajatlar to a
    period — the kassa's window and the opening balance behind its waterfall.

    Those filter thousands of rows in the database and cannot ask a Python property.
    The property is the readable statement of the rule and this is the same rule in
    the only other language it has to be said in; tests/test_expense_deferral.py pins
    the two equal row by row, because two spellings of one rule is precisely the pair
    that drifts apart.

    Spelled as explicit When branches rather than with Greatest(): Postgres's GREATEST
    ignores a NULL argument while SQLite's MAX() returns NULL if either side is, and
    this project runs on both (SQLite in dev, Postgres in prod). A pending row would
    then come back as its own date on one and as NULL on the other — the same query
    giving two different tills depending on which machine ran it."""
    waits = models.Q(logist__isnull=True, customs_agent__isnull=True,
                     category__in=ShipmentExpense.ARRIVAL_CATEGORIES)
    return models.Case(
        # Somebody else funded it, or it is a turkum paid on the day it is written:
        # the till moved when the row says it did.
        models.When(~waits, then=models.F("date")),
        # Waits, and the truck has not landed — no cash date at all yet.
        models.When(shipment__arrived__isnull=True,
                    then=models.Value(None, output_field=models.DateField())),
        # Waits and has landed: the later of the two, spelled out rather than MAX()d.
        models.When(shipment__arrived__gt=models.F("date"),
                    then=models.F("shipment__arrived")),
        default=models.F("date"),
        output_field=models.DateField(),
    )


def pending_expenses_by_currency(expenses=None):
    """[(currency, kutilayotgan)] — bills recorded against loads still on the road.

    Money the business has committed and not yet handed over: the till still holds it,
    so it is NOT a chiqim, and the kassa board would be lying by omission without it —
    an operator reading "kassada 87 mln" needs to know that 40 of it is spoken for the
    moment two trucks land. Read per currency like every other heap on that board.

    `expenses` is a list a caller already has loaded; left out, this reads them, and
    `select_related` because `is_pending` asks each row's yuk whether it has
    arrived."""
    if expenses is None:
        expenses = ShipmentExpense.objects.select_related("shipment").all()
    return _by_currency(
        (expense.currency,
         own_side(expense, expense.pending_out, expense.pending_out_uzs))
        for expense in expenses)


def latest_exchange_rate():
    """The most recently entered kurs — for the rows that no longer ask for one.

    A kelishuv struck in one currency and settled in it needs no kurs to know what is
    owed, so the form stopped asking for one. The goods on it still have to be priced
    into a landed cost, and that DOES mix a so'm transport bill with a dollar mol —
    so rather than invent a rate per row, the last real one somebody typed is
    inherited.

    Only rows where the operator genuinely chose a kurs are consulted: a to'lov, a
    xarajat, a sotuv. An empty database falls back to LEGACY_RATE, the same figure
    the columns themselves default to."""
    newest_at = newest_rate = None
    for model in (CustomerPayment, SupplierPayment, ShipmentExpense, Sale):
        row = (model.objects.order_by("-created_at")
               .values("created_at", "exchange_rate").first())
        if row and (newest_at is None or row["created_at"] > newest_at):
            newest_at, newest_rate = row["created_at"], row["exchange_rate"]
    return newest_rate or LEGACY_RATE


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
