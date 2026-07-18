from django.core.paginator import Paginator
from django.shortcuts import render

from accounts.decorators import role_required
from accounts.models import User

from .models import AuditLog


def dashboard(request):
    return render(request, "crm/dashboard.html")


@role_required(User.Role.ADMIN)
def audit_list(request):
    page = Paginator(AuditLog.objects.select_related("user"), 50).get_page(request.GET.get("page"))
    return render(request, "crm/audit_list.html", {"page": page})
