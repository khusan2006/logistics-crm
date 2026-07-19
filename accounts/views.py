from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator

from crm.models import AuditLog
from crm.utils import form_reload, form_response, form_success

from .decorators import role_required
from .forms import LoginForm, UserForm
from .models import User


@method_decorator(login_not_required, name="dispatch")
class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


@role_required(User.Role.ADMIN)
def user_list(request):
    page = Paginator(User.objects.all().order_by("username"), 30).get_page(request.GET.get("page"))
    return render(request, "accounts/user_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            user = form.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Foydalanuvchi", user.pk,
                f"Yangi foydalanuvchi: {user.username}",
            )
            messages.success(request, "Foydalanuvchi qo'shildi")
            return form_success(request, reverse("user_list"))
        return form_response(request, form, "Yangi foydalanuvchi", invalid=True)
    return form_response(request, form, "Yangi foydalanuvchi")


@role_required(User.Role.ADMIN)
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user)
    title = "Foydalanuvchini tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Foydalanuvchi", user.pk,
                f"Foydalanuvchi tahrirlandi: {user.username}",
            )
            messages.success(request, "Foydalanuvchi yangilandi")
            return form_reload(request, reverse("user_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)
