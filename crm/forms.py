import json
import re
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.urls import reverse_lazy
from django.utils import timezone

from .models import (
    LEGACY_RATE, Contract, ContractLine, Currency, Customer, CustomerPayment,
    CustomsAgent, CustomsPayment, Kapital, Logist,
    LogistPayment, Partner,
    FeeBearer, PayMethod, Reservation, Return, Sale, Shipment, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment,
    arrived_lots, brand_on_hand_kg, brand_stock_costed, bron_brands, convert_pair,
    customer_balance_by_currency, latest_exchange_rate,
)
from .formatting import normalize_container, phone_intl_widget, validate_intl_phone
from .templatetags.crm_extras import rate, som, usd


def date_widget(**attrs):
    """A <input type="date"> that renders ISO.

    The browser only understands yyyy-mm-dd there; Django otherwise formats the
    value for the active locale ("08.07.2026"), which the input rejects and shows
    as blank — so an edit form looked empty and saving it wiped the date.
    """
    return forms.DateInput(attrs={"type": "date", **attrs}, format="%Y-%m-%d")


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
    field_groups = []

    def rendered_fields(self):
        groups = {names[0]: (legend, names) for legend, names in self.field_groups}
        grouped = {name for _, names in self.field_groups for name in names}
        items = []
        for field in self.visible_fields():
            if field.name in groups:
                legend, names = groups[field.name]
                items.append({
                    "group": True, "legend": legend,
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


class TruckPlanForm(forms.ModelForm):
    """Just the planned truck count. Separate from ContractForm so the template can
    render it after the Mahsulotlar rows — the main form is emitted above them."""

    class Meta:
        model = Contract
        fields = ["planned_trucks"]
        widgets = {"planned_trucks": forms.NumberInput(
            attrs={"min": "1", "placeholder": "Masalan: 2"})}

    def clean_planned_trucks(self):
        count = self.cleaned_data.get("planned_trucks")
        if count is not None and count < 1:
            raise forms.ValidationError("Kamida 1 bo'lishi kerak")
        return count


class ContractLineForm(PriceEntryFormMixin, forms.ModelForm):
    """One "Mahsulot" row on the kelishuv form.

    Neither a valyuta nor a kurs is asked for here any more. The narx is in the
    kelishuv's currency by definition, and a row settled in the currency it was
    agreed in needs no rate to say what is owed — so the row inherits both from the
    header instead of offering a picker that could disagree with it."""

    class Meta:
        model = ContractLine
        fields = ["brand", "kg", "price"]
        widgets = {
            "brand": forms.TextInput(attrs={"placeholder": "Masalan: 2102 repak"}),
            "kg": forms.NumberInput(attrs={"placeholder": "0"}),
            # data-currency-label marks the box as one whose unit follows the
            # header's Valyuta. The label below is the one the page is SERVED with —
            # right on a reload and after a validation error — and base.html retitles
            # it live from this attribute while the operator is still picking.
            "price": forms.NumberInput(attrs={"step": "0.0001", "placeholder": "0.0000",
                                              "data-currency-label": "1 kg narxi"}),
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
        fields = ["contract", "status", "sent", "eta", "arrived", "logist",
                  "responsible", "driver_name", "driver_phone", "transport",
                  "container", "note"]
        widgets = {
            "contract": forms.Select(attrs={"data-contract-source": ""}),
            "sent": date_widget(),
            "eta": date_widget(),
            "arrived": date_widget(),
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
    field_order = ["contract", "status", "sent", "eta", "arrived",
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


class SupplierPaymentForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """A to'lov against one kelishuv.

    The kurs is asked for only when the money crosses a currency boundary — paying a
    so'm kelishuv in so'm settles it at face value, and there is nothing to convert.
    It IS asked when the two differ, because then the rate is what decides how much
    of the qarz the money actually clears.

    A rate entered that way is then frozen: the to'lov keeps the kurs it was made at
    however the market moves afterwards, so a kelishuv that was square last week
    cannot re-open itself this week. Correcting it is a deliberate edit of the
    to'lov, not something that happens on its own."""

    class Meta:
        model = SupplierPayment
        fields = ["contract", "date", "currency", "amount", "exchange_rate",
                  "commission_percent", "method", "fee_percent", "fee_bearer", "note"]
        widgets = {
            "date": date_widget(),
            "contract": ContractChoiceSelect(attrs={"data-contract-currency": ""}),
            "commission_percent": forms.NumberInput(attrs={
                "data-commission-percent": "", "step": "0.01", "min": "0", "max": "100",
                "placeholder": "0"}),
        }
        labels = {"amount": "Hamkor oladigan summa"}

    fee_counterparty = "Hamkordan ushlansin"

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

    def clean_commission_percent(self):
        percent = self.cleaned_data.get("commission_percent")
        if percent is None:
            return Decimal("0")
        if percent < 0:
            raise forms.ValidationError("Foiz manfiy bo'la olmaydi")
        if percent > 100:
            raise forms.ValidationError("Foiz 100 dan oshmasligi kerak")
        return percent

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
        # Paying before a yuk is sent is normal (avans), so the ceiling is the whole
        # kelishuv's value, not the goods shipped so far. The cap is on what the
        # hamkor RECEIVES — the middleman's cut rides on top and is not part of it.
        #
        # Measured in the KELISHUV's currency, not the dollar column: a so'm kelishuv
        # paid off in so'm still leaves a dollar remainder whenever the kurs has moved
        # since it was struck, and the form would go on demanding money that is not
        # owed. The to'lov's own converted value is what settles it.
        if contract and amount is not None and not self.errors:
            paid = cleaned.get("amount_uzs") if contract.is_som else amount
            left = contract.payable_left_own
            if self.instance.pk and self.instance.contract_id == contract.pk:
                left += (self.instance.amount_uzs if contract.is_som
                         else self.instance.amount)
            if paid is not None and paid > left:
                shown = som(left) if contract.is_som else usd(left)
                self.add_error(
                    "amount",
                    f"Kelishuv qiymatidan oshib ketdi (to'lash mumkin: {shown})")
        return cleaned


class SaleCreateForm(BronDrawFormMixin, InheritedRateMixin,
                     PriceEntryFormMixin, forms.ModelForm):
    """New sales are entered by BRAND, not lot: the view consumes the oldest
    arrived lots first (FIFO), splitting the kg across lots — one Sale row per
    lot slice, each snapshotting its own lot's landed cost."""

    brand = forms.ChoiceField(label="Marka (ombordan)")

    class Meta:
        model = Sale
        fields = ["customer", "brand", "kg", "currency", "price", "exchange_rate",
                  "date", "debt_deadline", "note"]
        widgets = {
            "date": date_widget(),
            "debt_deadline": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "customer": forms.Select(attrs={"data-quick-add-url": reverse_lazy("customer_quick_create"),
                                            "data-quick-add-label": "Yangi mijoz"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # marka · kelishuv kod · qolgan kg · tannarx — the informative shape of the
        # yuk and kelishuv dropdowns, with no filler words. _clean_number keeps kg
        # readable ("24000", not Decimal.normalize()'s "2.4E+4").
        # The kg offered is what is physically on the shelf. Anything bronned is
        # named after it as a warning, not deducted: the granula may be sold to
        # whoever is in front of the operator, bron or no bron.
        costed = brand_stock_costed()
        self.fields["brand"].choices = [
            (row["brand"],
             f"{row['brand']} · {', '.join(row['codes'])} · "
             f"{_clean_number(row['on_hand'])} kg omborda"
             + (f" ({_clean_number(row['reserved'])} kg bronlangan)" if row["reserved"] else "")
             + f" · {_clean_number(row['cost'].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))} $/kg")
            for row in costed if row["on_hand"] > 0
        ]

    def clean(self):
        cleaned = super().clean()
        brand, kg = cleaned.get("brand"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if brand and kg is not None and kg > 0:
            # The shelf is the only ceiling. A bron is a promise between the operator
            # and a mijoz, and the operator is the one who decides whether to keep it
            # today — granula that was refused to a buyer standing at the counter is
            # a sale lost to a rule that was never the mijoz's.
            available = brand_on_hand_kg(brand)
            if kg > available:
                self.add_error(
                    "kg", f"Ombor qoldig'idan oshmasligi kerak "
                          f"({_clean_number(available)} kg)")
        return cleaned


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
            "customer": forms.Select(attrs={"data-quick-add-url": reverse_lazy("customer_quick_create"),
                                            "data-quick-add-label": "Yangi mijoz"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = arrived_lots()
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
            # lets the modal JS add a "+ Yangi mijoz" inline quick-create next to it
            "customer": forms.Select(attrs={"data-quick-add-url": reverse_lazy("customer_quick_create"),
                                            "data-quick-add-label": "Yangi mijoz"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["line"].queryset = arrived_lots()

    def clean(self):
        cleaned = super().clean()
        line, kg = cleaned.get("line"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if line and line.arrived is None:
            self.add_error("line", "Faqat kelgan (arrived) lotdan sotish mumkin")
        if line and line.arrived is not None and kg is not None and kg > 0:
            available = line.available_kg
            if self.instance.pk and self.instance.line_id == line.pk:
                available += self.instance.kg
            if kg > available:
                self.add_error("kg", f"Ombor qoldig'idan oshmasligi kerak ({available} kg)")
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


class ReturnForm(PriceEntryFormMixin, forms.ModelForm):
    """Sale comes from the view (URL `?sale=`), not from the form — the field list
    deliberately excludes it."""

    class Meta:
        model = Return
        fields = ["kg", "currency", "price", "exchange_rate", "date", "restock", "note"]
        widgets = {
            "date": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, sale=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sale = sale or getattr(self.instance, "sale", None)
        # Default to the sale's own narx AND its currency + kurs: crediting a so'm
        # sale back at today's rate would refund a different sum than was charged.
        if self.sale and not self.instance.pk:
            self.initial.setdefault("price", self.sale.price)
            self.initial.setdefault("currency", self.sale.currency)
            self.initial.setdefault("exchange_rate", self.sale.exchange_rate)
            if self.sale.currency == Currency.UZS:
                self.initial["price"] = self.sale.price_uzs

    def clean(self):
        cleaned = super().clean()
        kg = cleaned.get("kg")
        if self.sale is None:
            raise forms.ValidationError("Sotuv topilmadi")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if kg is not None and kg > 0:
            already_returned = sum(
                (r.kg for r in self.sale.returns.exclude(pk=self.instance.pk)), Decimal("0"))
            available = self.sale.kg - already_returned
            if kg > available:
                self.add_error("kg", f"Qaytarish sotilgan kg dan oshmasligi kerak ({available} kg)")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.sale = self.sale
        if commit:
            obj.save()
        return obj


def _customer_payer_field(field):
    """Point a mijoz select at the balance-annotated options.

    balance walks sotuvlar (minus qaytarishlar) and past to'lovlar in Python, so the
    rows every option needs are fetched once rather than per mijoz."""
    field.queryset = Customer.objects.prefetch_related("sales__returns", "customer_payments")
    field.label_from_instance = customer_option_label


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


class BaseCustomerPaymentFormSet(forms.BaseModelFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        kept = [f for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if not kept:
            raise forms.ValidationError("Kamida bitta to'lov kiritilishi kerak")


CustomerPaymentFormSet = forms.modelformset_factory(
    CustomerPayment, form=CustomerPaymentRowForm, formset=BaseCustomerPaymentFormSet,
    extra=1, can_delete=True)


def payer_choices(category):
    """Who could have paid a xarajat of THIS turkum out of money we already sent
    them — bojxonachilar for a bojxona, logistlar for a transport.

    One list per box rather than everybody under two headings. The two roles do not
    overlap in practice: a bojxonachi clears loads and a logist pays drivers, and
    offering both in both boxes turns a two-item pick into a scan of every outside
    party in the books for a choice that only ever had one right answer.

    The kassa is first and is what the box rests on. It is not a placeholder — it is
    the answer for most xarajatlar, and the one this form gave for its whole life
    before the picker existed."""
    if category == ShipmentExpense.Category.CUSTOMS:
        rows = CustomsAgent.objects.all()
        prefix = "customs"
    elif category == ShipmentExpense.Category.TRANSPORT:
        rows = Logist.objects.all()
        prefix = "logist"
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
        return cleaned

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
    rate is what decides how much the logist ends up holding."""

    #: The currency the logist's balance is kept in — see the class docstring.
    float_currency = Currency.USD
    fee_counterparty = "Logistdan ushlansin"

    class Meta:
        model = LogistPayment
        fields = ["logist", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "fee_bearer", "note"]
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
    a kelishuv paid in its own currency inherits one."""

    fee_counterparty = "Bojxonachidan ushlansin"

    class Meta:
        model = CustomsPayment
        fields = ["agent", "shipment", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "fee_bearer", "note"]
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
