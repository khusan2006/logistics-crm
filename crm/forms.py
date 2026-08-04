import re
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.urls import reverse_lazy
from django.utils import timezone

from .models import (
    LEGACY_RATE, Contract, ContractLine, Currency, Customer, CustomerPayment, Logist,
    LogistPayment, Partner,
    PayMethod, Reservation, Return, Sale, Shipment, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment,
    arrived_lots, brand_stock_costed, bron_brands, convert_pair,
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
    one and bills the kassa twice over on the way out."""

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
        formset.save(). _post_clean is the one hook every save path passes through."""
        super()._post_clean()
        if self.uzs_field in self.cleaned_data:
            setattr(self.instance, self.uzs_field, self.cleaned_data[self.uzs_field])

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


class PartnerForm(forms.ModelForm):
    class Meta:
        model = Partner
        fields = ["name", "phone", "city", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3}), "phone": phone_intl_widget()}

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "address", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3}), "phone": phone_intl_widget()}

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))


class ContractForm(forms.ModelForm):
    """The kelishuv header. The dollar kursi used to live here as a display-only
    helper that showed a so'm preview and stored nothing; each Mahsulot row now
    carries its own real kurs, so the fake one has been dropped rather than left
    beside a field that looks identical but actually saves."""

    field_order = ["partner", "created", "note"]

    class Meta:
        model = Contract
        fields = ["partner", "created", "note"]
        widgets = {
            "created": date_widget(),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:  # new contract → default the date to today
            self.fields["created"].initial = timezone.localdate


def _keep_if(queryset, predicate, keep_pk=None):
    """Narrow a select to rows the predicate accepts — plus one already-chosen row
    kept regardless, so editing an entry whose kelishuv has since closed does not
    silently drop it. The predicate reads Python properties (remaining_kg,
    payable_left), so it runs in Python and the result is re-expressed as a pk
    filter to stay a queryset the field can page and order."""
    ids = [obj.pk for obj in queryset if predicate(obj) or obj.pk == keep_pk]
    return queryset.filter(pk__in=ids)


def contract_option_label(contract):
    """Kelishuv <option>: code, products, what is still owed, the agreed price —
    a range when the products are priced differently — and the whole agreement.

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
    return (f"{contract.code} · {contract.brand_summary} · "
            f"{_clean_number(contract.remaining_kg)} kg qolgan · {price} · "
            f"jami {_clean_number(contract.kg)} kg")


def customer_option_label(customer):
    """Mijoz <option>: the name and their ostatka — the very figure the to'lov is
    being taken against, so it does not have to be looked up on the Qarzlar screen
    first. An overpaid mijoz reads as avans rather than a negative qarz, which is
    the difference between "collect this" and "we owe them this"."""
    balance, balance_uzs = customer.balance, customer.balance_uzs
    # Both currencies, because an ostatka spans sotuvlar of both and cannot pick a
    # side. An <option> is plain text with no room for a stacked twin, so the so'm
    # figure rides alongside on the same line.
    if balance > 0:
        return f"{customer.name} · qarz {usd(balance)} · {som(balance_uzs)}"
    if balance < 0:
        return f"{customer.name} · avans {usd(-balance)} · {som(-balance_uzs)}"
    return f"{customer.name} · qarzsiz"


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
    """One "Mahsulot" row on the kelishuv form. The narx is entered in whichever
    currency the kelishuv was struck in."""

    class Meta:
        model = ContractLine
        fields = ["brand", "kg", "currency", "price", "exchange_rate"]
        widgets = {
            "brand": forms.TextInput(attrs={"placeholder": "Masalan: 2102 repak"}),
            "kg": forms.NumberInput(attrs={"placeholder": "0"}),
            "price": forms.NumberInput(attrs={"step": "0.0001", "placeholder": "0.0000"}),
            "exchange_rate": forms.NumberInput(attrs={"step": "1",
                                                      "placeholder": "Masalan: 12650"}),
        }

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
        fields = ["contract", "status", "sent", "eta", "logist", "responsible",
                  "driver_name", "driver_phone", "transport", "container", "note"]
        widgets = {
            "contract": forms.Select(attrs={"data-contract-source": ""}),
            "sent": date_widget(),
            "eta": date_widget(),
            "note": forms.Textarea(attrs={"rows": 2}),
            "transport": forms.TextInput(attrs={
                "data-plate-intl": "", "autocomplete": "off", "placeholder": "01 777 AAA"}),
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
    field_order = ["contract", "status", "sent", "eta",
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
        # Dollars, at the kurs that logist's own funding was converted at: the
        # advance is paid out of money we already sent them, so re-rating it at
        # today's kurs would give it a so'm value that money never had.
        rate = logist.latest_rate
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
        _group_thousands(self.fields["driver_advance"])
        # Editing a yuk shows the advance already recorded — otherwise saving an
        # untouched form would wipe it.
        if self.instance.pk and not self.is_bound:
            advance = self.instance.expenses.filter(is_driver_advance=True).first()
            if advance:
                self.initial.setdefault("driver_advance", advance.amount)

    def clean_transport(self):
        """Free text. There used to be a plate-shaped regex here, which rejected
        anything that was not 5–12 alphanumerics with a digit — no help to an
        operator holding a waybill that says something else."""
        return (self.cleaned_data.get("transport") or "").strip()

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
        return cleaned


class ShipmentLineForm(PriceEntryFormMixin, forms.ModelForm):
    """One product on the truck."""

    #: blank narx means "use the kelishuv's" — see ShipmentLine.unit_price
    allow_blank = True

    class Meta:
        model = ShipmentLine
        fields = ["contract_line", "kg", "currency", "price", "exchange_rate"]
        widgets = {"contract_line": ContractLineChoiceSelect(attrs={"data-line-source": ""}),
                   "exchange_rate": forms.NumberInput(attrs={"step": "1"})}
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
        self.fields["contract_line"].label_from_instance = (
            lambda ln: f"{ln.contract.code} · {ln.brand} · "
                       f"{_clean_number(ln.remaining_kg)} kg qolgan · "
                       f"{_clean_number(ln.price)} $/kg")

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
            "transport": forms.TextInput(attrs={
                "data-plate-intl": "", "autocomplete": "off",
                "placeholder": "Haydovchi ismi yoki 01 777 AAA"}),
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
    class Meta:
        model = SupplierPayment
        fields = ["contract", "date", "currency", "amount", "exchange_rate",
                  "commission_percent", "method", "fee_percent", "note"]
        widgets = {
            "date": date_widget(),
            "commission_percent": forms.NumberInput(attrs={
                "data-commission-percent": "", "step": "0.01", "min": "0", "max": "100",
                "placeholder": "0"}),
        }
        labels = {"amount": "Hamkor oladigan summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The kassa total is driven by this, so the operator should see it named.
        self.fields["amount"].widget.attrs["data-commission-base"] = ""
        # Same rich option as the yuk form: which kelishuv, whose, what marka,
        # what is still owed in goods and at what price. A fully-paid kelishuv has
        # nothing left to pay, so it drops off — but stays when editing its own
        # to'lov.
        base = (Contract.objects.select_related("partner")
                .prefetch_related("lines__shipment_lines", "supplier_payments"))
        self.fields["contract"].queryset = _keep_if(
            base, lambda c: c.payable_left > 0, self.instance.contract_id)
        self.fields["contract"].label_from_instance = contract_option_label

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
        cleaned = super().clean()
        contract, amount = cleaned.get("contract"), cleaned.get("amount")
        # Paying before a yuk is sent is normal (avans), so the ceiling is the whole
        # kelishuv's value, not the goods shipped so far. The cap is on what the
        # hamkor RECEIVES — the middleman's cut rides on top and is not part of it.
        if contract and amount is not None and not self.errors:
            left = contract.payable_left
            if self.instance.pk and self.instance.contract_id == contract.pk:
                left += self.instance.amount
            if amount > left:
                self.add_error(
                    "amount",
                    f"Kelishuv qiymatidan oshib ketdi (to'lash mumkin: {left} $)")
        return cleaned


class SaleCreateForm(PriceEntryFormMixin, forms.ModelForm):
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
        # The ceiling is what is physically on the shelf. Bronned kg are still
        # sellable — the option only SAYS how much is promised, so the operator
        # knows they are selling out from under somebody rather than being stopped.
        costed = brand_stock_costed()
        self.stock = {row["brand"]: row["on_hand"] for row in costed}
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
            available = self.stock.get(brand, Decimal("0"))
            if kg > available:
                self.add_error(
                    "kg", f"Ombor qoldig'idan oshmasligi kerak "
                          f"({_clean_number(available)} kg)")
        return cleaned


class SaleLotForm(PriceEntryFormMixin, forms.ModelForm):
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

    def clean(self):
        cleaned = super().clean()
        lot, kg = cleaned.get("lot"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if lot and kg is not None and kg > 0 and kg > lot.available_kg:
            # The lot's own physical kg is the only ceiling. A bron on this marka
            # does not shrink it: the granula may be promised to somebody, but the
            # operator is allowed to sell it to whoever is standing in front of them.
            self.add_error("kg", f"Bu lotning qoldig'idan oshmasligi kerak "
                                 f"({_clean_number(lot.available_kg)} kg)")
        return cleaned


class SaleForm(PriceEntryFormMixin, forms.ModelForm):
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


class CustomerPaymentForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """One to'lov, edited on its own. The create screen uses the target + rows pair
    below instead — a single settlement often arrives in two currencies."""

    class Meta:
        model = CustomerPayment
        fields = ["customer", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "note"]
        widgets = {"date": date_widget()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _customer_payer_field(self.fields["customer"])
        _mark_incoming_fee(self)


class CustomerPaymentTargetForm(forms.Form):
    """The header of a multi-row to'lov modal: who paid, and on what date.

    Both are shared by every row because they describe the one settlement: a mijoz
    clearing 10 000$ by handing over 5 000$ naqd and the rest in so'm has made one
    payment on one day, in two currencies. Splitting them into rows is about how the
    money arrived, not about when or from whom."""

    customer = forms.ModelChoiceField(queryset=Customer.objects.all(), label="Mijoz")
    date = forms.DateField(label="Sana", widget=date_widget(), initial=timezone.localdate)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _customer_payer_field(self.fields["customer"])


class CustomerPaymentRowForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    """One slice of a settlement: a sum, the currency it came in, and how it moved.
    Same shape as a xarajat row — see .lineset--payment in the stylesheet."""

    # A to'lov row carries one select fewer than a xarajat (no turkum), so the foiz
    # joins Valyuta and To'lov usuli on the second line rather than being stranded.
    field_order = ["amount", "currency", "method", "fee_percent", "exchange_rate", "note"]

    class Meta:
        model = CustomerPayment
        # No mijoz, no sana: they are shared, so the modal asks once in the header.
        fields = ["currency", "amount", "exchange_rate", "method", "fee_percent", "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _mark_incoming_fee(self)


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
        # Only the unbound case: a bound form renders what was posted, and re-filling
        # it from the database would undo the operator's own edit on a failed submit.
        if not self.is_bound:
            self.prefill()

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
                 "others": self.others.get(value, [])}
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
        izoh (the shared one names what is being ADDED) and its `logist`, which this
        form never asks for: rewriting that would move money between the kassa and
        somebody's balance behind the operator's back.

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
            if row is None:
                created.append(ShipmentExpense.objects.create(
                    shipment=shipment, category=category, created_by=user,
                    date=self.cleaned_data["date"], note=note, **money))
                continue
            # As drawn: the same figure in the same valyuta, paid the same way, at
            # the same foiz. Compared against what the BOX showed rather than field
            # by field against the row, so the shared kurs — which no box can show
            # per row — cannot make an untouched submission look like an edit.
            if (typed == self.typed_amount(row) and money["currency"] == row.currency
                    and money["method"] == row.method
                    and money["fee_percent"] == row.fee_percent):
                continue
            for name, value in money.items():
                setattr(row, name, value)
            row.save(update_fields=list(money))
            updated.append(row)
        return created, updated, deleted


class ShipmentExpenseForm(FeePercentFormMixin, MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = ShipmentExpense
        fields = ["shipment", "date", "category", "logist", "currency", "amount",
                  "exchange_rate", "method", "fee_percent", "note"]
        widgets = {"date": date_widget(),
                   "shipment": forms.HiddenInput()}
        help_texts = {"logist": "Bo'sh qoldirilsa — kassadan to'langan"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logist"].empty_label = "Kassadan to'landi"
        # Default to the yuk's own logist: if a load is being run by somebody, an
        # expense on it was almost certainly paid out of that person's balance, and
        # picking the wrong one silently moves money between two people's accounts.
        shipment = self.initial.get("shipment") or getattr(self.instance, "shipment_id", None)
        if shipment and not self.instance.pk:
            match = Shipment.objects.filter(pk=getattr(shipment, "pk", shipment)).first()
            if match and match.logist_id:
                self.initial.setdefault("logist", match.logist_id)

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
    and topping it up before the loads go out is the normal way round."""

    class Meta:
        model = LogistPayment
        fields = ["logist", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "note"]
        widgets = {"date": date_widget()}
        labels = {"amount": "Yuboriladigan summa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logist"].label_from_instance = logist_option_label


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
