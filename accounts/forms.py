from django.contrib.auth.forms import AuthenticationForm


class LoginForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Login yoki parol noto'g'ri. Qayta urinib ko'ring.",
        "inactive": "Bu hisob faol emas.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Login"
        self.fields["password"].label = "Parol"
