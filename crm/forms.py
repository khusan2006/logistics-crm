import re
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.urls import reverse_lazy
from django.utils import timezone

from .models import (
    Contract, ContractLine, Currency, Customer, CustomerPayment, Partner, Reservation, Return,
    Sale, Shipment, ShipmentExpense, ShipmentLeg, ShipmentLine, ShipmentStatus, SupplierPayment,
    arrived_lots, brand_stock_costed, convert_pair,
)
from .formatting import normalize_container, phone_intl_widget, validate_intl_phone


def date_widget(**attrs):
    """A <input type="date"> that renders ISO.

    The browser only understands yyyy-mm-dd there; Django otherwise formats the
    value for the active locale ("08.07.2026"), which the input rejects and shows
    as blank — so an edit form looked empty and saving it wiped the date.
    """
    return forms.DateInput(attrs={"type": "date", **attrs}, format="%Y-%m-%d")


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
            # Deliberately NOT required at field level: a bron with no narx agreed
            # yet has nothing to convert, so demanding a kurs there would block a
            # legitimate entry. clean() raises whenever a value IS present.
            self.fields["exchange_rate"].required = False
        if self.typed_field in self.fields:
            field = self.fields[self.typed_field]
            field.widget.attrs["data-money-amount"] = ""
            # The column is canonical USD, but the operator now types whichever
            # currency the Valyuta picker says — a hard-coded "(USD)" on the input
            # would be a lie the moment they choose So'm.
            field.label = re.sub(r"\s*\((USD|so'm)\)", "", str(field.label))

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

    No hamkor name: the code already starts with it (abulqosim-2 · abulqosim read
    as a stutter)."""
    prices = sorted({ln.price for ln in contract.lines.all()})
    if not prices:
        price = "—"
    elif len(prices) == 1:
        price = f"{_clean_number(prices[0])} $/kg"
    else:
        price = f"{_clean_number(prices[0])}–{_clean_number(prices[-1])} $/kg"
    return (f"{contract.code} · {contract.brand_summary} · "
            f"{_clean_number(contract.remaining_kg)} kg qolgan · {price} · "
            f"jami {_clean_number(contract.kg)} kg")


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


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        # No origin/destination: every run is Eron → O'zbekiston (model defaults).
        fields = ["contract", "status", "sent", "eta", "responsible",
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

    def clean_driver_phone(self):
        return validate_intl_phone(self.cleaned_data.get("driver_phone"))

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


class SupplierPaymentForm(MoneyEntryFormMixin, forms.ModelForm):
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
        costed = brand_stock_costed()
        self.stock = {b: avail for b, avail, _, _ in costed}
        self.fields["brand"].choices = [
            (b, f"{b} · {', '.join(kods)} · {_clean_number(avail)} kg · "
                f"{_clean_number(cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))} $/kg")
            for b, avail, cost, kods in costed
        ]

    def clean(self):
        cleaned = super().clean()
        brand, kg = cleaned.get("brand"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if brand and kg is not None and kg > 0:
            available = self.stock.get(brand, Decimal("0"))
            if kg > available:
                self.add_error("kg", f"Ombor qoldig'idan oshmasligi kerak ({available} kg)")
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
            self.add_error("kg", f"Bu lotning qoldig'idan oshmasligi kerak "
                                 f"({lot.available_kg.normalize()} kg)")
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
    """A reservation can target an arrived OR in-transit lot — sold_kg is 0 on an
    in-transit lot, so reservable there is simply kg minus other active reservations."""

    #: the narx is optional on a bron — the price can be agreed later
    allow_blank = True

    class Meta:
        model = Reservation
        fields = ["customer", "line", "kg", "currency", "price", "exchange_rate", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        cleaned = super().clean()
        line, kg = cleaned.get("line"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if line and kg is not None and kg > 0:
            other_reserved = sum(
                (r.kg for r in line.reservations.filter(status="active").exclude(pk=self.instance.pk)),
                Decimal("0"),
            )
            reservable = line.kg - line.sold_kg - other_reserved
            if kg > reservable:
                self.add_error("kg", f"Bron miqdori qolgan kg dan oshmasligi kerak ({reservable} kg)")
        return cleaned


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


class CustomerPaymentForm(MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = CustomerPayment
        fields = ["customer", "date", "currency", "amount", "exchange_rate",
                  "method", "fee_percent", "note"]
        widgets = {"date": date_widget()}


class ExpenseTargetForm(forms.Form):
    """The header of a multi-row xarajat modal: which yuk, and the date they all
    share. Xarajatlar are entered per trip, so repeating the date on every row
    only invited them to disagree.

    The yuk rides here because the rows say nothing about which load they belong
    to and the modal posts to a path with no query string."""

    shipment = forms.ModelChoiceField(queryset=Shipment.objects.all(),
                                      widget=forms.HiddenInput)
    date = forms.DateField(label="Sana", widget=date_widget(),
                           initial=timezone.localdate)


class ShipmentExpenseRowForm(MoneyEntryFormMixin, forms.ModelForm):
    """One xarajat row. Ordered so Turkum / Valyuta / To'lov usuli land on a line
    of their own — see .lineset--expense in the stylesheet."""

    field_order = ["amount", "category", "currency", "method", "exchange_rate",
                   "fee_percent", "note"]

    class Meta:
        model = ShipmentExpense
        # No date: it is shared, so the modal asks once in ExpenseTargetForm.
        fields = ["category", "currency", "amount", "exchange_rate", "method",
                  "fee_percent", "note"]
        widgets = {"note": forms.TextInput(attrs={"placeholder": "Ixtiyoriy"})}


class BaseExpenseFormSet(forms.BaseModelFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        kept = [f for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        if not kept:
            raise forms.ValidationError("Kamida bitta xarajat kiritilishi kerak")


ShipmentExpenseFormSet = forms.modelformset_factory(
    ShipmentExpense, form=ShipmentExpenseRowForm, formset=BaseExpenseFormSet,
    extra=1, can_delete=True)


class ShipmentExpenseForm(MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = ShipmentExpense
        fields = ["shipment", "date", "category", "currency", "amount",
                  "exchange_rate", "method", "fee_percent", "note"]
        widgets = {"date": date_widget(),
                   "shipment": forms.HiddenInput()}

    def save(self, commit=True):
        obj = super().save(commit=False)
        if commit:
            obj.save()
        return obj
