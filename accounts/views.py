from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.utils.decorators import method_decorator

from .forms import LoginForm


@method_decorator(login_not_required, name="dispatch")
class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True
