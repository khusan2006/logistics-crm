from decimal import ROUND_HALF_UP, Decimal

from django import forms

from .models import (
    Contract, Currency, Customer, Partner, Shipment, ShipmentExpense, ShipmentStatus, SupplierPayment,
)


class PartnerForm(forms.ModelForm):
    class Meta:
        model = Partner
        fields = ["name", "phone", "city", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "address", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}


class ContractForm(forms.ModelForm):
    class Meta:
        model = Contract
        fields = ["partner", "brand", "kg", "price", "created", "deadline", "note"]
        widgets = {
            "created": forms.DateInput(attrs={"type": "date"}),
            "deadline": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

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
    carry the entry-time facts. Reused by the expense form."""

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


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["contract", "kg", "status", "sent", "eta", "transport", "container", "note"]
        widgets = {
            "sent": forms.DateInput(attrs={"type": "date"}),
            "eta": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

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
