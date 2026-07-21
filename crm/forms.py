from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.urls import reverse_lazy
from django.utils import timezone

from .models import (
    Contract, Currency, Customer, CustomerPayment, Partner, Reservation, Return, Sale, Shipment,
    ShipmentExpense, ShipmentLeg, ShipmentStatus, SupplierPayment, brand_stock,
)
from .formatting import normalize_container, phone_intl_widget, validate_intl_phone


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
    # Not stored — a helper so the operator can see the so'm value of the USD price
    # (and total) at today's rate. The contract price itself stays canonical USD.
    som_rate = forms.DecimalField(
        label="Dollar kursi (1$ = so'm, ixtiyoriy)", required=False, min_value=0,
        widget=forms.NumberInput(attrs={"data-som-rate": "", "step": "1",
                                        "placeholder": "Masalan: 12650"}))

    field_order = ["partner", "brand", "kg", "price", "som_rate", "created", "deadline", "note"]

    class Meta:
        model = Contract
        fields = ["partner", "brand", "kg", "price", "created", "deadline", "note"]
        widgets = {
            "created": forms.DateInput(attrs={"type": "date"}),
            "deadline": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 3}),
            "price": forms.NumberInput(attrs={"data-som-price": "", "step": "0.0001"}),
            "kg": forms.NumberInput(attrs={"data-som-kg": ""}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:  # new contract → default the date to today
            self.fields["created"].initial = timezone.localdate

    def clean(self):
        cleaned = super().clean()
        created, deadline = cleaned.get("created"), cleaned.get("deadline")
        if created and deadline and deadline < created:
            self.add_error("deadline", "Muddat kelishuv sanasidan oldin bo'la olmaydi")
        kg = cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        # Shrinking below already-shipped kg is invalid (matters from Task 7 on).
        if self.instance.pk and kg is not None and kg < self.instance.shipped_kg:
            self.add_error("kg", "Kelishilgan kg yuborilgan kg dan kam bo'la olmaydi")
        return cleaned


class ShipmentStatusForm(forms.ModelForm):
    class Meta:
        model = ShipmentStatus
        fields = ["name", "is_arrival"]


class MoneyEntryFormMixin:
    """Shared so'm→USD conversion. The user types `amount` in `currency`; after
    clean(), cleaned_data["amount"] is canonical USD and amount_original/exchange_rate
    carry the entry-time facts. Reused by the expense form.

    Also marks the currency/amount/exchange_rate widgets with data-money-*
    hooks so the base.html JS enhancer can show/hide the rate field and
    render a live USD preview (Phase 3 Task 7)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "currency" in self.fields:
            self.fields["currency"].widget.attrs["data-money-currency"] = ""
        if "exchange_rate" in self.fields:
            self.fields["exchange_rate"].widget.attrs["data-money-rate"] = ""
        if "amount" in self.fields:
            self.fields["amount"].widget.attrs["data-money-amount"] = ""

    def clean(self):
        cleaned = super().clean()
        currency, amount = cleaned.get("currency"), cleaned.get("amount")
        rate = cleaned.get("exchange_rate") or Decimal("0")
        if amount is None:
            return cleaned
        if amount <= 0:
            self.add_error("amount", "Summa musbat bo'lishi kerak")
            return cleaned
        if currency == Currency.UZS:
            if rate <= 0:
                self.add_error("exchange_rate", "So'mdagi summa uchun dollar kursini kiriting")
                return cleaned
            cleaned["amount_original"] = amount
            cleaned["amount"] = (amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            cleaned["amount_original"] = amount
            cleaned["exchange_rate"] = Decimal("0")
        return cleaned


class ContractChoiceSelect(forms.Select):
    """A contract <select> whose options carry data-remaining (qolgan kg) and
    data-deadline, so the shipment form's JS can prefill Yuboriladigan kg and
    Taxminiy kelish from the chosen kelishuv."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)  # blank choice has a plain "" value
        if instance is not None:
            # a clean kg (no trailing .000): 1000.000 → "1000", 1000.500 → "1000.5"
            rem = f"{instance.remaining_kg}"
            if "." in rem:
                rem = rem.rstrip("0").rstrip(".")
            option["attrs"]["data-remaining"] = rem
            if instance.deadline:
                option["attrs"]["data-deadline"] = instance.deadline.isoformat()
            price = f"{instance.price}"
            if "." in price:
                price = price.rstrip("0").rstrip(".")
            option["attrs"]["data-price"] = price
        return option


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["contract", "kg", "price", "status", "origin", "destination", "sent",
                  "eta", "transport", "container", "note"]
        widgets = {
            "contract": ContractChoiceSelect(attrs={"data-contract-source": ""}),
            "sent": forms.DateInput(attrs={"type": "date"}),
            "eta": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
            "transport": forms.TextInput(attrs={
                "placeholder": "01 777 AAA (UZ) yoki 12 A 345-67 (IR)"}),
            "origin": forms.TextInput(attrs={"placeholder": "Masalan: Tehron"}),
            "destination": forms.TextInput(attrs={"placeholder": "Masalan: Toshkent ombori"}),
        }
        labels = {
            "kg": "Yuboriladigan kg",
            "sent": "Jo'natiladigan sana",
            "price": "1 kg narxi (USD)",
        }

    def clean_price(self):
        price = self.cleaned_data.get("price")
        if price is not None and price <= 0:
            raise forms.ValidationError("Narx musbat bo'lishi kerak")
        return price

    def clean_transport(self):
        t = (self.cleaned_data.get("transport") or "").strip()
        if t:
            compact = re.sub(r"[\s\-]", "", t)
            # a plate is short, alphanumeric (Latin or Persian letters), and has a digit
            if (not re.fullmatch(r"[A-Za-z0-9؀-ۿ]{5,12}", compact)
                    or not any(c.isdigit() for c in compact)):
                raise forms.ValidationError(
                    "Transport O'zbekiston yoki Eron avto raqami bo'lishi kerak "
                    "(masalan: 01 777 AAA)")
        return t

    def clean_container(self):
        container = (self.cleaned_data.get("container") or "").strip()
        if container:
            clash = Shipment.objects.filter(container__iexact=container)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError("Bu konteyner raqami avval kiritilgan")
        return container

    def clean(self):
        cleaned = super().clean()
        contract, kg = cleaned.get("contract"), cleaned.get("kg")
        sent, eta = cleaned.get("sent"), cleaned.get("eta")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if contract and kg is not None and kg > 0:
            left = contract.remaining_kg
            if self.instance.pk and self.instance.contract_id == contract.pk:
                left += self.instance.kg
            if kg > left:
                self.add_error("kg", f"Yuk miqdori qolgan kg dan oshmasligi kerak ({left} kg)")
        if sent and eta and eta < sent:
            self.add_error("eta", "Kelish sanasi jo'natish sanasidan oldin bo'la olmaydi")
        return cleaned


class ShipmentExtendForm(forms.Form):
    new_eta = forms.DateField(label="Yangi kelish sanasi",
                              widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Kechikish sababi", max_length=255)


class ShipmentLegForm(forms.ModelForm):
    class Meta:
        model = ShipmentLeg
        fields = ["from_location", "to_location", "transport", "container",
                  "departed", "arrived", "note"]
        widgets = {
            "departed": forms.DateInput(attrs={"type": "date"}),
            "arrived": forms.DateInput(attrs={"type": "date"}),
            "from_location": forms.TextInput(attrs={"placeholder": "Masalan: Tehron"}),
            "to_location": forms.TextInput(attrs={"placeholder": "Masalan: Chegara"}),
            "transport": forms.TextInput(attrs={"placeholder": "Haydovchi ismi yoki 01 777 AAA"}),
        }

    def clean(self):
        cleaned = super().clean()
        dep, arr = cleaned.get("departed"), cleaned.get("arrived")
        if dep and arr and arr < dep:
            self.add_error("arrived", "Yetib kelgan sana jo'natilgan sanadan oldin bo'la olmaydi")
        return cleaned


class SupplierPaymentForm(MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = SupplierPayment
        fields = ["contract", "date", "currency", "amount", "exchange_rate", "method", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def clean(self):
        cleaned = super().clean()
        contract, amount = cleaned.get("contract"), cleaned.get("amount")
        if contract and amount is not None and not self.errors:
            debt = contract.debt
            if self.instance.pk and self.instance.contract_id == contract.pk:
                debt += self.instance.amount
            if amount > debt:
                self.add_error("amount", f"Ortiqcha to'lovga ruxsat berilmaydi (qarz: {debt} $)")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.amount = self.cleaned_data["amount"]
        obj.amount_original = self.cleaned_data["amount_original"]
        obj.exchange_rate = self.cleaned_data["exchange_rate"]
        if commit:
            obj.save()
        return obj


class SaleCreateForm(forms.ModelForm):
    """New sales are entered by BRAND, not lot: the view consumes the oldest
    arrived lots first (FIFO), splitting the kg across lots — one Sale row per
    lot slice, each snapshotting its own lot's landed cost."""

    brand = forms.ChoiceField(label="Marka (ombordan)")

    class Meta:
        model = Sale
        fields = ["customer", "brand", "kg", "price", "date", "debt_deadline", "note"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "debt_deadline": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
            "customer": forms.Select(attrs={"data-quick-add-url": reverse_lazy("customer_quick_create"),
                                            "data-quick-add-label": "Yangi mijoz"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stock = dict(brand_stock())
        self.fields["brand"].choices = [
            (b, f"{b} — {avail.normalize()} kg mavjud") for b, avail in self.stock.items()
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


class SaleForm(forms.ModelForm):
    class Meta:
        model = Sale
        fields = ["customer", "shipment", "kg", "price", "date", "debt_deadline", "note"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "debt_deadline": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
            # lets the modal JS add a "+ Yangi mijoz" inline quick-create next to it
            "customer": forms.Select(attrs={"data-quick-add-url": reverse_lazy("customer_quick_create"),
                                            "data-quick-add-label": "Yangi mijoz"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shipment"].queryset = Shipment.objects.filter(arrived__isnull=False)

    def clean(self):
        cleaned = super().clean()
        shipment, kg = cleaned.get("shipment"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if shipment and shipment.arrived is None:
            self.add_error("shipment", "Faqat kelgan (arrived) lotdan sotish mumkin")
        if shipment and shipment.arrived is not None and kg is not None and kg > 0:
            available = shipment.available_kg
            if self.instance.pk and self.instance.shipment_id == shipment.pk:
                available += self.instance.kg
            if kg > available:
                self.add_error("kg", f"Ombor qoldig'idan oshmasligi kerak ({available} kg)")
        return cleaned


class ReservationForm(forms.ModelForm):
    """A reservation can target an arrived OR in-transit lot — sold_kg is 0 on an
    in-transit lot, so reservable there is simply kg minus other active reservations."""

    class Meta:
        model = Reservation
        fields = ["customer", "shipment", "kg", "price", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        cleaned = super().clean()
        shipment, kg = cleaned.get("shipment"), cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if shipment and kg is not None and kg > 0:
            other_reserved = sum(
                (r.kg for r in shipment.reservations.filter(status="active").exclude(pk=self.instance.pk)),
                Decimal("0"),
            )
            reservable = shipment.kg - shipment.sold_kg - other_reserved
            if kg > reservable:
                self.add_error("kg", f"Bron miqdori qolgan kg dan oshmasligi kerak ({reservable} kg)")
        return cleaned


class ReturnForm(forms.ModelForm):
    """Sale comes from the view (URL `?sale=`), not from the form — the field list
    deliberately excludes it."""

    class Meta:
        model = Return
        fields = ["kg", "price", "date", "restock", "note"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, sale=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sale = sale or getattr(self.instance, "sale", None)
        if self.sale and not self.instance.pk and not self.initial.get("price"):
            self.initial["price"] = self.sale.price

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
        fields = ["customer", "date", "currency", "amount", "exchange_rate", "method", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.amount = self.cleaned_data["amount"]
        obj.amount_original = self.cleaned_data["amount_original"]
        obj.exchange_rate = self.cleaned_data["exchange_rate"]
        if commit:
            obj.save()
        return obj


class ShipmentExpenseForm(MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = ShipmentExpense
        fields = ["shipment", "date", "category", "currency", "amount",
                  "exchange_rate", "method", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}),
                   "shipment": forms.HiddenInput()}

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.amount = self.cleaned_data["amount"]
        obj.amount_original = self.cleaned_data["amount_original"]
        obj.exchange_rate = self.cleaned_data["exchange_rate"]
        if commit:
            obj.save()
        return obj
