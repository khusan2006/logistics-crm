from django import forms

from .models import Contract, Partner


class PartnerForm(forms.ModelForm):
    class Meta:
        model = Partner
        fields = ["name", "phone", "city", "note"]
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
