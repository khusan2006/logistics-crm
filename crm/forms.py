import json
import re
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.db.models import Prefetch
from django.urls import reverse_lazy
from django.utils import timezone

from .models import (
    LEGACY_RATE, Contract, ContractLine, Currency, Customer, CustomerPayment,
    CustomsAgent, CustomsPayment, Kapital, KapitalKind, Konvertatsiya, Logist,
    LogistPayment, OtherExpense, Partner,
    FeeBearer, PayMethod, Reservation, Return, ReturnBatch, ReturnSettlement,
    Sale, Shipment, ShipmentDelay, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment,
    arrived_lots, brand_on_hand_kg, brand_stock_costed, bron_brands, convert_pair,
    customer_balance_by_currency, latest_exchange_rate, _by_currency,
)
from .fifo import brand_available_kg
from .formatting import normalize_container, phone_intl_widget, validate_intl_phone
from .templatetags.crm_extras import rate, som, usd


def date_widget(**attrs):
    """A <input type="date"> that renders ISO.

    The browser only understands yyyy-mm-dd there; Django otherwise formats the
    value for the active locale ("08.07.2026"), which the input rejects and shows
    as blank — so an edit form looked empty and saving it wiped the date.
    """
    return forms.DateInput(attrs={"type": "date", **attrs}, format="%Y-%m-%d")


def reject_future(value):
    """Money moves when it moves; it cannot move next month.

    A pul harakati dated ahead of today splits the books in two: the kassa page
    counts up to the day you are looking at, while every "how much is in the till"
    figure counts every row there is. One future-dated to'lov is enough to make the
    page show money that another part of the app insists is already there.

    Backdating stays allowed — an old daftar is entered with its real dates, and
    that is the ordinary case. Only tomorrow is refused.

    Deliberately NOT applied to `debt_deadline` (a muddat is a future date by
    definition), to a yuk's `eta` (a plan), or to `new_eta` on an uzaytirish.
    """
    if value and value > timezone.localdate():
        raise forms.ValidationError("Sana kelajakda bo'la olmaydi.")
    return value


def no_future_date(field):
    """Refuse tomorrow on a money field, in the form AND in the date picker.

    The `max` attribute is what stops the wrong date being picked at all; the
    validator is what makes the rule true — a hand-typed or scripted value never
    reaches the browser's picker."""
    field.validators.append(reject_future)
    field.widget.attrs.setdefault("max", timezone.localdate().isoformat())
    return field


def currency_suffix(currency):
    """The unit a money box should be labelled with, once the currency is no longer
    the operator's to pick — the kelishuv already settled it."""
    return "so'm" if currency == Currency.UZS else "USD"


def _group_thousands(field):
    """Mark a numeric field so base.html renders "1 000 000" as the operator types
    (data-money). The JS strips the spaces back to a plain number right before the
    form is submitted, so the server still receives 1000000."""
    field.widget.attrs["data-money"] = ""


def _agreed(values):
    """The one value they all share, or None when they differ (or there are none)."""
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


class GroupedFieldsMixin:
    """Lets a form box a run of related fields under one legend.

    Declare `field_groups = [("Legend", ["a", "b"])]`; `_form_fields.html` walks
    `rendered_fields()` and draws those inside a <fieldset>. A form that declares
    nothing renders exactly as before, so this is opt-in per form rather than a
    change to how every form looks."""

    #: [(legend, [field names])] — grouped where they first appear, in field order.
    #: An optional third element names the tone the box is drawn in
    #: (`fieldgroup--<tone>` in the CSS), for a form whose halves mean opposite
    #: things and should say so before the legend is read.
    field_groups = []

    def _declared_groups(self):
        """`field_groups` with the optional tone filled in, so nothing below has to
        care which of the two shapes a form wrote."""
        return [(g[0], g[1], g[2] if len(g) > 2 else "") for g in self.field_groups]

    def rendered_fields(self):
        declared = self._declared_groups()
        groups = {names[0]: (legend, names, tone) for legend, names, tone in declared}
        grouped = {name for _, names, _ in declared for name in names}
        items = []
        for field in self.visible_fields():
            if field.name in groups:
                legend, names, tone = groups[field.name]
                items.append({
                    "group": True, "legend": legend, "tone": tone,
                    # `self[name]` not `field`: the group lists names, and pulling
                    # each bound field by name keeps them in the legend's order
                    # rather than the form's, and survives a missing one.
                    "fields": [self[n] for n in names if n in self.fields],
                })
            elif field.name not in grouped:
                items.append({"group": False, "field": field})
        return items


class FeePercentFormMixin:
    """The shared rule for a perechisleniya foizi, on every form that carries one.

    A foiz outside 0–100 is a typo, and it does not fail loudly on its own: the
    arithmetic happily accepts 200%, which turns an incoming to'lov into a negative
    one and bills the kassa twice over on the way out.

    The foiz also has to say WHOSE it is. The bank takes its cut either way, but
    whether we carry it — crediting the other side the whole figure and coming up
    short ourselves — or they do is a decision per to'lov, so the form asks. Left
    alone it stays blank, which each model reads as the way that kind of row has
    always behaved (CashEntry.fee_on_company)."""

    #: What the "other side" is called on this form, for the radio label.
    fee_counterparty = "Qarshi tomondan ushlansin"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields.get("fee_bearer")
        if field is None:
            return
        field.required = False
        field.widget = forms.RadioSelect(attrs={"data-fee-bearer": ""})
        field.choices = [(FeeBearer.COMPANY, "Kompaniyadan ushlansin"),
                         (FeeBearer.COUNTERPARTY, self.fee_counterparty)]
        # A blank row keeps its historical meaning, so the box opens on whichever
        # option that is rather than on an empty pair of radios.
        instance = getattr(self, "instance", None)
        default = getattr(instance, "default_fee_bearer", FeeBearer.COMPANY)
        if not self.initial.get("fee_bearer") and not getattr(instance, "fee_bearer", ""):
            self.initial["fee_bearer"] = default

    def clean_fee_percent(self):
        percent = self.cleaned_data.get("fee_percent")
        if percent is None:
            return Decimal("0")
        if percent < 0:
            raise forms.ValidationError("Foiz manfiy bo'la olmaydi")
        if percent > 100:
            raise forms.ValidationError("Foiz 100 dan oshmasligi kerak")
        return percent


def _mark_incoming_fee(form):
    """Wire a mijoz to'lovi's summa and foiz to the live "qo'lga tegadi" hint.

    Only the incoming side gets it. A bank's foiz on money coming IN is carved out
    of the summa — the mijoz sends 10 000 at 2%, we receive 9 800, and only that
    9 800 settles their qarz. The operator types the 10 000, so without the hint the
    fee is invisible until it shows up as an unexplained 200 still owed."""
    form.fields["amount"].widget.attrs["data-fee-base"] = ""
    form.fields["fee_percent"].widget.attrs.update({
        "data-fee-percent": "", "step": "0.01", "min": "0", "max": "100",
    })


class MoneyEntryFormMixin:
    """Shared two-way conversion for a lump sum. The user types `amount` in
    `currency`; after clean(), cleaned_data holds BOTH `amount` (USD) and
    `amount_uzs` (so'm), the typed side exact and the other derived at
    `exchange_rate`.

    The kurs is required in both directions, not just for so'm. That is the whole
    point: a dollar row without one has no so'm value and could never be counted in
    a so'm total — which is exactly what the old `exchange_rate = 0` rows did.

    Also marks the currency/amount/exchange_rate widgets with data-money-*
    hooks so the base.html JS enhancer can render a live counter-currency preview."""

    #: cleaned_data key holding the converted so'm value
    uzs_field = "amount_uzs"
    #: the field the operator types into
    typed_field = "amount"
    #: quantum of the USD side — lump sums are cents, per-kg prices are finer
    usd_places = "0.01"
    #: shown when the typed value is zero or negative
    positive_error = "Summa musbat bo'lishi kerak"
    #: a blank value is an error unless the underlying field is nullable
    allow_blank = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every form built on this mixin books money, so its `date` is the day money
        # moved and cannot be in the future (see reject_future). A row form with no
        # date of its own — a kelishuv/yuk product line — simply has nothing to guard.
        if "date" in self.fields:
            no_future_date(self.fields["date"])
        if "currency" in self.fields:
            self.fields["currency"].widget.attrs["data-money-currency"] = ""
        if "exchange_rate" in self.fields:
            self.fields["exchange_rate"].widget.attrs["data-money-rate"] = ""
            _group_thousands(self.fields["exchange_rate"])
            # Deliberately NOT required at field level: a bron with no narx agreed
            # yet has nothing to convert, so demanding a kurs there would block a
            # legitimate entry. clean() raises whenever a value IS present.
            self.fields["exchange_rate"].required = False
        if self.typed_field in self.fields:
            field = self.fields[self.typed_field]
            field.widget.attrs["data-money-amount"] = ""
            _group_thousands(field)
            # The column is canonical USD, but the operator now types whichever
            # currency the Valyuta picker says — a hard-coded "(USD)" on the input
            # would be a lie the moment they choose So'm.
            field.label = re.sub(r"\s*\((USD|so'm)\)", "", str(field.label))
        # kg is not money, but it is the other figure that runs to five digits
        # (24 000 kg on a truck), so it gets the same as-you-type grouping.
        if "kg" in self.fields:
            _group_thousands(self.fields["kg"])
        self._seed_typed_side()

    def _seed_typed_side(self):
        """Open an existing so'm row showing the so'm figure, not the dollar one.

        The typed field IS the USD column (`amount` / `price`); the so'm twin is not
        a form field at all (see _post_clean). So a ModelForm binding an existing row
        puts the DOLLAR value into the one visible money box — a box labelled so'm on
        a so'm row, and re-read as so'm when the form comes back.

        That is how a 12 000 000 so'm to'lov returned as 1 000 so'm after an Save
        with nothing touched: 1 000 went out as the box's value and came back in as
        the typed so'm amount, then divided by the kurs into $0.08.

        Only the UNBOUND case matters — a bound form renders what was posted — and
        only a row that already exists: a blank form has nothing to restore."""
        instance = getattr(self, "instance", None)
        if instance is None or not getattr(instance, "pk", None):
            return
        if getattr(instance, "currency", None) != Currency.UZS:
            return
        typed = getattr(instance, self.uzs_field, None)
        if typed is not None:
            self.initial[self.typed_field] = typed

    def clean(self):
        cleaned = super().clean()
        currency = cleaned.get("currency")
        typed = cleaned.get(self.typed_field)
        rate = cleaned.get("exchange_rate") or Decimal("0")
        if typed is None:
            # A nullable narx (a truck line falling back to the kelishuv price) has
            # no so'm twin of its own either — it inherits the parent's.
            if self.allow_blank:
                cleaned[self.uzs_field] = None
            return cleaned
        if typed <= 0:
            self.add_error(self.typed_field, self.positive_error)
            return cleaned
        if rate <= 0:
            self.add_error("exchange_rate", "Dollar kursini kiriting")
            return cleaned
        usd, uzs = convert_pair(typed, currency, rate, self.usd_places)
        cleaned[self.typed_field] = usd
        cleaned[self.uzs_field] = uzs
        return cleaned

    def _post_clean(self):
        """Put the derived so'm value on the instance.

        This has to happen here rather than in save(): the so'm side is not a form
        field (it is computed, never typed), so ModelForm would not write it — and
        the Mahsulotlar formsets never call our save() at all, they go through
        formset.save(). _post_clean is the one hook every save path passes through.

        The currency and the kurs ride along whenever the form inherited them instead
        of asking (a kelishuv row, a yuk row). construct_instance() only copies fields
        the form declares, so without this the row would convert at the inherited kurs
        but STORE the column default — and re-opening it would re-derive its narx at a
        rate it was never priced with."""
        super()._post_clean()
        if self.uzs_field in self.cleaned_data:
            setattr(self.instance, self.uzs_field, self.cleaned_data[self.uzs_field])
        for inherited in ("currency", "exchange_rate"):
            if inherited not in self.fields and inherited in self.cleaned_data:
                setattr(self.instance, inherited, self.cleaned_data[inherited])

    def money_kwargs(self):
        """The converted money as kwargs, for the views that build their model
        instance by hand (the FIFO sotuv split, bron→sotuv) instead of via save()."""
        return {
            self.typed_field: self.cleaned_data[self.typed_field],
            self.uzs_field: self.cleaned_data.get(self.uzs_field),
            "currency": self.cleaned_data["currency"],
            "exchange_rate": self.cleaned_data["exchange_rate"],
        }


class PriceEntryFormMixin(MoneyEntryFormMixin):
    """The per-kg twin of MoneyEntryFormMixin: a narx rather than a lump sum.

    Prices carry four decimals where sums carry two — rounding a $/kg to cents
    would move a 24-tonne lot by dollars — so only the quantum and the field names
    differ; the conversion itself is the same."""

    typed_field = "price"
    uzs_field = "price_uzs"
    usd_places = "0.0001"
    positive_error = "Narx musbat bo'lishi kerak"


class CustomerBronSelect(forms.Select):
    """A mijoz <select> whose options carry the markalar that mijoz still has an
    open bron for, so the form's JS can ask the question only when there is one.

    Same idea as ContractChoiceSelect: the answer travels on the option, because
    which mijoz is buying is not known until they pick one and a round trip per
    change would be a request per keystroke on a searchable list."""

    #: {customer_id: [brand, ...]}, set by the form.
    bron_brands = {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        pk = getattr(value, "value", value)
        brands = self.bron_brands.get(pk if isinstance(pk, int) else None)
        if brands is None and str(pk).isdigit():
            brands = self.bron_brands.get(int(pk))
        option["attrs"]["data-bron-brands"] = json.dumps(sorted(brands or []))
        return option


class BronDrawFormMixin:
    """The "Brondan ushlansin" question, on the forms that create a sotuv.

    Serving a mijoz who holds a bron for this marka normally makes their promise
    smaller — otherwise the bron goes on blocking the shelf for granula they have
    already taken. But not every sotuv to a bron holder is against the bron: they
    may be buying something extra and still expect their booking to stand. Only the
    operator knows which, so the form asks instead of always drawing.

    The hidden twin is what makes the question safe to add. An unticked checkbox
    posts NOTHING, which is byte-for-byte the same as a POST that never carried the
    field — and those two must not mean the same thing. An operator clearing the box
    means "leave the bron alone"; a caller that does not know the box exists must
    keep the behaviour it has always had. The twin is always submitted, so its
    presence is what says the question was actually put.

    Added in __init__ rather than declared: Django's form metaclass only collects
    fields from bases that already carry `declared_fields`, so a Field sitting on a
    plain mixin is silently ignored — the box never renders and the answer is always
    absent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["draw_from_bron"] = forms.BooleanField(
            label="Brondan ushlansin", required=False, initial=True,
            help_text="Sotilgan kg shu mijozning broniidan ayiriladi. "
                      "Belgilanmasa, bron to'liq holicha qoladi.")
        # Which markalar each mijoz still has an open bron for, so the question is
        # only put when there IS one — asking a mijoz with no bron whether to draw
        # from it is noise on every ordinary sotuv. One query, walked in Python
        # because `is_open` reads remaining_kg, which is not a column.
        brons = {}
        for bron in Reservation.objects.filter(status=Reservation.Status.ACTIVE):
            if bron.remaining_kg > 0:
                brons.setdefault(bron.customer_id, set()).add(bron.brand)
        picker = self.fields["customer"]
        widget = CustomerBronSelect(attrs=dict(picker.widget.attrs))
        widget.choices = picker.widget.choices     # keeps the field's queryset
        widget.bron_brands = brons
        picker.widget = widget
        # Which marka is being sold: a select on the by-brand form, a fixed one on
        # the per-lot form, where the lot already decided it.
        if "brand" in self.fields:
            self.fields["brand"].widget.attrs["data-bron-brand"] = ""
        self.fields["draw_from_bron"].widget.attrs["data-bron-draw"] = ""
        self.fields["draw_from_bron_asked"] = forms.CharField(
            required=False, initial="1", widget=forms.HiddenInput())
        # Straight under the kg, because that is what the question is about. Appended
        # (which is what adding a field in __init__ does) it landed below Izoh, three
        # screens from the number it governs.
        self.order_fields([name for name in ("customer", "lot", "brand", "kg",
                                             "draw_from_bron")
                           if name in self.fields])

    def clean_draw_from_bron(self):
        ticked = self.cleaned_data.get("draw_from_bron", False)
        return ticked if self.data.get("draw_from_bron_asked") else True


class InheritedRateMixin:
    """For a row that needs a kurs but must not ASK for one.

    A sotuv is agreed, owed and settled in a single currency, so the rate decides
    nothing the operator can see: it never moves what the mijoz owes, it only fills
    the row's other money column so a total that mixes the two can add up. The box
    was one more thing to type on every sale and one more thing to get wrong, so it
    comes off the modal and the row inherits instead — its own rate when editing one
    that already exists, the last kurs anybody actually typed when creating.

    Hidden rather than removed, the same way a to'lov in its kelishuv's own currency
    hides its kurs: what is not asked for is filled in, but a rate that IS supplied
    still stands, so the conversion stays something a caller can pin down. The hidden
    input also keeps carrying the rate the live so'm/dollar preview reads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["exchange_rate"]
        field.widget = forms.HiddenInput(attrs={"data-money-rate": ""})
        field.required = False
        field.label = ""
        self.initial.setdefault("exchange_rate", self._inherited_rate())

    def _inherited_rate(self):
        return (self.instance.exchange_rate if self.instance.pk
                else latest_exchange_rate())

    def clean(self):
        # Seeded before the money mixin converts, so a post that carried no rate at
        # all (or a zero one) still lands on a real figure instead of the mixin's
        # "Dollar kursini kiriting" — a message about a box that is no longer there.
        if (self.cleaned_data.get("exchange_rate") or Decimal("0")) <= 0:
            self.cleaned_data["exchange_rate"] = self._inherited_rate()
        return super().clean()


class PartnerForm(forms.ModelForm):
    class Meta:
        model = Partner
        fields = ["name", "phone", "city", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3}), "phone": phone_intl_widget()}

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))


class CustomerForm(forms.ModelForm):
    """A mijoz, and optionally the money they have already handed over.

    A new mijoz often arrives having paid something up front — for an order that has
    not been written yet. Without a box for it the operator has to save the mijoz,
    find them again and open the to'lov modal; with one the opening avans is part of
    creating them. It becomes an ordinary CustomerPayment sitting on no sotuv, which
    is exactly what an avans is, so it settles their first sotuv by itself."""

    opening_avans = forms.DecimalField(
        label="Oldindan to'lagan puli", required=False, min_value=0, max_digits=14,
        decimal_places=2,
        help_text="Ixtiyoriy — mijoz oldindan pul bergan bo'lsa. Avans bo'lib turadi "
                  "va birinchi sotuvidan yechiladi.",
        widget=forms.NumberInput(attrs={"placeholder": "0", "data-money": ""}))
    opening_avans_currency = forms.ChoiceField(
        label="Avans valyutasi", choices=Currency.choices, initial=Currency.USD,
        required=False)

    class Meta:
        model = Customer
        fields = ["name", "phone", "address", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3}), "phone": phone_intl_widget()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only when creating. Editing a mijoz is not the place to book money — the
        # To'lov button beside them already is, and it records a date and a usul.
        if self.instance.pk:
            self.fields.pop("opening_avans")
            self.fields.pop("opening_avans_currency")

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))

    def opening_payment(self, customer, user=None):
        """The avans as a saved CustomerPayment, or None when nothing was typed.

        Naqd, and no kurs asked: an opening avans is money in hand in one currency,
        and nothing is being converted to know what it is worth. The row still gets a
        rate for the kassa's other column, inherited like every other one."""
        amount = self.cleaned_data.get("opening_avans")
        if not amount:
            return None
        currency = self.cleaned_data.get("opening_avans_currency") or Currency.USD
        rate = latest_exchange_rate()
        usd, uzs = convert_pair(amount, currency, rate)
        return CustomerPayment.objects.create(
            customer=customer, date=timezone.localdate(), amount=usd, amount_uzs=uzs,
            currency=currency, exchange_rate=rate, method=PayMethod.CASH,
            note="Boshlang'ich avans", created_by=user)


class CustomerAvansForm(forms.Form):
    """Money handed over ahead of an order by a mijoz who ALREADY exists.

    The twin of the opening avans above, which only ever appears while a mijoz is
    being created — this is the same event on their fifth visit, which until now
    meant there was no way to book it at all.

    Two amount boxes rather than one valyuta picker: a mijoz commonly puts down
    dollars AND so'm in the same breath ("1000$ va 5 mln so'm"), and a single-
    currency form would make that two trips the operator has to remember to file
    under the same date. Each side is money in hand in its own currency, so neither
    is converted into the other — they are booked as one row each.

    No kurs asked, for the reason the opening avans does not ask either: nothing is
    being converted to know what it is worth. The rows still inherit a rate so the
    kassa's other column holds something."""

    date = forms.DateField(label="Sana", widget=date_widget(),
                           initial=timezone.localdate)
    amount_usd = forms.DecimalField(
        label="Dollarda", required=False, min_value=0, max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs={"placeholder": "0", "data-money": ""}))
    amount_uzs = forms.DecimalField(
        label="So'mda", required=False, min_value=0, max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"placeholder": "0", "data-money": ""}))
    method = forms.ChoiceField(label="To'lov usuli", choices=PayMethod.choices,
                               initial=PayMethod.CASH)
    note = forms.CharField(label="Izoh", max_length=255, required=False,
                           widget=forms.TextInput(attrs={"placeholder": "Ixtiyoriy"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("amount_usd", "amount_uzs"):
            _group_thousands(self.fields[name])
        no_future_date(self.fields["date"])

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("amount_usd") or cleaned.get("amount_uzs")):
            raise forms.ValidationError("Kamida bitta summa kiritilishi kerak")
        return cleaned

    def payments(self, customer, user=None):
        """The avans as one CustomerPayment per currency that was actually filled in.

        An ordinary to'lov with no sotuv behind it, which is exactly what an avans
        is — so it settles their next sotuv by itself, and the caller's reconcile
        puts it on an OLDER unpaid one first if there is one."""
        inherited = latest_exchange_rate()
        rows = []
        for currency, typed in ((Currency.USD, self.cleaned_data.get("amount_usd")),
                                (Currency.UZS, self.cleaned_data.get("amount_uzs"))):
            if not typed:
                continue
            in_usd, in_uzs = convert_pair(typed, currency, inherited)
            rows.append(CustomerPayment.objects.create(
                customer=customer, date=self.cleaned_data["date"],
                amount=in_usd, amount_uzs=in_uzs, currency=currency,
                exchange_rate=inherited, method=self.cleaned_data["method"],
                note=self.cleaned_data.get("note") or "Avans", created_by=user))
        return rows


class ContractForm(forms.ModelForm):
    """The kelishuv header, including the one currency the whole agreement is struck
    and settled in. No kurs is asked for: an agreement in one currency is owed and
    paid in that same currency, and the rate the goods are later costed at is
    inherited rather than typed (see `latest_exchange_rate`)."""

    field_order = ["partner", "currency", "created", "note"]

    class Meta:
        model = Contract
        fields = ["partner", "currency", "created", "note"]
        widgets = {
            "created": date_widget(),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:  # new contract → default the date to today
            self.fields["created"].initial = timezone.localdate
        # Re-striking a live kelishuv in the other currency would re-read every
        # figure already booked against it — a $10 000 to'lov becoming 10 000 so'm —
        # so the choice is frozen the moment real money or goods are attached to it.
        # Django ignores a disabled field's submitted value and keeps the stored one.
        if contract_locked(self.instance):
            self.fields["currency"].disabled = True
            self.fields["currency"].help_text = (
                "To'lov yoki yuk biriktirilgan kelishuvning valyutasi o'zgartirilmaydi")


def contract_locked(contract):
    """A kelishuv with real money or goods already on it can no longer be re-struck
    in the other currency — every figure booked against it would be re-read."""
    return bool(contract and contract.pk and (
        contract.supplier_payments.exists() or contract.shipments.exists()))


def contract_currency(data, instance=None):
    """Which currency the Mahsulot rows are being typed in, read BEFORE the header
    form has been validated.

    The formset is built in the same breath as the form, and a row cannot be
    converted without knowing which currency its narx was typed in — so the value is
    taken off the raw POST. A locked kelishuv keeps its stored currency whatever the
    POST says, matching the disabled picker on the form."""
    if contract_locked(instance):
        return instance.currency
    submitted = (data or {}).get("currency")
    if submitted in Currency.values:
        return submitted
    if instance is not None and instance.pk:
        return instance.currency
    return Currency.USD


def _clean_percent(percent):
    """A foiz box as a usable number: blank is nought, and it has to be a real
    percentage. Shared by the single-row hamkor to'lov and the split one's rows, so
    the two cannot drift into disagreeing about what 150% means."""
    if percent is None:
        return Decimal("0")
    if percent < 0:
        raise forms.ValidationError("Foiz manfiy bo'la olmaydi")
    if percent > 100:
        raise forms.ValidationError("Foiz 100 dan oshmasligi kerak")
    return percent


def _keep_if(queryset, predicate, keep_pk=None):
    """Narrow a select to rows the predicate accepts — plus one already-chosen row
    kept regardless, so editing an entry whose kelishuv has since closed does not
    silently drop it. The predicate reads Python properties (remaining_kg,
    payable_left), so it runs in Python and the result is re-expressed as a pk
    filter to stay a queryset the field can page and order."""
    ids = [obj.pk for obj in queryset if predicate(obj) or obj.pk == keep_pk]
    return queryset.filter(pk__in=ids)


def contract_option_label(contract, payable=False):
    """Kelishuv <option>: code, products, what is still owed, the agreed price —
    a range when the products are priced differently — and the whole agreement.

    `payable` swaps the trailing "jami N kg" for what is still owed IN MONEY. On the
    yuk form the kg is the figure being spent down, so that is what it says; on the
    to'lov form the money is, and it is also the form's ceiling — without it the
    operator had to leave the modal, look the kelishuv up on Kelishuvlar, and come
    back to type a number the form already knew.

    Each narx reads in the currency ITS line was agreed in, so a kelishuv struck in
    so'm is offered in so'm. That is also why both ends of a range carry their unit
    rather than only the upper one: with mixed lines the two ends can be in
    different currencies, and a shared trailing "$/kg" would be a lie about one.

    No hamkor name: the code already starts with it (abulqosim-2 · abulqosim read
    as a stutter)."""
    lines = sorted(contract.lines.all(), key=lambda ln: ln.price or Decimal("0"))
    narxlar = list(dict.fromkeys(rate(ln.price, ln.price_uzs, ln.currency)
                                 for ln in lines))
    if not narxlar:
        price = "—"
    elif len(narxlar) == 1:
        price = narxlar[0]
    else:
        price = f"{narxlar[0]} – {narxlar[-1]}"
    if payable:
        left = contract.payable_left_own
        # In the kelishuv's own currency, like every other qarz figure — the dollar
        # column of a so'm kelishuv drifts with the kurs and would offer to collect
        # money that is not owed.
        tail = f"to'lash: {som(left) if contract.is_som else usd(left)}"
    else:
        tail = f"jami {_clean_number(contract.kg)} kg"
    return (f"{contract.code} · {contract.brand_summary} · "
            f"{_clean_number(contract.remaining_kg)} kg qolgan · {price} · {tail}")


def customer_option_label(customer):
    """Mijoz <option>: the name and their ostatka — the very figure the to'lov is
    being taken against, so it does not have to be looked up on the Qarzlar screen
    first. An overpaid mijoz reads as avans rather than a negative qarz, which is
    the difference between "collect this" and "we owe them this".

    One entry per currency they were dealt with in, each labelled on its own: a mijoz
    can be owed money in dollars while owing it in so'm, and a single netted figure
    would be struck at a kurs neither sotuv was agreed at."""
    parts = []
    for currency, amount in customer_balance_by_currency(customer):
        figure = som(abs(amount)) if currency == Currency.UZS else usd(abs(amount))
        parts.append(f"{'qarz' if amount > 0 else 'avans'} {figure}")
    if not parts:
        return f"{customer.name} · qarzsiz"
    return " · ".join([customer.name, *parts])


class ContractLineForm(PriceEntryFormMixin, forms.ModelForm):
    """One "Mahsulot" row on the kelishuv form.

    Neither a valyuta nor a kurs is asked for here any more. The narx is in the
    kelishuv's currency by definition, and a row settled in the currency it was
    agreed in needs no rate to say what is owed — so the row inherits both from the
    header instead of offering a picker that could disagree with it."""

    class Meta:
        model = ContractLine
        fields = ["brand", "kg", "price", "planned_trucks"]
        widgets = {
            "brand": forms.TextInput(attrs={"placeholder": "Masalan: 2102 repak"}),
            "kg": forms.NumberInput(attrs={"placeholder": "0"}),
            # data-currency-label marks the box as one whose unit follows the
            # header's Valyuta. The label below is the one the page is SERVED with —
            # right on a reload and after a validation error — and base.html retitles
            # it live from this attribute while the operator is still picking.
            "price": forms.NumberInput(attrs={"step": "0.0001", "placeholder": "0.0000",
                                              "data-currency-label": "1 kg narxi"}),
            # Per product, not per kelishuv: it used to be one box under the rows,
            # which could not say which marka of a two-marka kelishuv still owed a
            # truck. Optional — the target is often not known when the agreement
            # is signed.
            "planned_trucks": forms.NumberInput(attrs={"min": "1", "placeholder": "—"}),
        }

    def __init__(self, *args, currency=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The header's currency, handed down by the formset. The mixin has just
        # stripped the unit off the narx label because it assumes the operator picks
        # one; here it is known, so it goes back on.
        self.line_currency = currency or Currency.USD
        self.fields["price"].label = f"1 kg narxi ({currency_suffix(self.line_currency)})"
        # A so'm narx is a whole-so'm figure; the four decimals a dollar $/kg needs
        # (a cent per kg is dollars on a 24-tonne lot) read as a dollar box on it.
        if self.line_currency == Currency.UZS:
            self.fields["price"].widget.attrs.update({"step": "1", "placeholder": "0"})

    def clean_kg(self):
        kg = self.cleaned_data.get("kg")
        if kg is not None and kg <= 0:
            raise forms.ValidationError("Kg musbat bo'lishi kerak")
        return kg

    def clean_price(self):
        price = self.cleaned_data.get("price")
        if price is not None and price <= 0:
            raise forms.ValidationError("Narx musbat bo'lishi kerak")
        return price

    def clean_planned_trucks(self):
        count = self.cleaned_data.get("planned_trucks")
        if count is not None and count < 1:
            raise forms.ValidationError("Kamida 1 bo'lishi kerak")
        return count

    def clean(self):
        # Seeded before the mixin converts: it reads both out of cleaned_data, and
        # neither is a field on this form any more. An existing row keeps the kurs it
        # was booked at — recomputing it at today's rate would move a so'm figure that
        # was already agreed (see `latest_exchange_rate`).
        self.cleaned_data["currency"] = self.line_currency
        self.cleaned_data["exchange_rate"] = (
            self.instance.exchange_rate if self.instance.pk else latest_exchange_rate())
        cleaned = super().clean()
        kg = cleaned.get("kg")
        # Shrinking a product below what already went out would make qolgan negative.
        if self.instance.pk and kg is not None and kg < self.instance.shipped_kg:
            self.add_error("kg", f"Yuborilgan {self.instance.shipped_kg} kg dan kam bo'la olmaydi")
        return cleaned


class BaseContractLineFormSet(forms.BaseInlineFormSet):
    """A kelishuv is its products, so at least one row must survive, and the same
    brand must not appear twice — two rows of "2102 repak" would split one product's
    qolgan kg across two counters."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        brands, kept = [], 0
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            kept += 1
            brand = (form.cleaned_data.get("brand") or "").strip().casefold()
            if brand in brands:
                form.add_error("brand", "Bu mahsulot ro'yxatda bor")
            else:
                brands.append(brand)
        if not kept:
            raise forms.ValidationError("Kamida bitta mahsulot kiritilishi kerak")


ContractLineFormSet = forms.inlineformset_factory(
    Contract, ContractLine, form=ContractLineForm, formset=BaseContractLineFormSet,
    extra=1, min_num=0, can_delete=True)


class ContractChoiceSelect(forms.Select):
    """A kelishuv <select> whose options carry the currency each one was struck in,
    so the form's JS knows whether the to'lov about to be entered crosses a currency
    boundary — and therefore whether a kurs has to be asked for at all."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-currency"] = instance.currency
        return option


class ShipmentStatusForm(forms.ModelForm):
    class Meta:
        model = ShipmentStatus
        fields = ["name", "is_arrival"]


def _clean_number(value):
    """1000.000 → "1000", 1000.500 → "1000.5" — for data- attributes the JS reads."""
    text = f"{value}"
    return text.rstrip("0").rstrip(".") if "." in text else text


class ContractLineChoiceSelect(forms.Select):
    """A product <select> listing every kelishuv's products at once. Each option
    carries the kelishuv it belongs to, its qolgan kg and its agreed price, so the
    form's JS can hide the products of other kelishuvlar and prefill kg/narx —
    no dependent AJAX, and the server re-checks the pairing anyway."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-contract"] = str(instance.contract_id)
            option["attrs"]["data-remaining"] = _clean_number(instance.remaining_kg)
            option["attrs"]["data-price"] = _clean_number(instance.price)
        return option


class ShipmentForm(GroupedFieldsMixin, forms.ModelForm):
    class Meta:
        model = Shipment
        # No origin/destination: every run is Eron → O'zbekiston (model defaults).
        fields = ["contract", "status", "sent", "eta", "arrived", "qr_date", "logist",
                  "responsible", "driver_name", "driver_phone", "transport",
                  "container", "note"]
        widgets = {
            "contract": forms.Select(attrs={"data-contract-source": ""}),
            "sent": date_widget(),
            "eta": date_widget(),
            "arrived": date_widget(),
            # Only the PLANNED day. Whether the kod was actually handed over is the
            # QR button's to say, not this form's — see crm.views.shipment_set_qr.
            "qr_date": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            # Plain text on purpose. It used to carry a UZ/IR country picker that
            # uppercased and re-spaced what was typed, which read as "only these two
            # countries" — an operator holding a Turkish waybill had nowhere to put
            # it. A raqam is copied off the waybill, whichever country issued it, so
            # nothing here reformats or rejects it.
            "transport": forms.TextInput(attrs={
                "autocomplete": "off", "placeholder": "01 777 AAA · 34 ABC 123 · …"}),
            "container": forms.TextInput(attrs={
                "data-container-iso": "", "autocomplete": "off", "placeholder": "MSKU 123456 7"}),
            "responsible": forms.TextInput(attrs={
                "autocomplete": "off", "placeholder": "Yuk uchun javobgar xodim"}),
            "driver_name": forms.TextInput(attrs={
                "autocomplete": "off", "placeholder": "Masalan: Akmal aka"}),
            "driver_phone": phone_intl_widget(),
        }
        labels = {"sent": "Jo'natiladigan sana"}

    # Declaration order puts extra fields after the model's, which stranded the
    # advance at the bottom of the form — three fields about the logist, six fields
    # away from the logist picker. The generic template renders `form` in order, so
    # the order is set here rather than by hand-writing a template.
    field_order = ["contract", "status", "sent", "eta", "arrived", "qr_date",
                   "logist", "driver_advance",
                   "responsible", "driver_name", "driver_phone",
                   "transport", "container", "note"]

    # Boxed together under one legend. This modal ALSO carries a Valyuta and a kurs
    # on every Mahsulot row, so two unboxed currency labels would leave the operator
    # guessing which amount each belongs to. The box says: these four are one thing.
    field_groups = [("Logist va haydovchi avansi", ["logist", "driver_advance"])]

    # The advance is handed over as the truck leaves, so it belongs to the dispatch
    # form rather than to a later xarajat entry. Three fields because it is money:
    # a figure alone has no so'm twin and could never join a so'm total.
    driver_advance = forms.DecimalField(
        label="Haydovchiga avans ($)", max_digits=14, decimal_places=2,
        required=False, min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"placeholder": "0", "step": "0.01"}),
        help_text="Logist yo'lga chiqishda haydovchiga bergan pul — uning balansidan "
                  "yechiladi, kassadan qayta chiqmaydi. Faqat dollarda; so'm qiymati "
                  "shu logistga oxirgi to'lov kursida hisoblanadi.")
    def clean_driver_phone(self):
        return validate_intl_phone(self.cleaned_data.get("driver_phone"))

    def sync_driver_advance(self, shipment, user):
        """Create, update or remove the yuk's driver advance to match the form.

        One row, found by its flag rather than by category or logist: a logist may
        also have paid this load's bojxona, and rewriting that by accident would
        move money between two lines that have nothing to do with each other."""
        existing = shipment.expenses.filter(is_driver_advance=True).first()
        amount = self.cleaned_data.get("driver_advance")
        logist = self.cleaned_data.get("logist")
        if not amount or not logist:
            # Cleared, or the logist was removed — the advance goes with it, and
            # the yuk's tannarx drops back by that much.
            if existing:
                existing.delete()
            return None
        # Dollars, at the kurs that logist's own funding was converted at: the advance
        # is paid out of money we already sent them, so rating it at today's kurs
        # would give it a so'm value that money never had.
        #
        # And once booked, that kurs is the advance's own. Topping the logist up again
        # moves `latest_rate`, and this method runs on EVERY yuk save — so re-reading
        # it here restated the so'm value of cash handed to a driver weeks earlier,
        # triggered by an edit that had nothing to do with it. Moving the advance to a
        # different logist is a different event, and does take that logist's rate.
        kept = existing if existing and existing.logist_id == logist.pk else None
        rate = kept.exchange_rate if kept else logist.latest_rate
        usd_value, uzs_value = convert_pair(amount, Currency.USD, rate)
        fields = {
            "amount": usd_value, "amount_uzs": uzs_value,
            "currency": Currency.USD, "exchange_rate": rate,
            "logist": logist, "category": ShipmentExpense.Category.TRANSPORT,
            "date": shipment.sent or timezone.localdate(),
        }
        if existing:
            for key, value in fields.items():
                setattr(existing, key, value)
            existing.save()
            return existing
        return ShipmentExpense.objects.create(
            shipment=shipment, is_driver_advance=True, method=PayMethod.CASH,
            note="Haydovchiga avans", created_by=user, **fields)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Required on the form, still nullable on the model: a yuk with no
        # departure date fell out of the Oylik hisobot "jo'natilgan" count, but
        # rows imported before this rule must stay editable.
        self.fields["sent"].required = True
        # A kelishuv with every kg already on the road has nothing left to load, so
        # it drops off the new-yuk list — but stays when editing its own yuk.
        base = (Contract.objects.select_related("partner")
                .prefetch_related("lines__shipment_lines"))
        self.fields["contract"].queryset = _keep_if(
            base, lambda c: c.remaining_kg > 0, self.instance.contract_id)
        self.fields["contract"].label_from_instance = contract_option_label
        self.fields["logist"].empty_label = "Logistsiz"
        # Yetib kelgan sana is offered only once the yuk HAS arrived — a date beside
        # a load still on the road is an invitation to type one, and a yuk carrying an
        # arrival date while its holat says otherwise would sit in the ombor
        # (arrived_lots filters on `arrived`, nothing else) with its tannarx already
        # in stock valuation. Whether a load has arrived stays the holat's answer;
        # this field only corrects WHEN. It is the date auto-stamped as "today" by the
        # status change, which is wrong every time a truck is marked in a day late.
        # Required-ness is decided in `_clean_arrived` against the SUBMITTED holat,
        # not here: a yuk being moved back onto the road must not be blocked by a date
        # that is about to be cleared.
        if not (self.instance.pk and self.instance.arrived):
            self.fields.pop("arrived")
        _group_thousands(self.fields["driver_advance"])
        # Editing a yuk shows the advance already recorded — otherwise saving an
        # untouched form would wipe it.
        if self.instance.pk and not self.is_bound:
            advance = self.instance.expenses.filter(is_driver_advance=True).first()
            if advance:
                self.initial.setdefault("driver_advance", advance.amount)

    # No clean_transport: the raqam is free text, and Django's CharField already
    # trims the spaces around it. There used to be a plate-shaped regex here, which
    # rejected anything that was not 5–12 alphanumerics with a digit — no help to an
    # operator holding a waybill that says something else.

    def clean_container(self):
        """Tidied, never rejected: uppercased and grouped when it looks like ISO
        6346, left as typed otherwise. The duplicate check that lived here is gone
        too — the same container legitimately comes back on a later yuk."""
        return normalize_container(self.cleaned_data.get("container"))

    def clean(self):
        cleaned = super().clean()
        sent, eta = cleaned.get("sent"), cleaned.get("eta")
        if sent and eta and eta < sent:
            self.add_error("eta", "Kelish sanasi jo'natish sanasidan oldin bo'la olmaydi")
        # An advance with nobody behind it has no balance to come out of, and would
        # silently become an ordinary kassa expense — the exact double-count this
        # whole feature exists to prevent. No kurs check: it is not asked for, it
        # comes off the logist's own funding.
        if cleaned.get("driver_advance") and not cleaned.get("logist"):
            self.add_error("logist", "Avansni kim berdi? Logistni tanlang")
        self._clean_arrived(cleaned)
        return cleaned

    def _clean_arrived(self, cleaned):
        """Guard the arrival date, which is not a label on this screen: `arrived_lots`
        filters on it and nothing else, so it decides what is on the shelf, what a
        lot's kg count under in a month, and the FIFO order sotuvlar draw from.

        Required only while the holat says arrived. Otherwise moving a yuk back onto
        the road would be blocked by a date field that is about to be cleared anyway —
        the view clears it, since the holat is what decides WHETHER a yuk landed."""
        if "arrived" not in self.fields:
            return
        arrived, status = cleaned.get("arrived"), cleaned.get("status")
        if status is not None and status.is_arrival and not arrived:
            self.add_error("arrived", "Yetib kelgan sanani kiriting.")
            return
        if not arrived:
            return
        if arrived > timezone.localdate():
            self.add_error("arrived", "Yetib kelgan sana kelajakda bo'la olmaydi")
        sent = cleaned.get("sent")
        if sent and arrived < sent:
            self.add_error(
                "arrived",
                f"Yetib kelgan sana jo'natish sanasidan ({sent}) oldin bo'la olmaydi")


class ShipmentLineForm(PriceEntryFormMixin, forms.ModelForm):
    """One product on the truck.

    Like the kelishuv row, it neither asks for a valyuta nor a kurs: a truck is
    priced in the currency of the kelishuv it is drawn from, and that is the currency
    its goods land in `shipped_value_own`."""

    #: blank narx means "use the kelishuv's" — see ShipmentLine.unit_price
    allow_blank = True

    class Meta:
        model = ShipmentLine
        fields = ["contract_line", "kg", "price"]
        widgets = {"contract_line": ContractLineChoiceSelect(attrs={"data-line-source": ""})}
        labels = {"kg": "Yuboriladigan kg", "price": "1 kg narxi"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Likewise per product: a fully-shipped line is not offered as a lot, but
        # the line already on this yuk stays selectable while editing it.
        base = (ContractLine.objects.select_related("contract")
                .prefetch_related("shipment_lines")
                .order_by("contract__code_slug", "contract__code_number", "position", "id"))
        self.fields["contract_line"].queryset = _keep_if(
            base, lambda ln: ln.remaining_kg > 0, self.instance.contract_line_id)
        # Everything needed to pick the right row without leaving the dropdown:
        # which kelishuv, which marka, how much is still owed, at what price.
        # Priced in its own kelishuv's currency — a so'm line printed with a dollar
        # sign in front of it is the exact lie this phase is removing.
        self.fields["contract_line"].label_from_instance = (
            lambda ln: f"{ln.contract.code} · {ln.brand} · "
                       f"{_clean_number(ln.remaining_kg)} kg qolgan · "
                       f"{rate(ln.price, ln.price_uzs, ln.currency)}")

    def clean_kg(self):
        kg = self.cleaned_data.get("kg")
        if kg is not None and kg <= 0:
            raise forms.ValidationError("Kg musbat bo'lishi kerak")
        return kg

    def clean_price(self):
        price = self.cleaned_data.get("price")
        if price is not None and price <= 0:
            raise forms.ValidationError("Narx musbat bo'lishi kerak")
        return price

    def clean(self):
        # Seeded before the mixin converts, same as the kelishuv row — except the
        # currency comes from whichever product this row picked, since the truck's
        # kelishuv is only settled once `contract_line` has been cleaned.
        line = self.cleaned_data.get("contract_line")
        self.cleaned_data["currency"] = (
            line.contract.currency if line else Currency.USD)
        self.cleaned_data["exchange_rate"] = (
            self.instance.exchange_rate if self.instance.pk else latest_exchange_rate())
        return super().clean()


class BaseShipmentLineFormSet(forms.BaseInlineFormSet):
    """Guards the three ways a truck's product rows can be wrong: empty, carrying
    the same product twice, or carrying more than the kelishuv has left."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [f for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")
                and f.cleaned_data.get("contract_line")]
        if not rows:
            raise forms.ValidationError("Kamida bitta mahsulot kiritilishi kerak")

        wanted = {}
        for form in rows:
            line = form.cleaned_data["contract_line"]
            if line.pk in wanted:
                form.add_error("contract_line", "Bu mahsulot ro'yxatda bor")
                continue
            wanted[line.pk] = (form, line, form.cleaned_data.get("kg") or Decimal("0"))

        contracts = {line.contract_id for _, line, _ in wanted.values()}
        if len(contracts) > 1:
            raise forms.ValidationError(
                "Bitta yukdagi mahsulotlar bitta kelishuvga tegishli bo'lishi kerak")

        # What this truck already books against each product frees that much back up.
        already = {}
        if self.instance.pk:
            for existing in self.instance.lines.all():
                already[existing.contract_line_id] = existing.kg

        for form, line, kg in wanted.values():
            left = line.remaining_kg + already.get(line.pk, Decimal("0"))
            if kg > left:
                form.add_error(
                    "kg", f"Yuk miqdori qolgan kg dan oshmasligi kerak ({left} kg)")


ShipmentLineFormSet = forms.inlineformset_factory(
    Shipment, ShipmentLine, form=ShipmentLineForm, formset=BaseShipmentLineFormSet,
    extra=1, min_num=0, can_delete=True)


class ShipmentExtendForm(forms.Form):
    new_eta = forms.DateField(label="Yangi kelish sanasi",
                              widget=date_widget())
    reason = forms.CharField(label="Kechikish sababi", max_length=255)


class ShipmentDelayForm(forms.ModelForm):
    """Correct an uzaytirish that was entered wrong.

    Only the LATEST one may have its DATE changed. Extensions are a chain — each
    row's `old_eta` is the row before it's `new_eta`, and the yuk's own eta is
    simply the last `new_eta` — so moving a date in the middle would either break
    that chain or rewrite every row after it, and rewriting rows that recorded what
    happened on the day is not correcting them.

    The sabab carries none of that, so it stays correctable on every row: a reason
    typed in haste is the thing most often wrong, and it is the half a kechikish
    report is read for."""

    class Meta:
        model = ShipmentDelay
        fields = ["new_eta", "reason"]
        labels = {"new_eta": "Yangi kelish sanasi", "reason": "Kechikish sababi"}

    def __init__(self, *args, latest=True, **kwargs):
        super().__init__(*args, **kwargs)
        if latest:
            self.fields["new_eta"].widget = date_widget()
        else:
            # Not the last word on this yuk's eta, so the date is not this form's to
            # move. Dropped rather than disabled: a disabled field posts nothing and
            # a ModelForm would then write it away as empty.
            self.fields.pop("new_eta")


class ShipmentQrForm(forms.Form):
    """When the kod actually reached the driver.

    Asked rather than assumed to be today: the mark is often entered a day or two
    after the fact, and a kod recorded on the day someone got round to clicking is
    a date that says nothing. `qr_date` is not the default either — that field is
    the plan, and the whole reason this one exists is that the two differ.

    Optional on purpose: submitting it empty is how the mark comes back off. The
    press used to be a toggle, and a QR marked on the wrong yuk out of a row of
    near-identical trucks still needs a way back that is not a full edit."""

    qr_given = forms.DateField(
        label="QR kod berilgan sana", required=False, widget=date_widget(),
        help_text="Kod haqiqatda qo'lga tegan kun. "
                  "Belgini olish uchun — bo'sh qoldiring.")


class ShipmentDriverForm(forms.ModelForm):
    """Who is driving this yuk and what it is riding in — the ONLY part of a load a
    tarjimon may change.

    A separate form rather than a subset of ShipmentForm because the restriction has
    to live in the field list itself. A ModelForm only ever writes the fields it
    declares, so a tarjimon who posts `contract=7&status=3` alongside the four fields
    here changes nothing: the extra keys are not bound and never reach the instance.
    Reusing ShipmentForm and hiding the other inputs in the template would leave
    exactly that hole open, since the template does not decide what a form saves.

    Not `logist`: an outside party who holds our money is a financial relationship,
    not driver detail, and the same rule keeps every money field off this form."""

    class Meta:
        model = Shipment
        fields = ["driver_name", "driver_phone", "transport", "container"]
        widgets = {
            # The same widgets ShipmentForm gives these four, so the two forms cannot
            # disagree about what a valid raqam looks like — see the note there on why
            # nothing here reformats or rejects what is copied off a waybill.
            "driver_name": forms.TextInput(attrs={
                "autocomplete": "off", "placeholder": "Masalan: Akmal aka"}),
            "driver_phone": phone_intl_widget(),
            "transport": forms.TextInput(attrs={
                "autocomplete": "off", "placeholder": "01 777 AAA · 34 ABC 123 · …"}),
            "container": forms.TextInput(attrs={
                "data-container-iso": "", "autocomplete": "off",
                "placeholder": "MSKU 123456 7"}),
        }

    def clean_container(self):
        return normalize_container(self.cleaned_data.get("container"))


class ShipmentLegForm(forms.ModelForm):
    class Meta:
        model = ShipmentLeg
        fields = ["from_location", "to_location", "transport", "container",
                  "departed", "arrived", "note"]
        widgets = {
            "departed": date_widget(),
            "arrived": date_widget(),
            "from_location": forms.TextInput(attrs={"placeholder": "Masalan: Tehron"}),
            "to_location": forms.TextInput(attrs={"placeholder": "Masalan: Chegara"}),
            # Free text, same as the yuk's own raqam — see ShipmentForm.
            "transport": forms.TextInput(attrs={
                "autocomplete": "off",
                "placeholder": "Haydovchi ismi yoki mashina raqami"}),
            "container": forms.TextInput(attrs={
                "data-container-iso": "", "autocomplete": "off", "placeholder": "MSKU 123456 7"}),
        }

    def clean_container(self):
        return normalize_container(self.cleaned_data.get("container"))

    def clean(self):
        cleaned = super().clean()
        dep, arr = cleaned.get("departed"), cleaned.get("arrived")
        if dep and arr and arr < dep:
            self.add_error("arrived", "Yetib kelgan sana jo'natilgan sanadan oldin bo'la olmaydi")
        return cleaned


def credited_to_partner(data, contract):
    """What a hamkor to'lov being typed would actually CREDIT the kelishuv with, in the
    kelishuv's own currency.

    Neither the bank's foiz nor the vositachi cut shortens it: both ride on top of
    what we send, so a hamkor paid 1 000 is credited 1 000 and the kassa is out the
    extra (`SupplierPayment.credited_amount`, `total_out`).

    Still routed through an unsaved row rather than returning the typed summa
    outright, so the form and the model can never hold two opinions about what the
    hamkor received — whatever that rule turns out to be, this reads it from the one
    place that states it."""
    row = SupplierPayment(
        amount=data.get("amount") or Decimal("0"),
        amount_uzs=data.get("amount_uzs") or Decimal("0"),
        exchange_rate=data.get("exchange_rate") or LEGACY_RATE,
        method=data.get("method") or "",
        fee_percent=data.get("fee_percent") or Decimal("0"))
    return row.credited_amount_uzs if contract.is_som else row.credited_amount


class SupplierPaymentForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """A to'lov against one kelishuv.

    The kurs is asked for only when the money crosses a currency boundary — paying a
    so'm kelishuv in so'm settles it at face value, and there is nothing to convert.
    It IS asked when the two differ, because then the rate is what decides how much
    of the qarz the money actually clears.

    A rate entered that way is then frozen: the to'lov keeps the kurs it was made at
    however the market moves afterwards, so a kelishuv that was square last week
    cannot re-open itself this week. Correcting it is a deliberate edit of the
    to'lov, not something that happens on its own.

    `fee_bearer` is deliberately absent from the fields, the same way `KapitalForm`
    leaves it out. On money going out to a hamkor the bank's cut is always ours: the
    hamkor is owed a figure and has to receive that figure, so the foiz rides on top
    and the kassa is out the extra. Asking per to'lov offered an answer that is not
    ours to give — and picking the other one credited the hamkor less than we sent,
    which is what left kelishuvlar short by the foiz. A blank `fee_bearer` means
    exactly this via `CashEntry.default_fee_bearer`."""

    class Meta:
        model = SupplierPayment
        fields = ["contract", "contract_line", "date", "currency", "amount",
                  "exchange_rate", "commission_percent", "method", "fee_percent",
                  "note"]
        widgets = {
            "date": date_widget(),
            # data-contract-source as well as -currency: the product list below is
            # every kelishuv's products at once, and this is what narrows it.
            "contract": ContractChoiceSelect(attrs={"data-contract-currency": "",
                                                    "data-contract-source": ""}),
            "contract_line": ContractLineChoiceSelect(attrs={"data-line-source": ""}),
            "commission_percent": forms.NumberInput(attrs={
                "data-commission-percent": "", "step": "0.01", "min": "0", "max": "100",
                "placeholder": "0"}),
        }
        labels = {"amount": "Hamkor oladigan summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The kassa total is driven by this, so the operator should see it named.
        self.fields["amount"].widget.attrs["data-commission-base"] = ""
        self.fields["exchange_rate"].help_text = (
            "Faqat kelishuv valyutasidan boshqa valyutada to'lanayotganda kerak")
        # Same rich option as the yuk form: which kelishuv, whose, what marka,
        # what is still owed in goods and at what price. A fully-paid kelishuv has
        # nothing left to pay, so it drops off — but stays when editing its own
        # to'lov.
        base = (Contract.objects.select_related("partner")
                .prefetch_related("lines__shipment_lines", "supplier_payments"))
        self.fields["contract"].queryset = _keep_if(
            base, lambda c: c.payable_left_own > 0, self.instance.contract_id)
        # Money, not kg, in the tail here: it is what this form is about to spend
        # down, and it is the ceiling the form will check the entry against.
        self.fields["contract"].label_from_instance = (
            lambda c: contract_option_label(c, payable=True))

        # Which product the money is against. Every selectable kelishuv's products
        # are listed at once and the form's JS drops the ones belonging to other
        # kelishuvlar — the same no-AJAX arrangement the yuk form uses. `clean`
        # re-checks the pairing, because the client is not the authority on it.
        self.fields["contract_line"].queryset = (
            ContractLine.objects.filter(contract__in=self.fields["contract"].queryset)
            .select_related("contract").order_by("contract_id", "position", "id"))
        self.fields["contract_line"].label_from_instance = (
            lambda ln: f"{ln.brand} · {_clean_number(ln.kg)} kg")
        # A real choice again, and named as one. It was demoted to a bare prompt
        # while `clean` refused it — but a to'lov that names no marka is now the
        # ZAKLAD, and `allocate_supplier_payment` splits it across the kelishuv by
        # mashina count. Left as "Mahsulotni tanlang" the option read as an omission
        # the form was about to complain about, which is exactly what it did.
        #
        # A single-product kelishuv never sees this: the form fills that one in for
        # them (and the JS preselects it).
        self.fields["contract_line"].empty_label = "Butun kelishuv (zaklad)"
        self.fields["contract_line"].help_text = (
            "Pul qaysi mahsulot uchun ketganini belgilang. "
            "Bo'sh qoldirilsa — zaklad: mashinalar soniga qarab bo'linadi")

    def clean_commission_percent(self):
        return _clean_percent(self.cleaned_data.get("commission_percent"))

    def clean(self):
        # Seeded before the mixin converts. Paying a kelishuv in its own currency
        # settles it at face value, so no kurs is ASKED for — the row still gets one
        # (its own on an edit, the last one entered otherwise) because both money
        # columns have to hold something for the kassa to add up.
        #
        # Only a missing rate is filled in. One the operator did supply stands, even
        # here: the box is hidden rather than forbidden, and choosing the rate a
        # to'lov is booked at is theirs to make. When the currencies differ nothing is
        # seeded at all, and the mixin's "Dollar kursini kiriting" is what enforces it.
        contract = self.cleaned_data.get("contract")
        typed_rate = self.cleaned_data.get("exchange_rate") or Decimal("0")
        if (contract is not None and typed_rate <= 0
                and self.cleaned_data.get("currency") == contract.currency):
            self.cleaned_data["exchange_rate"] = (
                self.instance.exchange_rate if self.instance.pk else latest_exchange_rate())
        cleaned = super().clean()
        contract, amount = cleaned.get("contract"), cleaned.get("amount")

        # Which product the money is for. The client-side filter is a convenience,
        # not the authority — a stale page or a hand-built POST can still pair a
        # product with someone else's kelishuv.
        line = cleaned.get("contract_line")
        if line is not None and contract is not None and line.contract_id != contract.pk:
            self.add_error("contract_line", "Bu mahsulot tanlangan kelishuvda yo'q")
        elif contract is not None:
            lines = list(contract.lines.all())
            if len(lines) == 1:
                # Nothing to choose: one product IS the kelishuv, so the operator is
                # not asked and the to'lov still records which marka it bought.
                cleaned["contract_line"] = lines[0]
            # Blank on a multi-marka kelishuv is NOT an omission to complain about.
            # It is the zaklad — money handed over before anyone knows which marka
            # it will buy — and `allocate_supplier_payment` splits it by mashina
            # count. Demanding a marka here made that whole branch unreachable: the
            # model would place it correctly and the form never let it through.

        # Paying before a yuk is sent is normal (avans), so the ceiling is the whole
        # kelishuv's value, not the goods shipped so far. The cap is on what the
        # hamkor RECEIVES — the middleman's cut rides on top and is not part of it,
        # and a bank foiz they carry comes out of it (see `credited_to_partner`).
        #
        # Measured in the KELISHUV's currency, not the dollar column: a so'm kelishuv
        # paid off in so'm still leaves a dollar remainder whenever the kurs has moved
        # since it was struck, and the form would go on demanding money that is not
        # owed. The to'lov's own converted value is what settles it.
        if contract and amount is not None and not self.errors:
            paid = credited_to_partner(cleaned, contract)
            left = contract.payable_left_own
            # Editing this same to'lov: what it already credited comes back off the
            # kelishuv first, or the row would be weighed against a qoldiq it is
            # itself part of. Its CREDITED figure, the one `payable_left_own` took
            # away — adding back the gross would leave the ceiling a foiz too loose.
            if self.instance.pk and self.instance.contract_id == contract.pk:
                left += (self.instance.credited_amount_uzs if contract.is_som
                         else self.instance.credited_amount)
            if paid is not None and paid > left:
                shown = som(left) if contract.is_som else usd(left)
                self.add_error(
                    "amount",
                    f"Kelishuv qiymatidan oshib ketdi (to'lash mumkin: {shown})")
        return cleaned


def _stock_brand_choices():
    """The markalar with granula physically on the shelf, labelled the informative
    way the yuk and kelishuv dropdowns are: marka · kelishuv kod · qolgan kg ·
    tannarx, with no filler words. `_clean_number` keeps kg readable ("24000", not
    Decimal.normalize()'s "2.4E+4").

    Anything bronned is NAMED after the on-hand figure as a warning, never deducted
    from it: the granula may be sold to whoever is in front of the operator, bron or
    no bron."""
    return [
        (row["brand"],
         f"{row['brand']} · {', '.join(row['codes'])} · "
         f"{_clean_number(row['on_hand'])} kg omborda"
         + (f" ({_clean_number(row['reserved'])} kg bronlangan)" if row["reserved"] else "")
         + f" · {_clean_number(row['cost'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))} $/kg")
        for row in brand_stock_costed() if row["on_hand"] > 0
    ]


def _customer_phone_field(field):
    """Put the telefon beside the ism in a mijoz picker, and take the "---------"
    off the empty row.

    Two mijoz sharing a name is ordinary — a family, or the same person entered
    twice — and the ism on its own leaves the operator guessing which row is the
    one standing at the counter. The raqam is what they have in front of them, so
    it is what tells the two apart without closing the modal to go and look.

    Not `customer_option_label`: that one carries the ostatka, which is the figure
    a to'lov is taken against. A sotuv is not being taken against anything yet, and
    a qarz on the option there would read as a price.

    empty_label "" and not None: None DELETES the empty row, which would leave the
    first mijoz on the list selected on a form nobody has touched — a sotuv booked
    against whoever sorts first. Blank keeps the row, so the field is still empty
    until someone picks, and the searchable picker skips a value-less, label-less
    option by design — so the dashes stop showing as the box's own content and the
    data-placeholder shows through instead."""
    field.empty_label = ""
    field.label_from_instance = (
        lambda customer: f"{customer.name} · {customer.phone}"
        if customer.phone else customer.name)


def _customer_picker_widget():
    """The mijoz <select> on the sotuv forms: searchable, with an inline quick-add.

    The list runs to hundreds of names and a native select can only be scrolled,
    so finding one meant hunting an alphabetical wall. `data-combobox` turns it
    into the same type-to-filter picker the Mijoz filters on Mijoz to'lovlari and
    Bronlar already use — and it filters on the whole option text, which
    `_customer_phone_field` has already made "ism · telefon", so the raqam finds
    them too when two mijoz share a name.

    Progressive enhancement: the native select stays in the DOM and still submits,
    which is what lets CustomerBronSelect keep stamping its options and the
    quick-add keep appending to it, both untouched.

    A function rather than one shared widget: a widget carries per-form state —
    its choices, and the CustomerBronSelect that BronDrawFormMixin swaps in — so
    three forms sharing one object would tread on each other."""
    return forms.Select(attrs={
        "data-combobox": "",
        # What the box says while it is empty. The blank row it stands in for
        # carries no label at all now — see _customer_phone_field.
        "data-placeholder": "Mijozni tanlang",
        "data-quick-add-url": reverse_lazy("customer_quick_create"),
        "data-quick-add-label": "Yangi mijoz",
    })


class SaleLineForm(forms.Form):
    """One marka on a sotuv.

    Deliberately NOT a ModelForm: a row is not a Sale. The view splits each row FIFO
    across the lots that actually hold that marka, so one row becomes as many Sale
    objects as it takes lots to fill — which is why the narx lives here, per marka,
    while the valyuta and the kurs stay on the header. One sotuv is one deal, struck
    in one currency; the price is the part that differs per product."""

    brand = forms.ChoiceField(label="Marka (ombordan)")
    kg = forms.DecimalField(label="Sotilgan kg", max_digits=12, decimal_places=3)
    price = forms.DecimalField(label="1 kg sotuv narxi", max_digits=14, decimal_places=4)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["brand"].choices = _stock_brand_choices()
        # Read by the Brondan ushlansin JS, which now asks whether ANY row's marka is
        # one this mijoz holds a bron for.
        self.fields["brand"].widget.attrs["data-bron-brand"] = ""
        _group_thousands(self.fields["kg"])
        _group_thousands(self.fields["price"])

    def clean_kg(self):
        kg = self.cleaned_data.get("kg")
        if kg is not None and kg <= 0:
            raise forms.ValidationError("Kg musbat bo'lishi kerak")
        return kg

    def clean_price(self):
        price = self.cleaned_data.get("price")
        if price is not None and price <= 0:
            raise forms.ValidationError("Narx musbat bo'lishi kerak")
        return price


class BaseSaleLineFormSet(forms.BaseFormSet):
    """Guards the three ways a sotuv's marka rows can be wrong: empty, carrying the
    same marka twice, or asking for more than is on the shelf."""

    def rows(self):
        """The rows that mean something — filled in and not struck out."""
        return [f for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")
                and f.cleaned_data.get("brand")]

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = self.rows()
        if not rows:
            raise forms.ValidationError("Kamida bitta mahsulot kiritilishi kerak")

        # One row per marka. Two rows of the same granula would each be checked
        # against the whole shelf and pass, then take twice what is there — and the
        # operator meant one line anyway.
        wanted = {}
        for form in rows:
            brand = form.cleaned_data["brand"]
            if brand in wanted:
                form.add_error("brand", "Bu marka ro'yxatda bor")
                continue
            wanted[brand] = (form, form.cleaned_data.get("kg") or Decimal("0"))

        for brand, (form, kg) in wanted.items():
            # The shelf is the only ceiling. A bron is a promise between the operator
            # and a mijoz, and the operator is the one who decides whether to keep it
            # today — granula refused to a buyer standing at the counter is a sale
            # lost to a rule that was never the mijoz's.
            available = brand_on_hand_kg(brand)
            if kg > available:
                form.add_error(
                    "kg", f"Ombor qoldig'idan oshmasligi kerak "
                          f"({_clean_number(available)} kg)")


SaleLineFormSet = forms.formset_factory(
    SaleLineForm, formset=BaseSaleLineFormSet, extra=1, can_delete=True)


class SaleCreateForm(BronDrawFormMixin, InheritedRateMixin, forms.ModelForm):
    """The header of a sotuv: who is buying, in what currency, on what terms.

    WHAT is being sold lives in `SaleLineFormSet` beside it — a sotuv may carry
    several markalar, and one trip to the counter should be one entry rather than
    one modal per product. Each row is then consumed from the oldest arrived lots
    first (FIFO), one Sale row per lot slice so every slice keeps its own lot's
    landed cost.

    No PriceEntryFormMixin here any more: there is no single narx to convert on the
    header now that each marka carries its own. The valyuta stays (one deal, one
    currency) and `InheritedRateMixin` keeps filling the kurs, so the view has both
    halves it needs to convert each row's price."""

    class Meta:
        model = Sale
        fields = ["customer", "currency", "exchange_rate",
                  "date", "debt_deadline", "note"]
        widgets = {
            "date": date_widget(),
            "debt_deadline": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "customer": _customer_picker_widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].widget.attrs["data-money-currency"] = ""
        # A sotuv is money moving too, and it draws granula off the shelf — neither
        # can happen tomorrow. `debt_deadline` beside it is left alone: a muddat is a
        # future date by definition.
        no_future_date(self.fields["date"])
        _customer_phone_field(self.fields["customer"])


class SaleLotForm(BronDrawFormMixin, InheritedRateMixin,
                  PriceEntryFormMixin, forms.ModelForm):
    """Sale from ONE chosen lot, entered from inside a marka in the ombor. The same
    granula can sit in several lots at different landed costs, so picking the lot
    has to beat FIFO here — otherwise you could never sell the dearer one. The lot
    rides along in a hidden field because the modal posts to a bare path."""

    lot = forms.ModelChoiceField(queryset=Shipment.objects.none(),
                                 widget=forms.HiddenInput())

    class Meta:
        model = Sale
        fields = ["lot", "customer", "kg", "currency", "price", "exchange_rate",
                  "date", "debt_deadline", "note"]
        widgets = {
            "date": date_widget(),
            "debt_deadline": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "customer": _customer_picker_widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = arrived_lots()
        _customer_phone_field(self.fields["customer"])
        # No marka picker here — the lot decided it — so the Brondan ushlansin box
        # carries the brand itself for the JS that shows or hides it.
        lot = self.initial.get("lot") or self.data.get("lot")
        chosen = arrived_lots().filter(pk=lot).first() if lot else None
        if chosen is not None:
            self.fields["draw_from_bron"].widget.attrs["data-bron-fixed-brand"] = chosen.brand

    def clean(self):
        cleaned = super().clean()
        lot, kg = cleaned.get("lot"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if lot and kg is not None and kg > 0 and kg > lot.available_kg:
            # One ceiling, and it is a physical one: this lot's own kg. A bron on the
            # marka used to be a second ceiling here and is not any more — it names
            # who is waiting, it does not refuse the sotuv.
            self.add_error("kg", f"Bu lotning qoldig'idan oshmasligi kerak "
                                 f"({_clean_number(lot.available_kg)} kg)")
        return cleaned


class SaleForm(InheritedRateMixin, PriceEntryFormMixin, forms.ModelForm):
    class Meta:
        model = Sale
        fields = ["customer", "line", "kg", "currency", "price", "exchange_rate",
                  "date", "debt_deadline", "note"]
        widgets = {
            "date": date_widget(),
            "debt_deadline": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "customer": _customer_picker_widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["line"].queryset = arrived_lots()
        _customer_phone_field(self.fields["customer"])

    def clean(self):
        cleaned = super().clean()
        line, kg = cleaned.get("line"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if line and line.arrived is None:
            self.add_error("line", "Faqat kelgan (arrived) lotdan sotish mumkin")
        if line and line.arrived is not None and kg is not None and kg > 0:
            # Measured against the MARKA, not against this one lot. The same granula
            # sits in several lots at once, and after fifty sotuvlar the lot a sotuv
            # happens to be attached to is almost always empty — checking it refused
            # every correction to an old sotuv while the ombor was full of the stuff.
            # Where the extra kg come from is the replay's problem, not the form's.
            available = brand_available_kg(line.contract_line.brand,
                                           excluding=self.instance)
            if kg > available:
                self.add_error("kg", f"Bu markadan omborda {_clean_number(available)} "
                                     f"kg bor — undan oshmasligi kerak")
        return cleaned


class ReservationForm(PriceEntryFormMixin, forms.ModelForm):
    """A bron is taken against a MARKA, not a lot: whichever kelishuv's truck lands
    first with that granula fills it. So the choice list is every marka still coming
    on a kelishuv plus everything already in the ombor — deliberately including
    markalar with zero stock today, since booking ahead is the point.

    There is no kg ceiling here, and none against what is already bronned either.
    Reserving 40 000 kg against a kelishuv that has not shipped yet is normal
    business, and since a bron holds nothing back, two mijoz booking the same kg is
    a fact the operator settles at hand-over rather than an error to refuse now."""

    #: the narx is optional on a bron — the price can be agreed later
    allow_blank = True

    brand = forms.ChoiceField(label="Marka")

    class Meta:
        model = Reservation
        fields = ["customer", "brand", "kg", "currency", "price", "exchange_rate", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stock = {row["brand"]: row for row in brand_stock_costed()}
        choices = []
        for brand in bron_brands():
            row = stock.get(brand)
            if row:
                hint = f"omborda {_clean_number(row['on_hand'])} kg"
                if row["reserved"]:
                    hint += f", {_clean_number(row['reserved'])} kg bronlangan"
            else:
                hint = "hozircha omborda yo'q — kelganda beriladi"
            choices.append((brand, f"{brand} · {hint}"))
        self.fields["brand"].choices = choices
        # An existing bron keeps its marka even if that marka has since dropped off
        # the list, so editing one never silently rewrites what was reserved.
        if self.instance.pk and self.instance.brand not in dict(choices):
            self.fields["brand"].choices = [
                (self.instance.brand, self.instance.brand)] + choices

    def clean_kg(self):
        kg = self.cleaned_data.get("kg")
        if kg is not None and kg <= 0:
            raise forms.ValidationError("Kg musbat bo'lishi kerak")
        if kg is not None and self.instance.pk and kg < self.instance.fulfilled_kg:
            raise forms.ValidationError(
                f"Allaqachon {_clean_number(self.instance.fulfilled_kg)} kg berilgan — "
                "bundan kam qilib bo'lmaydi")
        return kg


def own_side_pair(currency, pair):
    """The half of a (usd, so'm) pair that `currency` is kept in."""
    usd, uzs = pair
    return uzs if currency == Currency.UZS else usd


def returns_without(batch):
    """`returns`, read as if `batch` had never been entered.

    Every figure a vazvrat form measures against — `returnable_kg`, `remaining_own`,
    `net_total` — is summed off `sale.returns.all()` in Python, so filtering the
    prefetch is enough to answer the one question editing asks: what would this
    sotuv look like if THIS visit were taken back out?

    Without it a vazvrat could not even be saved unchanged. The ceiling on each row
    already has the batch's own kg subtracted from it, so re-submitting the same
    number reads as returning it twice.

    `None` means "as things stand", which is what creating a new vazvrat wants."""
    rows = Return.objects.all()
    if batch is not None:
        rows = rows.exclude(batch_id=getattr(batch, "pk", batch))
    return Prefetch("returns", queryset=rows)


def parse_return_rows(post):
    """[(sale_id, kg)] typed into the vazvrat table — boxes named `ret_<sale_id>`.

    Read straight off POST rather than through a formset, the same way the to'lov
    modal reads its taqsimlash boxes: these are not free-form rows the operator adds
    and removes, they are a fixed list drawn from the mijoz's own sotuvlar, each one
    identified by the sotuv it reverses. Blanks and zeros are dropped, so returning
    one line out of forty submits one row.

    Nothing here is trusted — the ids are looked up against the chosen mijoz's own
    sotuvlar in `ReturnBatchForm.clean`, so a tampered id finds nothing."""
    rows = []
    for key, value in post.items():
        if not key.startswith("ret_"):
            continue
        value = (value or "").strip().replace(" ", "").replace(" ", "")
        if not value:
            continue
        try:
            sale_id = int(key[len("ret_"):])
            kg = Decimal(value.replace(",", "."))
        except (ValueError, ArithmeticError):
            continue
        if kg > 0:
            rows.append((sale_id, kg))
    return rows


class ReturnBatchForm(forms.ModelForm):
    """Vazvrat: what a mijoz brought back in one visit, and where the money goes.

    The operator picks the MIJOZ and then types kg against that mijoz's SOTUVLAR —
    never against a bare marka. That is not a layout preference. The same marka goes
    out to the same mijoz at several narx (one of them across 23 sotuv from $1.4167
    to $1.65), so "5 000 kg 2102 campaund qaytdi" has no value until it is tied to
    the row it came off. Tying it to the row also means the narx is never typed, so
    a vazvrat cannot be worth more per kg than the sotuv it reverses.

    The money follows ONE rule, and that rule covers all three cases an operator
    thinks in: the vazvrat cancels open qarz first, and only what is left over is
    money the mijoz had already paid and is owed back. Goods still unpaid therefore
    just shrink the qarz and move no money; goods paid in full hand the whole value
    back; a part-paid sotuv splits itself without anybody having to choose.

    Split per CURRENCY, because a visit can hand back a dollar sotuv and a so'm
    sotuv together and the two never blend — see `ReturnSettlement`."""

    SETTLE_ADVANCE = "advance"
    SETTLE_CASH = "cash"
    SETTLE_OWED = "owed"

    settle = forms.ChoiceField(
        label="Mijoz to'lab bo'lgan qismi",
        choices=[
            (SETTLE_ADVANCE, "Mijoz avansida qolsin — keyingi savdolariga ishlatiladi"),
            (SETTLE_CASH, "Hozir kassadan qaytarildi"),
            (SETTLE_OWED, "Hozir pul yo'q — keyin qaytaramiz"),
        ],
        initial=SETTLE_ADVANCE,
        widget=forms.RadioSelect,
        # Optional on purpose: an unanswered question leaves the money in the avans,
        # which is where it already is. Paying cash out by default would be the
        # riskier silence.
        required=False)
    method = forms.ChoiceField(label="To'lov usuli", choices=PayMethod.choices,
                               initial=PayMethod.CASH, required=False)
    due_date = forms.DateField(
        label="Qaysi kuni qaytaramiz", required=False, widget=date_widget(),
        help_text="Pul topilgan kuni ro'yxatdan “To'landi” bosiladi")

    #: Drawn by `_return_rows.html` under the table rather than up in the field list.
    #: The question is about what the typed rows come to, so it belongs after them —
    #: and it is only asked at all when money can actually come back.
    settlement_fields = ("settle", "method", "due_date")

    #: Which of the three answers each field belongs to. The routes do not ask for
    #: the same things: money left in the avans leaves the till by no usul and on no
    #: day, cash handed over needs the heap it comes out of, and a promise needs the
    #: day it is promised for — the day IS the promise. A field named for no route
    #: here is always shown. Read by `_return_rows.html`, which hides the rest.
    settle_routes = {"method": [SETTLE_CASH, SETTLE_OWED], "due_date": [SETTLE_OWED]}

    class Meta:
        model = ReturnBatch
        fields = ["customer", "date", "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}
        # Named on the form rather than on the model: a bare "Sana" on a modal that
        # also lists four sotuv sanasi and asks for a refund day is the one date on
        # the screen that does not say which date it is.
        labels = {"date": "Vazvrat sanasi"}

    @staticmethod
    def returnable_value_by_currency(customer):
        """[(currency, qiymat)] of everything this mijoz could still send back, at
        the narx each sotuv was struck at."""
        entries = []
        for sale in customer.sales.all():
            left = sale.returnable_kg
            if left > 0:
                entries.append((sale.currency,
                                (left * sale.price_own).quantize(
                                    Decimal("0.01"), rounding=ROUND_HALF_UP)))
        return _by_currency(entries)

    @staticmethod
    def open_debt_by_currency(customer):
        """[(currency, qarz)] this mijoz still owes on their sotuvlar."""
        return _by_currency((s.currency, max(Decimal("0"), s.remaining_own))
                            for s in customer.sales.all())

    @classmethod
    def can_overpay(cls, customer):
        """True when SOME vazvrat this mijoz could make would leave money owed back
        to them.

        If everything they are holding is worth no more than what they still owe,
        then no vazvrat of any size can hand money over — the qarz swallows all of
        it. Asking how to settle an excess that cannot exist is asking a question
        with no answer, so the form drops it entirely rather than defaulting it.
        Per currency, because a dollar qarz cannot swallow a so'm vazvrat."""
        if customer is None:
            return True
        debts = dict(cls.open_debt_by_currency(customer))
        return any(value > debts.get(currency, Decimal("0"))
                   for currency, value in cls.returnable_value_by_currency(customer))

    def editing_customer_sales(self):
        """The mijoz's sotuvlar as this form sees them — with the batch being
        corrected taken out. Used by the view to draw the rows, so the ceiling on
        screen is the one `clean()` will enforce."""
        customer = self.current_customer()
        if customer is None:
            return []
        return (customer.sales
                .select_related("line__contract_line", "customer")
                .prefetch_related(returns_without(self.editing), "allocations")
                .order_by("date", "id"))

    def current_customer(self):
        """The mijoz the form is about right now — from the POST when bound, from
        the initial when the modal was opened on a sotuv."""
        raw = (self.data.get("customer") if self.is_bound
               else self.initial.get("customer"))
        if not raw:
            return None
        return Customer.objects.filter(pk=raw).first()

    def __init__(self, *args, editing=None, **kwargs):
        #: The vazvrat being corrected, or None when a new one is being entered.
        #: Everything this form measures has to be read with this batch taken back
        #: out — see `returns_without`.
        self.editing = editing
        super().__init__(*args, **kwargs)
        _customer_payer_field(self.fields["customer"])
        customer = self.current_customer()
        if customer is not None and not self.can_overpay(customer):
            for name in self.settlement_fields:
                self.fields.pop(name, None)
        self.fields["date"].widget = date_widget()
        self.fields["date"].initial = timezone.localdate
        no_future_date(self.fields["date"])
        #: [(sale, kg, qarzdan_usd, qarzdan_uzs)] the operator asked for — filled by
        #: clean(), written by the view.
        self.rows = []
        #: {currency: summa} that cancelled qarz, and that is owed back.
        self.to_debt = {}
        self.excess = {}

    @property
    def rendered_fields(self):
        """The generic renderer's protocol (see `_form_fields.html`), used here to
        HOLD BACK the settlement fields so the rows partial can draw them under the
        table. Everything else keeps its declared order."""
        return [{"group": False, "field": self[name]} for name in self.fields
                if name not in self.settlement_fields]

    @property
    def total_excess(self):
        """Whether ANY money is owed back, in any currency — the one question the
        settlement radio depends on. Summed across heaps only to answer yes or no;
        no figure derived from it is ever shown."""
        return sum((own_side_pair(currency, pair)
                    for currency, pair in self.excess.items()), Decimal("0"))

    def clean(self):
        cleaned = super().clean()
        customer = cleaned.get("customer")
        if customer is None:
            return cleaned

        sales = {s.pk: s for s in customer.sales
                 .select_related("line__contract_line")
                 .prefetch_related(returns_without(self.editing), "allocations")}
        for sale_id, kg in parse_return_rows(self.data):
            sale = sales.get(sale_id)
            if sale is None:
                # A stale or tampered id. Refused rather than skipped: silently
                # dropping a row the operator can see on screen would save a vazvrat
                # smaller than the one they pressed the button on.
                raise forms.ValidationError("Sotuv bu mijozga tegishli emas.")
            left = sale.returnable_kg
            if kg > left:
                self.add_error(
                    None,
                    f"#{sale.pk} · {sale.line.contract_line.brand}: ko'pi bilan "
                    f"{_clean_number(left)} kg qaytarish mumkin "
                    f"(sotilgan {_clean_number(sale.kg)} kg).")
                continue
            # The split, in the sotuv's own money. Worked out here rather than in the
            # view so one place decides it and the modal can print it back before
            # anything is saved.
            value_usd = (kg * sale.price).quantize(Decimal("0.01"),
                                                   rounding=ROUND_HALF_UP)
            value_uzs = (kg * sale.price_uzs).quantize(Decimal("0.01"),
                                                       rounding=ROUND_HALF_UP)
            value = value_uzs if sale.is_som else value_usd
            open_debt = max(Decimal("0"), sale.remaining_own)
            to_debt = min(value, open_debt)
            self.to_debt[sale.currency] = self.to_debt.get(
                sale.currency, Decimal("0")) + to_debt
            # Both columns of the qarz half, as the same fraction of this line's pair
            # — the rule every derived figure in the app follows, so the two halves
            # can never disagree about the kurs.
            debt_share = (to_debt / value) if value else Decimal("0")
            self.rows.append((
                sale, kg,
                (value_usd * debt_share).quantize(Decimal("0.01"),
                                                  rounding=ROUND_HALF_UP),
                (value_uzs * debt_share).quantize(Decimal("0.01"),
                                                  rounding=ROUND_HALF_UP)))
            if value <= to_debt or not value:
                continue
            # Both columns of the excess, taken as the same FRACTION of this line's
            # pair rather than each converted on its own: the two halves of a stored
            # figure have to agree about what the kurs was that day, and the only
            # kurs that has any standing here is the sotuv's own.
            share = (value - to_debt) / value
            usd, uzs = self.excess.get(sale.currency, (Decimal("0"), Decimal("0")))
            self.excess[sale.currency] = (
                usd + (value_usd * share).quantize(Decimal("0.01"),
                                                   rounding=ROUND_HALF_UP),
                uzs + (value_uzs * share).quantize(Decimal("0.01"),
                                                   rounding=ROUND_HALF_UP))

        if not self.rows and not self.errors:
            self.add_error(None, "Hech bo'lmasa bitta qatorga qaytgan kg ni kiriting.")

        settle = cleaned.get("settle") or self.SETTLE_ADVANCE
        cleaned["settle"] = settle
        # Only asked when there IS money to hand back; on an unpaid sotuv the whole
        # vazvrat lands on the qarz and the question has no answer.
        if self.total_excess > 0 and settle == self.SETTLE_OWED and not cleaned.get("due_date"):
            self.add_error("due_date", "Qaysi kuni qaytarishni belgilang.")
        return cleaned


def _customer_payer_field(field):
    """Point a mijoz select at the balance-annotated options, and make it searchable.

    balance walks sotuvlar (minus qaytarishlar) and past to'lovlar in Python, so the
    rows every option needs are fetched once rather than per mijoz.

    `data-combobox` for the same reason the sotuv picker carries it: the list runs to
    hundreds of names and a native select can only be SCROLLED, so every screen that
    starts by picking a mijoz started with a hunt down an alphabetical wall. Typing
    filters; typing nothing still drops the whole list, so it is the select it always
    was for anybody who would rather look than type.

    No quick-add here, unlike the sotuv picker: a mijoz who has never bought anything
    has nothing to pay for and nothing to bring back, so a "yangi mijoz" button on
    these three forms would only ever create a dead row."""
    field.queryset = Customer.objects.prefetch_related("sales__returns", "customer_payments")
    field.label_from_instance = customer_option_label
    field.widget.attrs.setdefault("data-combobox", "")
    field.widget.attrs.setdefault("data-placeholder", "Mijozni tanlang")
    # "" and not None, for the reason spelled out in `_customer_phone_field`: None
    # DELETES the empty row and books the form against whoever sorts first. Blank
    # keeps it and the picker skips a label-less option, so the box reads as its
    # placeholder rather than as a row of dashes.
    field.empty_label = ""


#: The "which qarz" picker. Its VALUE is the currency being settled, so the modal's
#: JS reads it directly — no per-option lookup like ContractChoiceSelect needs — to
#: decide whether a to'lov row crosses a boundary and must ask for a kurs.
DEBT_CURRENCY_CHOICES = [("", "Avtomatik — eng eski qarzdan")] + list(Currency.choices)


def debt_currency_widget():
    return forms.Select(attrs={"data-debt-currency": ""})


class DebtTargetedRateMixin:
    """The kurs is asked for only when the money crosses into the other currency.

    Paying a dollar qarz in dollars settles it at face value — the figure IS the
    qarz, and a rate would decide nothing about it. Paying that same qarz in so'm is
    the one case where the rate decides how much of the qarz the money actually
    clears, so that is the only case the box appears for.

    The mirror of what a hamkor to'lov already does against its kelishuv's currency;
    what is being settled here is the mijoz's qarz, in the currency the operator
    named on the modal. With no qarz named there is no boundary, so nothing is
    demanded — the row inherits and the money goes wherever it fits."""

    def settled_against(self):
        """The currency of the debt this row is aimed at, or "" for none."""
        raise NotImplementedError

    def settles_at_face_value(self):
        """True only when we KNOW this row needs no rate: a qarz was named and this
        money arrived in the same currency, so the figure is the qarz.

        A row with no qarz named is not that case. "Avtomatik" means the money may
        land on either currency's debt, so the rate still decides how much of one it
        clears — and is still asked for, exactly as before this field existed."""
        against = self.settled_against()
        return bool(against) and self.cleaned_data.get("currency") == against

    def clean(self):
        # Seeded before the money mixin converts, and only for the face-value case:
        # a rate the operator did supply always stands, and anywhere else nothing is
        # seeded at all so the mixin's "Dollar kursini kiriting" is what enforces it.
        if ((self.cleaned_data.get("exchange_rate") or Decimal("0")) <= 0
                and self.settles_at_face_value()):
            self.cleaned_data["exchange_rate"] = (
                self.instance.exchange_rate if self.instance.pk
                else latest_exchange_rate())
        return super().clean()


class CustomerPaymentForm(DebtTargetedRateMixin, FeePercentFormMixin,
                          MoneyEntryFormMixin, forms.ModelForm):
    """One to'lov, edited on its own. The create screen uses the target + rows pair
    below instead — a single settlement often arrives in two currencies."""

    fee_counterparty = "Mijozdan ushlansin"

    class Meta:
        model = CustomerPayment
        fields = ["customer", "date", "target_currency", "currency", "amount",
                  "exchange_rate", "method", "fee_percent", "fee_bearer", "note"]
        widgets = {"date": date_widget(), "target_currency": debt_currency_widget()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _customer_payer_field(self.fields["customer"])
        _mark_incoming_fee(self)
        field = self.fields["target_currency"]
        field.required = False
        field.choices = DEBT_CURRENCY_CHOICES

    def settled_against(self):
        return self.cleaned_data.get("target_currency") or ""


class CustomerPaymentTargetForm(forms.Form):
    """The header of a multi-row to'lov modal: who paid, on what date, and against
    WHICH of their debts.

    All three are shared by every row because they describe the one settlement: a
    mijoz clearing 10 000$ by handing over 5 000$ naqd and the rest in so'm has made
    one payment on one day, in two currencies. Splitting them into rows is about how
    the money arrived, not about when, from whom, or what it is settling.

    `debt_currency` exists because a mijoz can owe in both currencies at once, and
    those are two separate debts. Before it, the money went oldest-first across both
    and there was no way to say which one was being collected — the operator picked a
    mijoz and could not tell the app what they were being paid for."""

    customer = forms.ModelChoiceField(queryset=Customer.objects.all(), label="Mijoz")
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)
    debt_currency = forms.ChoiceField(
        label="Qaysi qarzga", required=False, choices=DEBT_CURRENCY_CHOICES,
        widget=debt_currency_widget(),
        help_text="Mijozning qarzi ikki valyutada bo'lsa, qaysi biri to'lanayotganini tanlang")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _customer_payer_field(self.fields["customer"])
        # This header carries the sana for every row beneath it, so the guard belongs
        # here too — the rows get it from MoneyEntryFormMixin.
        no_future_date(self.fields["date"])


class CustomerPaymentRowForm(DebtTargetedRateMixin, FeePercentFormMixin,
                             MoneyEntryFormMixin, forms.ModelForm):
    """One slice of a settlement: a sum, the currency it came in, and how it moved.
    Same shape as a xarajat row — see .lineset--payment in the stylesheet."""

    fee_counterparty = "Mijozdan ushlansin"

    # A to'lov row carries one select fewer than a xarajat (no turkum), so the foiz
    # joins Valyuta and To'lov usuli on the second line rather than being stranded.
    field_order = ["amount", "currency", "method", "fee_percent", "fee_bearer",
                   "exchange_rate", "note"]

    class Meta:
        model = CustomerPayment
        # No mijoz, no sana, no qarz: all three are shared, so the modal's header
        # asks once and every row is settled against the same answer.
        fields = ["currency", "amount", "exchange_rate", "method", "fee_percent",
                  "fee_bearer", "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}

    def __init__(self, *args, target_currency="", **kwargs):
        # Handed down from the header rather than read off the row: which qarz is
        # being collected is a fact about the settlement, not about how one slice of
        # it happened to arrive.
        self.target_currency = target_currency or ""
        super().__init__(*args, **kwargs)
        _mark_incoming_fee(self)

    def settled_against(self):
        return self.target_currency


class BaseSplitPaymentFormSet(forms.BaseModelFormSet):
    """One payment written as the several ways it actually moved.

    Half naqd and half perechisleniya is one settlement to the person making it and
    two movements of money to the kassa — the naqd leaves the safe, the transfer
    leaves the bank and carries the bank's foiz on the way. Each row is one of those
    movements, so it keeps its own summa, valyuta, kurs, usul and foiz; what the
    payment is FOR — the kelishuv, the logist, the sana — is asked once in the header
    above them.

    That is also why this is rows rather than a breakdown inside one row: a single
    row could not say which part of itself the bank charged 2% on.

    `kept_forms` is what a subclass checks a total against — the rows that will
    actually be saved, with the deleted and the never-filled-in ones dropped."""

    def kept_forms(self):
        return [f for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")]

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not self.kept_forms():
            raise forms.ValidationError("Kamida bitta to'lov kiritilishi kerak")


#: Kept under its old name: it is what the mijoz to'lov modal has always been built
#: from, and the outgoing forms below now share the same base.
BaseCustomerPaymentFormSet = BaseSplitPaymentFormSet


def split_payment_formset(model, form, formset=BaseSplitPaymentFormSet):
    """The formset every split-payment modal uses: one blank row to start, and a −
    on each so a row typed by mistake can go."""
    return forms.modelformset_factory(model, form=form, formset=formset,
                                      extra=1, can_delete=True)


CustomerPaymentFormSet = split_payment_formset(CustomerPayment, CustomerPaymentRowForm)


class SupplierPaymentTargetForm(forms.Form):
    """The header of a split hamkor to'lov: which kelishuv is being paid, which
    product of it, and when.

    All three are facts about the settlement rather than about how the money moved,
    so they are asked once — the same division the mijoz modal makes (see
    `CustomerPaymentTargetForm`). Everything that CAN differ between the naqd half
    and the bank half — summa, valyuta, kurs, usul, foiz, vositachi — lives on the
    rows.

    The marka belongs up here for exactly that reason: paying half in cash and half
    by perechisleniya is one delivery being settled two ways, not two deliveries.
    Asked per row it could say the halves bought different markalar, which is not a
    thing that happens — and the operator would have to answer it twice."""

    contract = forms.ModelChoiceField(
        queryset=Contract.objects.none(), label="Kelishuv",
        # data-contract-source as well as -currency: the Mahsulot list below holds
        # EVERY selectable kelishuv's products at once, and this attribute is the
        # only thing that tells the page which select to narrow it against. Without
        # it the narrowing never ran here, and the operator was offered markalar
        # from other kelishuvlar — a pairing `clean` then refused on save.
        widget=ContractChoiceSelect(attrs={"data-contract-currency": "",
                                           "data-contract-source": ""}))
    contract_line = forms.ModelChoiceField(
        queryset=ContractLine.objects.none(), label="Mahsulot", required=False,
        widget=ContractLineChoiceSelect(attrs={"data-line-source": ""}))
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        base = (Contract.objects.select_related("partner")
                .prefetch_related("lines__shipment_lines", "supplier_payments"))
        self.fields["contract"].queryset = _keep_if(base, lambda c: c.payable_left_own > 0)
        self.fields["contract"].label_from_instance = (
            lambda c: contract_option_label(c, payable=True))
        # Same no-AJAX arrangement `SupplierPaymentForm` uses: every selectable
        # kelishuv's products are listed at once and the JS drops the ones belonging
        # to other kelishuvlar. `clean` re-checks the pairing — the client is not the
        # authority on it.
        self.fields["contract_line"].queryset = (
            ContractLine.objects.filter(contract__in=self.fields["contract"].queryset)
            .select_related("contract").order_by("contract_id", "position", "id"))
        self.fields["contract_line"].label_from_instance = (
            lambda ln: f"{ln.brand} · {_clean_number(ln.kg)} kg")
        # Named, not a bare prompt — the twin of `SupplierPaymentForm`, and for the
        # reason spelled out there: blank is the zaklad, not a missing answer.
        self.fields["contract_line"].empty_label = "Butun kelishuv (zaklad)"
        self.fields["contract_line"].help_text = (
            "Pul qaysi mahsulot uchun ketganini belgilang. "
            "Bo'sh qoldirilsa — zaklad: mashinalar soniga qarab bo'linadi")
        # This header carries the sana for every row beneath it.
        no_future_date(self.fields["date"])

    def clean(self):
        cleaned = super().clean()
        contract, line = cleaned.get("contract"), cleaned.get("contract_line")
        # `required=False` and settled here instead: on a single-product kelishuv
        # there is nothing to ask, and on a multi-product one blank is a real answer
        # — the zaklad, which `allocate_supplier_payment` splits by mashina count.
        if line is not None and contract is not None and line.contract_id != contract.pk:
            self.add_error("contract_line", "Bu mahsulot tanlangan kelishuvda yo'q")
        elif contract is not None:
            lines = list(contract.lines.all())
            if len(lines) == 1:
                cleaned["contract_line"] = lines[0]
        return cleaned


class SupplierPaymentRowForm(DebtTargetedRateMixin, FeePercentFormMixin,
                             MoneyEntryFormMixin, forms.ModelForm):
    """One way a hamkor to'lov moved: a sum, the currency it left in, and how.

    The vositachi foizi rides on the ROW, not on the header, for the same reason the
    bank foiz does: a payment sent half in cash by hand and half through a vositachi's
    account paid a cut on one half and nothing on the other, and a single shared box
    could only say one of those two things.

    No `fee_bearer` here either, for the reason `SupplierPaymentForm` gives: on money
    going out to a hamkor the bank's cut is always ours."""

    # Vositachi before perechisleniya: the two foiz share a line (see the
    # `:has(.lineset-field--commission_percent)` rules), and the bank one is the half
    # that disappears on a naqd row. Leading with the field that is always there
    # keeps the survivor on the left rather than stranded in the right-hand column
    # with a gap beside it.
    field_order = ["amount", "currency", "method", "commission_percent",
                   "fee_percent", "exchange_rate", "note"]

    class Meta:
        model = SupplierPayment
        # No kelishuv, no sana: both are shared, so the header asks once.
        fields = ["currency", "amount", "exchange_rate", "commission_percent",
                  "method", "fee_percent", "note"]
        widgets = {
            "commission_percent": forms.NumberInput(attrs={
                "data-commission-percent": "", "step": "0.01", "min": "0", "max": "100",
                "placeholder": "0"}),
            "note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"}),
        }
        labels = {"amount": "Hamkor oladigan summa"}

    def __init__(self, *args, contract=None, **kwargs):
        # Handed down from the header: which kelishuv is being paid decides whether
        # this row has to ask for a kurs, and the header is not clean yet when the
        # rows are built (same reason CustomerPaymentRowForm takes its currency).
        self.contract = contract
        super().__init__(*args, **kwargs)
        self.fields["amount"].widget.attrs["data-commission-base"] = ""
        self.fields["exchange_rate"].help_text = (
            "Faqat kelishuv valyutasidan boshqa valyutada to'lanayotganda kerak")

    def settled_against(self):
        return self.contract.currency if self.contract else ""

    def clean_commission_percent(self):
        return _clean_percent(self.cleaned_data.get("commission_percent"))


class BaseSupplierPaymentFormSet(BaseSplitPaymentFormSet):
    """The kelishuv's ceiling, checked over the WHOLE settlement.

    Per row it would let two halves through that only overshoot together: $6 000 naqd
    and $6 000 bank are each under a $10 000 kelishuv and are $2 000 over it as the
    one payment they are. The single-row form checks the same thing (see
    `SupplierPaymentForm.clean`); this is that check with the rows added up first.

    Each row is weighed by what it CREDITS rather than by what it says, which is why
    the halves are converted one at a time: only the bank half loses a foiz, so
    crediting the pair as one figure would take the foiz off the naqd half too."""

    contract = None

    def clean(self):
        super().clean()
        if any(self.errors) or self.non_form_errors() or self.contract is None:
            return
        contract = self.contract
        paid = sum((credited_to_partner(form.cleaned_data, contract)
                    for form in self.kept_forms()), Decimal("0"))
        left = contract.payable_left_own
        if paid > left:
            shown = som(left) if contract.is_som else usd(left)
            raise forms.ValidationError(
                f"Kelishuv qiymatidan oshib ketdi (to'lash mumkin: {shown})")


SupplierPaymentFormSet = split_payment_formset(
    SupplierPayment, SupplierPaymentRowForm, formset=BaseSupplierPaymentFormSet)


class LogistPaymentTargetForm(forms.Form):
    """The header of a split logist to'ldirish: whose balance, and when."""

    logist = forms.ModelChoiceField(queryset=Logist.objects.all(), label="Logist")
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logist"].label_from_instance = logist_option_label
        no_future_date(self.fields["date"])


class LogistPaymentRowForm(DebtTargetedRateMixin, FeePercentFormMixin,
                           MoneyEntryFormMixin, forms.ModelForm):
    """One way a logist top-up left us. The balance it lands in is kept in dollars
    (see `LogistPaymentForm`), so a dollar row crosses nothing and asks for no kurs
    while a so'm one does.

    No `fee_bearer` here either — the bank's cut on money going out is ours."""

    float_currency = Currency.USD
    field_order = ["amount", "currency", "method", "fee_percent",
                   "exchange_rate", "note"]

    class Meta:
        model = LogistPayment
        fields = ["currency", "amount", "exchange_rate", "method", "fee_percent",
                  "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}
        labels = {"amount": "Yuboriladigan summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exchange_rate"].help_text = "Faqat so'mda yuborilayotganda kerak"
        self.fields["currency"].widget.attrs["data-settled-against"] = self.float_currency

    def settled_against(self):
        return self.float_currency


LogistPaymentFormSet = split_payment_formset(LogistPayment, LogistPaymentRowForm)


class CustomsPaymentTargetForm(forms.Form):
    """The header of a split bojxona to'lov: which bojxonachi, for which yuk, when."""

    agent = forms.ModelChoiceField(queryset=CustomsAgent.objects.all(),
                                   label="Bojxonachi")
    shipment = forms.ModelChoiceField(
        queryset=Shipment.objects.none(), label="Qaysi yuk uchun", required=False,
        empty_label="Yukka bog'lanmagan",
        help_text="Bo'sh qoldirilsa — umumiy to'ldirish, yukka bog'lanmaydi")
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent"].label_from_instance = customs_agent_option_label
        self.fields["shipment"].queryset = (
            Shipment.objects.select_related("contract__partner")
            .prefetch_related("customs_payments", "expenses"))
        self.fields["shipment"].label_from_instance = customs_shipment_option_label
        no_future_date(self.fields["date"])


class CustomsPaymentRowForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """One way money reached a bojxonachi. Nothing here ever crosses a currency — a
    bojxonachi holds a dollar heap and a so'm heap rather than one float — so no kurs
    is ever demanded and the row simply inherits one for the kassa's other column,
    exactly as the single-row form does.

    No `fee_bearer` here either — the bank's cut on money going out is ours."""

    field_order = ["amount", "currency", "method", "fee_percent",
                   "exchange_rate", "note"]

    class Meta:
        model = CustomsPayment
        fields = ["currency", "amount", "exchange_rate", "method", "fee_percent",
                  "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}
        labels = {"amount": "Yuboriladigan summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].widget.attrs["data-settled-against"] = ""
        # Bojxona is overwhelmingly paid in so'm; only a NEW row is steered.
        if not self.instance.pk:
            self.initial.setdefault("currency", Currency.UZS)

    def clean(self):
        if (self.cleaned_data.get("exchange_rate") or Decimal("0")) <= 0:
            self.cleaned_data["exchange_rate"] = latest_exchange_rate()
        return super().clean()


CustomsPaymentFormSet = split_payment_formset(CustomsPayment, CustomsPaymentRowForm)


class KapitalTargetForm(forms.Form):
    """The header of a split kapital entry: in or out, and when."""

    kind = forms.ChoiceField(label="Turi", choices=KapitalKind.choices,
                             initial=KapitalKind.IN)
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        no_future_date(self.fields["date"])


class KapitalRowForm(DebtTargetedRateMixin, FeePercentFormMixin,
                     MoneyEntryFormMixin, forms.ModelForm):
    """One way the ta'sischi's money moved. No fee_bearer, for the reason the
    single-row form gives: both sides of the transfer are the same pocket."""

    float_currency = Currency.USD
    field_order = ["amount", "currency", "method", "fee_percent", "exchange_rate", "note"]

    class Meta:
        model = Kapital
        fields = ["currency", "amount", "exchange_rate", "method", "fee_percent", "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}
        labels = {"amount": "Summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exchange_rate"].help_text = "Faqat so'mda kiritilayotganda kerak"
        self.fields["currency"].widget.attrs["data-settled-against"] = self.float_currency

    def settled_against(self):
        return self.float_currency


KapitalFormSet = split_payment_formset(Kapital, KapitalRowForm)


class OtherExpenseTargetForm(forms.Form):
    """The header of a boshqa chiqim: what it was for, and when.

    The izoh lives HERE rather than on each row because it describes the payment, not
    the way the money moved — one rent bill settled half naqd and half by transfer is
    one "Avgust ijarasi", said once. It is the only description this row has (no
    turkum, by request), which is why it is required where every other note in the
    app is optional: a row saying "$400 left on the 9th" records nothing."""

    note = forms.CharField(
        label="Izoh", max_length=255,
        widget=forms.TextInput(attrs={"placeholder": "Masalan: avgust ijarasi, ish haqi"}),
        help_text="Nima uchun chiqdi — bu yagona izoh")
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        no_future_date(self.fields["date"])


class OtherExpenseRowForm(DebtTargetedRateMixin, FeePercentFormMixin,
                          MoneyEntryFormMixin, forms.ModelForm):
    """One way a boshqa chiqim left the kassa. No izoh and no fee_bearer here: the
    first is the header's (it describes the payment, not the movement) and the second
    is answered once by `OtherExpense.default_fee_bearer` — money going out rides the
    bank's cut on top, the way every other chiqim in the app does."""

    float_currency = Currency.USD
    field_order = ["amount", "currency", "method", "fee_percent", "exchange_rate"]

    class Meta:
        model = OtherExpense
        fields = ["currency", "amount", "exchange_rate", "method", "fee_percent"]
        labels = {"amount": "Summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exchange_rate"].help_text = "Faqat so'mda kiritilayotganda kerak"
        self.fields["currency"].widget.attrs["data-settled-against"] = self.float_currency

    def settled_against(self):
        return self.float_currency


OtherExpenseFormSet = split_payment_formset(OtherExpense, OtherExpenseRowForm)


class OtherExpenseForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """One boshqa chiqim, reopened on its own — the edit twin of the pair above.

    Built like `KapitalForm` and for the same reasons: no counterparty, no qarz to
    overpay, nobody's balance to keep. It moves the till and stops there."""

    #: The currency the kassa's own figure is anchored in — a dollar entry crosses
    #: nothing, so the same JS hides the kurs box for it.
    float_currency = Currency.USD

    class Meta:
        model = OtherExpense
        fields = ["date", "note", "currency", "amount", "exchange_rate",
                  "method", "fee_percent"]
        widgets = {"date": date_widget()}
        labels = {"amount": "Summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        no_future_date(self.fields["date"])
        self.fields["exchange_rate"].help_text = "Faqat so'mda kiritilayotganda kerak"
        self.fields["currency"].widget.attrs["data-settled-against"] = self.float_currency

    def settled_against(self):
        return self.float_currency


def payer_choices(category):
    """Who could have paid a xarajat of THIS turkum out of money we already sent
    them — bojxonachilar for a bojxona, logistlar for a transport.

    One list per box rather than everybody under two headings. The two roles do not
    overlap in practice: a bojxonachi clears loads and a logist pays drivers, and
    offering both in both boxes turns a two-item pick into a scan of every outside
    party in the books for a choice that only ever had one right answer.

    The kassa is first in the transport box and is what it rests on. It is not a
    placeholder there — it is the answer for most xarajatlar, and the one this form
    gave for its whole life before the picker existed.

    The bojxona box does not offer it at all. Clearing money reaches bojxona through
    a tamojni: we send it to them before the yuk is cleared and it is spent out of
    what they hold, so "kassadan to'landi" on a bojxona never named a payer — it was
    the picker being left alone. That box lists the tamojnilar and nothing else, and
    its blank entry is a prompt to name one rather than an answer standing beside
    them; a figure typed without one is refused (ExpenseGridForm.clean)."""
    if category == ShipmentExpense.Category.CUSTOMS:
        return [("", "Tamojnini tanlang"),
                *((f"customs:{row.pk}", row.name)
                  for row in CustomsAgent.objects.all())]
    if category == ShipmentExpense.Category.TRANSPORT:
        rows, prefix = Logist.objects.all(), "logist"
    else:
        rows, prefix = (), ""
    return [("", "Kassadan to'landi"),
            *((f"{prefix}:{row.pk}", row.name) for row in rows)]


def resolve_payer(value):
    """A `payer_choices` value as the two FK columns it sets — always BOTH, so
    choosing a bojxonachi clears any logist rather than leaving a row claiming two
    payers (ShipmentExpense.clean refuses that pair)."""
    kind, _sep, pk = (value or "").partition(":")
    if kind == "logist" and pk.isdigit():
        return {"logist_id": int(pk), "customs_agent_id": None}
    if kind == "customs" and pk.isdigit():
        return {"logist_id": None, "customs_agent_id": int(pk)}
    return {"logist_id": None, "customs_agent_id": None}


def payer_value(row):
    """A stored xarajat as the picker value that stands for it — the inverse of
    `resolve_payer`, so a box opens showing what its row actually says."""
    if row.logist_id:
        return f"logist:{row.logist_id}"
    if row.customs_agent_id:
        return f"customs:{row.customs_agent_id}"
    return ""


def default_payer(shipment):
    """The bojxonachi this load's clearing money was already sent to, or the kassa.

    Deliberately NOT the yuk's logist, even though the single-xarajat form does
    default to them. Every load in the books predates this box, and on all of them
    the grid wrote kassa rows; steering it to the logist now would start moving a
    gruzchi onto somebody's balance on loads whose entry never changed. Having been
    SENT customs money is the one signal that cannot be true of an older load, so it
    is the only one allowed to move the default off the kassa."""
    if shipment is None or not getattr(shipment, "pk", None):
        return ""
    funded = shipment.customs_payments.first()
    return f"customs:{funded.agent_id}" if funded else ""


class ExpenseGridForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.Form):
    """Every xarajat turkum as its own small box, filled in one pass.

    The old shape asked the operator to add a row, pick a turkum from a dropdown,
    type a figure, add another row, pick again — for costs they already know as a
    set ("bojxona 3200, deklarant 175, gruzchi 65"). Here the turkumlar are the
    form: seven labelled boxes, type into the ones that apply, leave the rest blank.

    Valyuta, kurs and to'lov usuli are asked once and shared. That is not a
    simplification of the data — across every yuk in the books, the method has never
    differed between one truck's xarajatlar. A line that genuinely needs its own
    method is still one more submission away.

    Opened on a yuk that already has xarajatlar, the boxes show them. The modal is
    the yuk's xarajatlar rather than an entry queue: coming back to add a bojxona to
    a load that already carries a gruzchi and a deklarant used to mean seven empty
    boxes, with no way to tell from here what was already in the books and nothing
    stopping the gruzchi being typed a second time."""

    #: (model category value, label) — the grid's order, which is the order the
    #: money is actually spent on a load rather than alphabetical.
    CATEGORIES = ShipmentExpense.Category.choices

    shipment = forms.ModelChoiceField(queryset=Shipment.objects.all(),
                                      widget=forms.HiddenInput)
    date = forms.DateField(label="Sana", widget=date_widget(),
                           initial=timezone.localdate)
    # Radios, not selects: two and three options respectively, so a dropdown hides
    # the whole choice behind a click to show what a segmented control states
    # outright — and the method colours already mean something everywhere else.
    currency = forms.ChoiceField(label="Valyuta", choices=Currency.choices,
                                 initial=Currency.USD, widget=forms.RadioSelect)
    method = forms.ChoiceField(label="To'lov usuli", choices=PayMethod.choices,
                               initial=PayMethod.CASH, widget=forms.RadioSelect)
    exchange_rate = forms.DecimalField(
        label="Dollar kursi (1$ = so'm)", max_digits=12, decimal_places=2,
        initial=LEGACY_RATE)
    fee_percent = forms.DecimalField(
        label="Perechisleniya foizi (%)", max_digits=5, decimal_places=2,
        required=False, initial=0,
        help_text="Bank orqali to'langan xarajatlarga qo'llanadi — turkum o'zinikini "
                  "kiritsa, o'shanisi ustun")
    note = forms.CharField(label="Izoh", max_length=255, required=False,
                           widget=forms.TextInput(attrs={"placeholder": "Ixtiyoriy"}))
    #: The only turkumlar somebody else's balance ever pays: a bojxonachi clears the
    #: yuk, a logist covers the haydovchi. The gruzchi, the sertifikat and the yo'l
    #: xarajati come out of the kassa on the day, so those boxes do not ask.
    #:
    #: Per box rather than once above the grid, for the same reason valyuta and usul
    #: have per-box overrides: one control over seven boxes cannot say that the
    #: bojxonachi paid the bojxona while the logist paid the transport, which is the
    #: ordinary case on a load that has both. Shared, it also put the 65 gruzchi
    #: typed beside a 37 mln bojxona onto the bojxonachi's balance — money he never
    #: handled, and a qoldiq nobody could explain afterwards.
    PAYER_CATEGORIES = frozenset({
        ShipmentExpense.Category.CUSTOMS, ShipmentExpense.Category.TRANSPORT})

    def __init__(self, *args, shipment=None, **kwargs):
        super().__init__(*args, **kwargs)
        # What the yuk already has. `recorded` are the rows the boxes stand in for;
        # `others` are the rows they cannot, kept only to be named under the box so
        # a figure in the jadval the grid does not carry is never a mystery.
        self.shipment = shipment
        self.recorded, self.others = self.load_rows(shipment)
        for value, label in self.CATEGORIES:
            field = forms.DecimalField(
                label=label, max_digits=14, decimal_places=2, required=False,
                min_value=Decimal("0"),
                widget=forms.NumberInput(attrs={"placeholder": "0", "step": "0.01"}))
            self.fields[self.field_name(value)] = field
            _group_thousands(field)
            # Same hook the single-amount forms use, on every box: the Valyuta
            # picker above is shared, so switching it previews all seven at once.
            field.widget.attrs["data-money-amount"] = ""
            # Per-turkum overrides, blank = "use the shared one above". Folded away
            # in a <details> so the common case — one truck, one way of paying —
            # stays a plain grid of figures, while a bojxona paid by bank in so'm
            # alongside a cash gruzchi is still one submission.
            # Short option text: these sit in ~86px dropdowns beside the figure, and
            # the blank one doubles as the placeholder — an untouched box reads
            # "Valyuta" / "Usul", which is exactly what "not overridden" means.
            self.fields[self.currency_name(value)] = forms.ChoiceField(
                label="Valyuta", required=False, initial="",
                choices=[("", "Valyuta"), (Currency.USD, "$"), (Currency.UZS, "so'm")],
                widget=forms.Select(attrs={"class": "xmini"}))
            self.fields[self.method_name(value)] = forms.ChoiceField(
                label="To'lov usuli", required=False, initial="",
                choices=[("", "Usul"), (PayMethod.CASH, "Naqd"),
                         (PayMethod.CARD, "Karta"), (PayMethod.TRANSFER, "Bank")],
                widget=forms.Select(attrs={"class": "xmini"}))
            # The foiz belongs to the row that was actually wired, not to the trip.
            # One truck routinely pays a bojxona by bank and a gruzchi in cash, and
            # the banks the business uses do not all charge the same — so a single
            # shared figure either bills the cash rows (which CashEntry.fee_amount
            # then silently drops) or understates the wired one. Blank = "as set
            # below", the same rule the valyuta and usul overrides follow.
            self.fields[self.fee_name(value)] = forms.DecimalField(
                label="Perechisleniya foizi (%)", max_digits=5, decimal_places=2,
                required=False, min_value=Decimal("0"), max_value=Decimal("100"),
                widget=forms.NumberInput(attrs={
                    "class": "xmini xfee", "placeholder": "foiz",
                    "step": "0.01", "min": "0", "max": "100"}))
            # Which row this box was showing when the modal was drawn. Saqlash
            # rewrites and removes only these — the grid speaks for what the
            # operator had in front of them, not for whatever the yuk holds by the
            # time it is submitted. Without it a modal opened before a colleague
            # added a xarajat would delete that xarajat on save, because its box was
            # blank here for the innocent reason that it did not exist yet.
            self.fields[self.row_name(value)] = forms.IntegerField(
                required=False, widget=forms.HiddenInput)
            # Only on the two turkumlar somebody's balance ever pays. It sits full
            # width under the figure rather than beside it in the 86px satellite
            # column, because a person's name does not fit in 86px and a truncated
            # one is worse than no picker at all.
            #
            # Choices built per instance, not at import: a bojxonachi added this
            # morning has to be pickable this afternoon without a restart.
            if value in self.PAYER_CATEGORIES:
                self.fields[self.payer_name(value)] = forms.ChoiceField(
                    label="Kim to'laydi", required=False, initial="",
                    choices=self.payer_options(value),
                    widget=forms.Select(attrs={"class": "xpayer"}))
        # Only the unbound case: a bound form renders what was posted, and re-filling
        # it from the database would undo the operator's own edit on a failed submit.
        if not self.is_bound:
            self.prefill()
            # An empty Bojxona box on a load whose clearing money has already gone
            # out opens naming the bojxonachi it went to: entering that figure as a
            # kassa row is the double-count this picker exists to stop, and it is
            # the one default that cannot be wrong about a load from before the
            # picker existed (nothing was ever sent for those).
            #
            # Transport is deliberately left on the kassa even when the yuk names a
            # logist. Every load in the books predates this box and the grid wrote
            # kassa rows on all of them; steering it now would start moving money
            # onto a balance on loads whose entry never changed.
            box = self.payer_name(ShipmentExpense.Category.CUSTOMS)
            if not self.initial.get(box):
                self.initial[box] = default_payer(shipment)

    @staticmethod
    def load_rows(shipment):
        """({turkum: the row its box stands in for}, {turkum: [the rest]}).

        A turkum recorded exactly once maps onto its box: the box opens showing that
        figure and saving rewrites that row. A turkum recorded twice — two yo'l
        xarajati on two different days — has no single figure to show, and prefilling
        one of the two would rewrite that one while silently leaving the other, so
        the box stays empty and additive and those rows are edited from the jadval.

        The haydovchi avansi is never one of them whatever else the turkum holds: the
        yuk form owns that row and rewrites it on every save (sync_driver_advance),
        so a figure typed over it here would be undone the next time the yuk is
        edited."""
        if shipment is None or not getattr(shipment, "pk", None):
            return {}, {}
        found = {}
        for row in shipment.expenses.all():
            found.setdefault(row.category, []).append(row)
        recorded, others = {}, {}
        for category, rows in found.items():
            managed = [row for row in rows if not row.is_driver_advance]
            if len(managed) == 1:
                recorded[category] = managed[0]
            rest = [row for row in rows if row is not recorded.get(category)]
            if rest:
                others[category] = rest
        return recorded, others

    @staticmethod
    def typed_amount(row):
        """The figure as it was typed: a so'm row shows its so'm side rather than the
        dollar twin, the same rule MoneyEntryFormMixin._seed_typed_side follows on the
        single-row forms — a 12 000 000 so'm bojxona reopening as 1 000 would be read
        back as 1 000 so'm and saved as $0.08."""
        return row.amount_uzs if row.currency == Currency.UZS else row.amount

    def prefill(self):
        """Show the yuk's xarajatlar in their boxes.

        The shared pickers show what the rows agree on — one truck's costs nearly
        always went out the same way — and only a turkum that differs carries its own
        override, so the grid reads the way it was filled in rather than turning every
        box into an exception.

        Sana is deliberately not among them: it stays on today, because it dates the
        rows being ADDED. An existing row keeps the sana it was recorded with (see
        save()), shown under its box, rather than being dragged to today by an edit
        to the figure beside it."""
        rows = list(self.recorded.values())
        if not rows:
            return
        shared = {
            "currency": _agreed(row.currency for row in rows),
            "method": _agreed(row.method for row in rows),
            "exchange_rate": _agreed(row.exchange_rate for row in rows),
            "fee_percent": _agreed(row.fee_percent for row in rows),
        }
        # Valyuta, usul and foiz have a per-box override to fall back on when the
        # rows disagree; the kurs has none, and leaving it on the legacy default
        # would price a new box at a rate this yuk never used. The newest row's is
        # the nearest thing the yuk has to today's.
        if shared["exchange_rate"] is None:
            shared["exchange_rate"] = max(
                rows, key=lambda row: (row.date, row.pk)).exchange_rate
        for name, value in shared.items():
            if value is not None:
                self.initial[name] = value
        for category, row in self.recorded.items():
            self.initial[self.row_name(category)] = row.pk
            self.initial[self.field_name(category)] = self.typed_amount(row)
            if row.currency != shared["currency"]:
                self.initial[self.currency_name(category)] = row.currency
            if row.method != shared["method"]:
                self.initial[self.method_name(category)] = row.method
            if row.fee_percent != shared["fee_percent"]:
                self.initial[self.fee_name(category)] = row.fee_percent
            # Its OWN payer, unlike the three above, which fall back to a shared
            # picker. There is no shared payer to fall back to — the whole point of
            # this box being per turkum is that a bojxonachi and a logist can be on
            # the same yuk — so the box shows exactly what the row says.
            if category in self.PAYER_CATEGORIES:
                self.initial[self.payer_name(category)] = payer_value(row)

    @staticmethod
    def field_name(category):
        return f"amount_{category}"

    @staticmethod
    def currency_name(category):
        return f"currency_{category}"

    @staticmethod
    def method_name(category):
        return f"method_{category}"

    @staticmethod
    def fee_name(category):
        return f"fee_{category}"

    @staticmethod
    def row_name(category):
        return f"row_{category}"

    @staticmethod
    def payer_name(category):
        return f"payer_{category}"

    def payer_options(self, category):
        """This box's list, plus whoever its recorded row actually names if that
        person is no longer offered here.

        A logist DID once pay a bojxona — ShipmentExpense allows it and the single
        xarajat form still offers it — and this box now lists bojxonachilar only.
        Dropped from the choices, such a row would open showing nobody and be
        refused on submit as an invalid choice, so correcting the figure beside it
        would be impossible without first reassigning money the operator never
        touched. Kept pickable, the box tells the truth and a save is a no-op."""
        choices = payer_choices(category)
        row = self.recorded.get(category)
        current = payer_value(row) if row is not None else ""
        if current and current not in {value for value, _label in choices}:
            choices.append((current, f"{row.paid_by.name} (avvalgi)"))
        # A bojxona already in the books with nobody on it comes from before the
        # kassa stopped being an answer here. Its box says what the ROW says rather
        # than asking for a tamojni, so a figure corrected beside it can be saved
        # without first moving real money onto somebody's balance. Only this row is
        # allowed it — a bojxona typed into an empty box still names one.
        if (category == ShipmentExpense.Category.CUSTOMS and row is not None
                and row.from_kassa and choices and choices[0][0] == ""):
            choices[0] = ("", "Kassadan to'landi (avvalgidek)")
        return choices

    def row_payer(self, category):
        """The two payer columns this box is asking for, or {} for a turkum that
        never asks — which the caller must treat as "leave the row alone" rather
        than as "the kassa paid it"."""
        if category not in self.PAYER_CATEGORIES:
            return {}
        return resolve_payer(self.cleaned_data.get(self.payer_name(category)))

    def amount_fields(self):
        """One box per turkum, for a template that lays the grid out itself rather
        than trusting field order. `recorded` is the row this box stands in for (so
        the box can say when it was entered) and `others` the rows it does not."""
        return [{"category": value,
                 "amount": self[self.field_name(value)],
                 "currency": self[self.currency_name(value)],
                 "method": self[self.method_name(value)],
                 "fee": self[self.fee_name(value)],
                 "row": self[self.row_name(value)],
                 "recorded": self.recorded.get(value),
                 "others": self.others.get(value, []),
                 # None on the turkumlar the kassa always pays, so the template asks
                 # `{% if cell.payer %}` rather than repeating PAYER_CATEGORIES.
                 "payer": (self[self.payer_name(value)]
                           if value in self.PAYER_CATEGORIES else None)}
                for value, _ in self.CATEGORIES]

    def row_fee(self, category):
        """The foiz this turkum is charged: its own if one was typed, else the
        shared one. `0` typed into a box is an explicit "no foiz on this row" and
        must win over the shared figure, so a blank is told apart from a zero
        rather than both being falsy."""
        own = self.cleaned_data.get(self.fee_name(category))
        if own is not None:
            return own
        return self.cleaned_data.get("fee_percent") or Decimal("0")

    def clean(self):
        cleaned = super().clean()
        entered = [(value, cleaned.get(self.field_name(value)))
                   for value, _ in self.CATEGORIES]
        self.entries = [(value, amount) for value, amount in entered
                        if amount is not None and amount > 0]
        # Nothing typed and no box that opened showing a row: the modal would
        # otherwise save nothing and close as if it had worked. On a grid that DID
        # open filled, an empty one is not an empty submission — it is every xarajat
        # cleared, which save() carries out.
        showed = any(cleaned.get(self.row_name(value)) for value, _ in self.CATEGORIES)
        if not self.entries and not showed:
            raise forms.ValidationError("Kamida bitta xarajat kiritilishi kerak")
        for value, amount in entered:
            if amount is not None and amount <= 0:
                self.add_error(self.field_name(value), "Musbat son kiriting")
        # A bojxona figure with nobody named would be written as a kassa row — the
        # one answer its box no longer offers. Asked here rather than by making the
        # field required, so a blank box on a turkum nobody typed into stays blank
        # and a cleared bojxona (its row being deleted) is not made to name a payer
        # on the way out.
        customs = ShipmentExpense.Category.CUSTOMS
        if (dict(self.entries).get(customs)
                and not cleaned.get(self.payer_name(customs))
                and not self.drawn_as_kassa_bojxona(cleaned.get(self.row_name(customs)))):
            self.add_error(self.payer_name(customs),
                           "Qaysi tamojni to'lagan — tanlang")
        return cleaned

    def drawn_as_kassa_bojxona(self, pk):
        """True when the bojxona box was drawn for a row already recorded with
        nobody on it — the one case a blank payer is an answer rather than a skipped
        pick, because it is the answer that row already carries.

        Matched against the row the box actually showed (row_customs), so a blank
        arriving with somebody else's pk, or with none at all, is still refused."""
        row = self.recorded.get(ShipmentExpense.Category.CUSTOMS)
        return bool(pk and row is not None and row.pk == pk and row.from_kassa)

    def row_money(self, category, typed, rate):
        """What a filled box means in money, converted at its own valyuta.

        Currency and method are per line, defaulting to the shared pickers — a
        bojxona wired in so'm next to a gruzchi paid cash is one submission."""
        currency = (self.cleaned_data.get(self.currency_name(category))
                    or self.cleaned_data["currency"])
        method = (self.cleaned_data.get(self.method_name(category))
                  or self.cleaned_data["method"])
        usd_value, uzs_value = convert_pair(typed, currency, rate)
        return {"amount": usd_value, "amount_uzs": uzs_value, "currency": currency,
                "method": method, "exchange_rate": rate,
                "fee_percent": self.row_fee(category)}

    def save(self, user):
        """Make the yuk's xarajatlar match the grid — (created, updated, deleted).

        A box that opened showing a row rewrites THAT row rather than adding a second
        one: the whole point of opening filled in is that coming back to add a bojxona
        cannot duplicate the gruzchi sitting in front of the operator. Cleared, that
        row goes — the grid is the yuk's xarajatlar, so a box left empty and a turkum
        with no xarajat have to mean the same thing.

        "That row" is the one the box carried when the modal was drawn (row_<turkum>),
        never simply whatever the yuk has under that turkum now. A xarajat added while
        this modal sat open is left alone rather than deleted for being absent from a
        grid that never showed it.

        A rewrite touches only the money: summa, valyuta, usul, foiz and the kurs
        behind them. The row keeps its own sana — a xarajat entered last week does
        not move to today because the figure beside it was corrected — along with its
        izoh (the shared one names what is being ADDED). The Kim to'laydi box IS
        rewritten, because it is per turkum and opens showing that row's own payer —
        see the comparison below.

        A box that comes back exactly as it was drawn is not written at all. That
        matters most for the kurs: it is one field above seven boxes, so a yuk whose
        rows were entered at different kursi would otherwise have every one of them
        re-rated by a submit that touched nothing."""
        shipment = self.cleaned_data["shipment"]
        # Its own yuk, and never the haydovchi avansi: a hand-built payload naming
        # somebody else's row must not reach it through here.
        rewritable = {row.pk: row
                      for row in shipment.expenses.exclude(is_driver_advance=True)}
        rate = self.cleaned_data["exchange_rate"]
        note = self.cleaned_data.get("note", "")
        typed_by_category = dict(self.entries)
        created, updated, deleted = [], [], []
        for category, _label in self.CATEGORIES:
            typed = typed_by_category.get(category)
            row = rewritable.get(self.cleaned_data.get(self.row_name(category)))
            if row is not None and row.category != category:
                # The box moved turkum under it — treat it as an unfamiliar row and
                # leave it alone rather than rewriting a bojxona as a gruzchi.
                row = None
            if typed is None:
                if row is not None:
                    row.delete()
                    deleted.append(row)
                continue
            money = self.row_money(category, typed, rate)
            payer = self.row_payer(category)
            if row is None:
                created.append(ShipmentExpense.objects.create(
                    shipment=shipment, category=category, created_by=user,
                    date=self.cleaned_data["date"], note=note, **payer, **money))
                continue
            # As drawn: the same figure in the same valyuta, paid the same way, at
            # the same foiz, by the same person. Compared against what the BOX showed
            # rather than field by field against the row, so the shared kurs — which
            # no box can show per row — cannot make an untouched submission look like
            # an edit.
            #
            # The payer IS rewritten here, unlike when this was one control above the
            # grid. A per-turkum box opens showing that row's own payer, so changing
            # it is a deliberate, visible edit rather than one shared answer being
            # applied to rows it was never asked about.
            if (typed == self.typed_amount(row) and money["currency"] == row.currency
                    and money["method"] == row.method
                    and money["fee_percent"] == row.fee_percent
                    and all(getattr(row, name) == value
                            for name, value in payer.items())):
                continue
            fields = {**money, **payer}
            for name, value in fields.items():
                setattr(row, name, value)
            row.save(update_fields=list(fields))
            updated.append(row)
        return created, updated, deleted


class ShipmentExpenseForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = ShipmentExpense
        fields = ["shipment", "date", "category", "logist", "customs_agent",
                  "currency", "amount", "exchange_rate", "method", "fee_percent",
                  "note"]
        widgets = {"date": date_widget(),
                   "shipment": forms.HiddenInput()}
        help_texts = {"logist": "Bo'sh qoldirilsa — kassadan to'langan",
                      "customs_agent": "Biz oldindan yuborgan puldan to'langan bo'lsa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logist"].empty_label = "Kassadan to'landi"
        self.fields["customs_agent"].empty_label = "Kassadan to'landi"
        self.fields["customs_agent"].label_from_instance = customs_agent_option_label
        # On a bojxona the bojxonachi box lists the tamojnilar alone. The money for
        # a clearing goes to one of them first and is spent out of what they hold,
        # so the kassa was never an answer here — a row carrying it is one where the
        # box was left alone, which is what clean() now refuses. The blank entry
        # stays, as a prompt: every bojxona already in the books predates this rule,
        # and a box with no blank would open on whichever tamojni sorts first and
        # move real money onto them the moment anything else on the row was saved.
        #
        # Every other turkum keeps the kassa: a gruzchi or a yo'l xarajati really is
        # paid straight out of it.
        #
        # One exception, and it is the row this rule arrived too late for: a bojxona
        # ALREADY recorded with nobody on it. Every one of those was entered when
        # the kassa was the answer here, and refusing them would mean no sana and no
        # summa on them could be corrected without first moving real money onto a
        # tamojni. Such a row keeps its own answer, spelled out as the old one.
        self.kassa_bojxona_as_recorded = bool(
            self.instance.pk
            and self.instance.category == ShipmentExpense.Category.CUSTOMS
            and self.instance.logist_id is None
            and self.instance.customs_agent_id is None)
        if self.instance.category == ShipmentExpense.Category.CUSTOMS:
            self.fields["customs_agent"].empty_label = (
                "Kassadan to'landi (avvalgidek)" if self.kassa_bojxona_as_recorded
                else "Tamojnini tanlang")
            self.fields["customs_agent"].help_text = "Bu bojxonani qaysi tamojni to'ladi"
            self.fields["logist"].help_text = "Bojxonani logist to'lagan bo'lsa"
        # Default to whoever is already carrying money for this load: the logist who
        # runs it, or the bojxonachi we sent its clearing money to. Picking the wrong
        # one silently moves money between two people's accounts, and leaving it
        # blank bills the kassa for cash that already left.
        #
        # ONE of them, never both — a load can have a logist AND a funded bojxonachi,
        # and pre-filling the pair would open the form already holding the one
        # combination clean() refuses. The logist keeps precedence because that is
        # the default this form has always opened with.
        shipment = self.initial.get("shipment") or getattr(self.instance, "shipment_id", None)
        if shipment and not self.instance.pk:
            match = (Shipment.objects.filter(pk=getattr(shipment, "pk", shipment))
                     .prefetch_related("customs_payments").first())
            if match and match.logist_id:
                self.initial.setdefault("logist", match.logist_id)
            elif match:
                funded = match.customs_payments.all()
                if funded:
                    self.initial.setdefault("customs_agent", funded[0].agent_id)

    def clean(self):
        cleaned = super().clean()
        # The model refuses this too (ShipmentExpense.clean), but a ModelForm never
        # calls full_clean's model hook for fields it did not render — and here it
        # renders both, so the operator gets the answer on the field rather than a
        # 500 from the database later.
        if cleaned.get("logist") and cleaned.get("customs_agent"):
            self.add_error("customs_agent",
                           "Bittasini tanlang — yo logist, yo bojxonachi to'lagan")
        # Nobody named on a bojxona means the kassa paid it, and the kassa does not
        # pay bojxona — the tamojni it was sent to does. Read off the SUBMITTED
        # turkum rather than the row's own, so a xarajat switched to Bojxona in this
        # box is asked the same question as one that opened as one.
        elif (cleaned.get("category") == ShipmentExpense.Category.CUSTOMS
              and not cleaned.get("logist") and not cleaned.get("customs_agent")
              and not self.kassa_bojxona_as_recorded):
            self.add_error("customs_agent", "Qaysi tamojni to'lagan — tanlang")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        if commit:
            obj.save()
        return obj


class LogistForm(forms.ModelForm):
    class Meta:
        model = Logist
        fields = ["name", "phone", "note"]
        widgets = {
            "phone": phone_intl_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "name": forms.TextInput(attrs={"autocomplete": "off",
                                           "placeholder": "Masalan: Sardor aka"}),
        }

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))


class LogistPaymentForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """Money we send a logist. No ceiling: unlike a hamkor to'lov there is nothing
    to overpay — the balance is a running float they draw drivers' advances from,
    and topping it up before the loads go out is the normal way round.

    That float is kept in dollars: every driver advance is booked in dollars
    (ShipmentForm.sync_driver_advance), so the balance is a dollar figure. A dollar
    top-up therefore crosses nothing and asks for no kurs, exactly as a kelishuv paid
    in its own currency does; a so'm top-up does convert into that float, so there the
    rate is what decides how much the logist ends up holding.

    No `fee_bearer`, for the reason `SupplierPaymentForm` gives: on money going OUT
    the bank's cut is always ours. The logist has to end up holding the figure we
    said we were sending, so the foiz rides on top of it and the kassa is out the
    extra."""

    #: The currency the logist's balance is kept in — see the class docstring.
    float_currency = Currency.USD

    class Meta:
        model = LogistPayment
        fields = ["logist", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "note"]
        widgets = {"date": date_widget()}
        labels = {"amount": "Yuboriladigan summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logist"].label_from_instance = logist_option_label
        self.fields["exchange_rate"].help_text = (
            "Faqat so'mda yuborilayotganda kerak")
        # A constant rather than a picker: unlike a kelishuv there is nothing to
        # choose here, but the same JS decides whether the kurs box is shown.
        self.fields["currency"].widget.attrs["data-settled-against"] = self.float_currency

    def clean(self):
        # Same rule as a hamkor to'lov, and only a MISSING rate is filled in — one the
        # operator did supply stands. An edit keeps the rate the top-up was booked at,
        # so a figure already on the books never moves on its own.
        typed_rate = self.cleaned_data.get("exchange_rate") or Decimal("0")
        if typed_rate <= 0 and self.cleaned_data.get("currency") == self.float_currency:
            self.cleaned_data["exchange_rate"] = (
                self.instance.exchange_rate if self.instance.pk else latest_exchange_rate())
        return super().clean()


def logist_option_label(logist):
    """Logist <option>: the name and what they are holding, since the balance is
    the fact you need when deciding how much to send."""
    balance = logist.balance
    if balance > 0:
        state = f"{_clean_number(balance)} $ qoldiq"
    elif balance < 0:
        state = f"{_clean_number(-balance)} $ bizning qarzimiz"
    else:
        state = "qoldiq yo'q"
    return f"{logist.name} · {state}"


class KapitalForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """Ta'sischi's own money in or out of the kassa.

    No counterparty and no ceiling: unlike a hamkor to'lov there is no qarz to
    overpay and nobody's balance to keep — the row moves the till and stops there.

    `fee_bearer` is deliberately absent from the fields. The bank's cut on a
    perechisleniya is real and the row nets by it, but asking WHOSE loss it is only
    makes sense with two parties, and here both sides of the transfer are the same
    pocket. `Kapital.default_fee_bearer` answers it once instead."""

    #: The currency the kassa's own figure is anchored in — as with a logist top-up
    #: a dollar entry crosses nothing, so the same JS hides the kurs box for it.
    float_currency = Currency.USD

    class Meta:
        model = Kapital
        fields = ["kind", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "note"]
        widgets = {"date": date_widget()}
        labels = {"amount": "Summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exchange_rate"].help_text = "Faqat so'mda kiritilayotganda kerak"
        self.fields["currency"].widget.attrs["data-settled-against"] = self.float_currency

    def clean(self):
        # Same rule as a logist top-up, and only a MISSING rate is filled in — one the
        # operator did supply stands, so an edit keeps the kurs the row was booked at
        # and a figure already on the books never moves on its own.
        typed_rate = self.cleaned_data.get("exchange_rate") or Decimal("0")
        if typed_rate <= 0 and self.cleaned_data.get("currency") == self.float_currency:
            self.cleaned_data["exchange_rate"] = (
                self.instance.exchange_rate if self.instance.pk else latest_exchange_rate())
        return super().clean()


class KonvertatsiyaForm(GroupedFieldsMixin, forms.ModelForm):
    """Money changed from one heap of the kassa into another.

    Two boxed halves — what LEFT and what ARRIVED — because that is how the operator
    does it at the counter: they hand over a figure and they get a figure back, and
    the kurs is whatever those two say it was. Nothing on this form is derived from
    anything else on it, so every combination works the same way: naqd so'm into naqd
    dollar, naqd into a karta, a bank balance into cash, so'm to so'm.

    Deliberately NOT built on `MoneyEntryFormMixin`. That mixin converts ONE typed
    sum into its twin at a kurs, which is the wrong shape here: this row has two real
    figures in two places and neither is a conversion of the other.

    The kurs box between the two halves is a CALCULATOR, not the record. Type it with
    either sum and the other one fills itself in, which is how the operator thinks
    about it ("bugun 12 700 dan") — and then it can be corrected by hand, because a
    valyutachi rounds and what actually changed hands is the pair of sums, not the
    rate. What gets stored is what the two sums say (Konvertatsiya.save), so the kurs
    the row reports can never disagree with the money it moved.
    """

    # Toned the way the daftar already draws these two ideas — what leaves a heap
    # reads like a chiqim, what lands like a kirim — so the modal and the row it
    # produces are recognisably the same event, and the operator can tell the halves
    # apart at a glance instead of reading two near-identical legends.
    field_groups = [
        ("Qayerdan chiqdi", ["from_method", "from_currency", "from_amount"], "out"),
        ("Qayerga tushdi", ["to_method", "to_currency", "to_amount"], "in"),
    ]

    class Meta:
        model = Konvertatsiya
        # The kurs sits BETWEEN the two halves, where it belongs in the sentence the
        # form is asking: this much left, at this rate, that much arrived.
        fields = ["date", "from_method", "from_currency", "from_amount",
                  "exchange_rate", "to_method", "to_currency", "to_amount", "note"]
        widgets = {"date": date_widget(),
                   "note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}
        labels = {"exchange_rate": "Kurs (1$ = so'm)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        no_future_date(self.fields["date"])
        for name in ("from_amount", "to_amount", "exchange_rate"):
            _group_thousands(self.fields[name])
        self.fields["from_amount"].help_text = "Kassadan ayriladigan summa"
        self.fields["to_amount"].help_text = "Kassaga qo'shiladigan summa"
        # Not required, and not the record: left blank the two sums still say what the
        # kurs was, and on a same-currency move there is no rate to ask for at all
        # (the JS hides the box for exactly that case).
        self.fields["exchange_rate"].required = False
        self.fields["exchange_rate"].help_text = (
            "Yozsangiz ikkinchi summa o'zi hisoblanadi — keyin uni qo'lda "
            "to'g'rilasa ham bo'ladi")
        # What the JS needs to do that sum, marked here beside the fields it belongs
        # to rather than hunted for by name in the template.
        self.fields["exchange_rate"].widget.attrs["data-swap-rate"] = ""
        self.fields["from_amount"].widget.attrs["data-swap-from"] = ""
        self.fields["to_amount"].widget.attrs["data-swap-to"] = ""
        self.fields["from_currency"].widget.attrs["data-swap-from-currency"] = ""
        self.fields["to_currency"].widget.attrs["data-swap-to-currency"] = ""

    def _positive(self, name):
        amount = self.cleaned_data.get(name)
        if amount is not None and amount <= 0:
            self.add_error(name, "Summa musbat bo'lishi kerak")
        return amount

    def clean(self):
        cleaned = super().clean()
        self._positive("from_amount")
        self._positive("to_amount")
        # A move from a heap to itself takes money out and puts the same money back:
        # it changes nothing, and saved it would only clutter the daftar with a row
        # that means "nothing happened". Almost always a half-filled form.
        if (cleaned.get("from_method") == cleaned.get("to_method")
                and cleaned.get("from_currency") == cleaned.get("to_currency")):
            raise forms.ValidationError(
                "Pul o'zi turgan joyning o'ziga o'tkazilmaydi — usul yoki valyuta "
                "boshqa bo'lishi kerak.")
        # A blank kurs is the ordinary case, not an error: on a crossing row the two
        # sums restate it on save anyway, and on a same-currency move there was never
        # a rate to strike — the column still needs a value to give the row's twin
        # figure one, so it inherits (an edit keeps its own, a new row takes the last
        # one anybody typed).
        if not cleaned.get("exchange_rate") or cleaned["exchange_rate"] <= 0:
            cleaned["exchange_rate"] = (self.instance.exchange_rate if self.instance.pk
                                        else latest_exchange_rate())
        return cleaned


class CustomsAgentForm(forms.ModelForm):
    class Meta:
        model = CustomsAgent
        fields = ["name", "phone", "note"]
        widgets = {
            "phone": phone_intl_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "name": forms.TextInput(attrs={"autocomplete": "off",
                                           "placeholder": "Masalan: Bahrom aka"}),
        }

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))


class CustomsPaymentForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """Money we send a bojxonachi. No ceiling and no attempt to hold it to an
    estimate: the whole point is that nobody knows the real figure yet, so ~40 mln
    goes out for a truck and the difference settles later against what was spent.

    No kurs is asked, in EITHER direction — the same rule the hamkor and mijoz forms
    follow, taken to its end. Those ask only when the money crosses into the other
    currency, because that is the one case the rate decides how much of the thing is
    settled. A bojxonachi holds two heaps rather than one float (see CustomsAgent),
    so a so'm to'lov lands in the so'm heap and a dollar one in the dollar heap, and
    nothing ever crosses. The row still gets a rate — both money columns have to
    hold something for the kassa's converted total — but it is inherited, exactly as
    a kelishuv paid in its own currency inherits one.

    No `fee_bearer`, for the reason `SupplierPaymentForm` gives: on money going OUT
    the bank's cut is always ours. The bojxonachi has to end up holding the figure
    we said we were sending — they are about to spend it on our truck — so the foiz
    rides on top of it and the kassa is out the extra."""

    class Meta:
        model = CustomsPayment
        fields = ["agent", "shipment", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "note"]
        widgets = {"date": date_widget()}
        labels = {"amount": "Yuboriladigan summa"}
        help_texts = {
            "shipment": "Bo'sh qoldirilsa — umumiy to'ldirish, yukka bog'lanmaydi",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent"].label_from_instance = customs_agent_option_label
        self.fields["shipment"].label_from_instance = customs_shipment_option_label
        self.fields["shipment"].empty_label = "Yukka bog'lanmagan"
        self.fields["shipment"].queryset = (
            Shipment.objects.select_related("contract__partner")
            .prefetch_related("customs_payments", "expenses"))
        # An EMPTY settlement currency, which the base.html toggle reads as "this
        # money crosses nothing, ever" and keeps the kurs box hidden in both
        # directions. Hidden rather than removed, the same way a hamkor to'lov
        # hides it: a rate the operator does supply still stands.
        self.fields["currency"].widget.attrs["data-settled-against"] = ""
        # The column's default is dollars, which is right for every other row in the
        # app and wrong for this one — bojxona is overwhelmingly paid in so'm. Only
        # a NEW row is steered; an existing one opens in the currency it was sent in.
        if not self.instance.pk:
            self.initial.setdefault("currency", Currency.UZS)

    def clean(self):
        # Seeded before the money mixin converts, and unconditionally: nothing this
        # form takes ever crosses a currency, so there is no case left where a rate
        # has to be demanded. A rate the operator DID supply always stands, and an
        # edit keeps the rate the to'lov was booked at rather than re-rating a figure
        # already on the books.
        if (self.cleaned_data.get("exchange_rate") or Decimal("0")) <= 0:
            self.cleaned_data["exchange_rate"] = (
                self.instance.exchange_rate if self.instance.pk else latest_exchange_rate())
        return super().clean()


def customs_agent_option_label(agent):
    """Bojxonachi <option>: the name and what they are holding — the fact you need
    when deciding how much to send for the next truck.

    Per currency, because that is what they are actually holding. A dollar heap and
    a so'm heap read as two states side by side rather than as one figure that is
    half conversion; somebody carrying both, in opposite directions, says so."""
    parts = [f"{_unit(currency, amount)} qoldiq"
             for currency, amount in agent.held_by_currency()]
    parts += [f"{_unit(currency, amount)} bizning qarzimiz"
              for currency, amount in agent.owed_by_currency()]
    return f"{agent.name} · {' · '.join(parts) or 'qoldiq yo\'q'}"


def _unit(currency, amount):
    """A figure with its unit, short enough to sit inside an <option>. The same
    shape logist_option_label uses, with the so'm side spelled out."""
    return f"{_clean_number(amount)} {'so\'m' if currency == Currency.UZS else '$'}"


def customs_shipment_option_label(shipment):
    """Yuk <option>: the code and what is already on it for bojxona.

    A load that has had money sent for it once is the one case picking from this
    list goes wrong — a second 40 mln against the same truck reads as a 40 mln
    overspend later — so the figure is on the option rather than a screen away."""
    label = f"#{shipment.pk} · {shipment.contract.code}"
    sent = " · ".join(_unit(currency, amount)
                      for currency, amount in shipment.customs_sent_by_currency())
    return f"{label} · {sent} yuborilgan" if sent else label


class ReturnSettlementPayForm(forms.Form):
    """Handing a mijoz back money we promised them on a vazvrat.

    Asks for a SUMMA rather than just confirming, because the kassa does not always
    hold the whole promise on the day it falls due. Paying part of it leaves the rest
    standing as the promise it already was — same due date, same mijoz — so a debt
    that is being worked off never quietly disappears from "Biz qarzdormiz".

    The figure is in the vazvrat's own valyuta and nothing else: the promise was
    struck when the goods came back, and paying a so'm promise in dollars at today's
    kurs hands over a sum nobody agreed to."""

    amount = forms.DecimalField(label="To'lanadigan summa", max_digits=18,
                                decimal_places=2, min_value=Decimal("0.01"))
    method = forms.ChoiceField(label="To'lov usuli", choices=PayMethod.choices)
    date = forms.DateField(label="To'langan sana", widget=date_widget(),
                           initial=timezone.localdate)

    def __init__(self, *args, settlement=None, **kwargs):
        self.settlement = settlement
        super().__init__(*args, **kwargs)
        _group_thousands(self.fields["amount"])
        no_future_date(self.fields["date"])
        if settlement is not None:
            self.fields["amount"].initial = settlement.amount_own
            self.fields["method"].initial = settlement.method
            self.fields["amount"].widget.attrs["data-suffix"] = currency_suffix(
                settlement.currency)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        owed = self.settlement.amount_own
        if amount > owed:
            raise forms.ValidationError(
                f"Bu vazvratdan qolgani {som(owed) if self.settlement.currency == Currency.UZS else usd(owed)}"
                f" — undan ko'p qaytarib bo'lmaydi.")
        return amount

    @property
    def is_full(self):
        """Whether this pays the promise off entirely — the case that needs no new
        row, only a date on the one that is already there."""
        return self.cleaned_data["amount"] >= self.settlement.amount_own


class ReturnSettlementEditForm(forms.ModelForm):
    """Correcting a promise we have not kept yet: how it will be paid, and when.

    The SUMMA is not here on purpose. It is what the vazvrat worked out when the
    goods came back, and letting it be typed over would let "Biz qarzdormiz"
    disagree with the vazvrat it came from — pay less than the whole thing instead,
    which leaves an honest remainder, or undo the vazvrat and enter it again."""

    class Meta:
        model = ReturnSettlement
        fields = ["method", "due_date"]
        labels = {"due_date": "To'lashimiz kerak bo'lgan sana"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_date"].widget = date_widget()
        self.fields["due_date"].required = True
        self.fields["due_date"].help_text = (
            "Muddatga ulgurmasangiz shu yerdan keyingi kunga suring")
