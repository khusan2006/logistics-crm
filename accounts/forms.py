from django import forms
from django.contrib.auth.forms import AuthenticationForm

from crm.formatting import phone_intl_widget, validate_intl_phone
from .models import User


class LoginForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Login yoki parol noto'g'ri. Qayta urinib ko'ring.",
        "inactive": "Bu hisob faol emas.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Login"
        self.fields["password"].label = "Parol"


class UserForm(forms.ModelForm):
    """Admin create/edit form. Password is required on create, optional on edit
    (blank = keep the current password)."""

    password = forms.CharField(
        label="Parol", required=False, widget=forms.PasswordInput,
        help_text="Bo'sh qoldirilsa, joriy parol saqlanadi.",
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "phone", "role"]
        widgets = {"phone": phone_intl_widget()}

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            self.fields["password"].required = True
            self.fields["password"].help_text = ""

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        is_admin = user.role == User.Role.ADMIN
        user.is_staff = is_admin
        user.is_superuser = is_admin
        if commit:
            user.save()
        return user
