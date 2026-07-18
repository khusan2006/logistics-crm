from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import ProtectedError, Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from accounts.decorators import role_required
from accounts.models import User

from .forms import ContractForm, PartnerForm
from .models import AuditLog, Contract, Partner
from .utils import form_reload, form_response, form_success, render_confirm


def dashboard(request):
    return render(request, "crm/dashboard.html")


@role_required(User.Role.ADMIN)
def audit_list(request):
    page = Paginator(AuditLog.objects.select_related("user"), 50).get_page(request.GET.get("page"))
    return render(request, "crm/audit_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def partner_list(request):
    q = request.GET.get("q", "").strip()
    partners = Partner.objects.all()
    if q:
        partners = partners.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(city__icontains=q))
    page = Paginator(partners, 30).get_page(request.GET.get("page"))
    return render(request, "crm/partner_list.html", {"page": page, "q": q})


@role_required(User.Role.ADMIN)
def partner_create(request):
    form = PartnerForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            partner = form.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Hamkor", partner.pk, f"Yangi hamkor: {partner.name}"
            )
            messages.success(request, "Hamkor qo'shildi")
            return form_success(request, reverse("partner_list"))
        return form_response(request, form, "Yangi hamkor", invalid=True)
    return form_response(request, form, "Yangi hamkor")


@role_required(User.Role.ADMIN)
def partner_edit(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    form = PartnerForm(request.POST or None, instance=partner)
    title = "Hamkorni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Hamkor", partner.pk, f"Hamkor tahrirlandi: {partner.name}"
            )
            messages.success(request, "Hamkor yangilandi")
            return form_reload(request, reverse("partner_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def partner_delete(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    if request.method == "POST":
        name = partner.name
        try:
            partner.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Hamkor", pk, f"Hamkor o'chirildi: {name}")
            messages.success(request, "Hamkor o'chirildi")
        except ProtectedError:
            messages.error(request, "Hamkorga kelishuv biriktirilgan — o'chirib bo'lmaydi")
        return form_reload(request, reverse("partner_list"))
    return render_confirm(
        request,
        "Hamkorni o'chirish",
        f"“{partner.name}” hamkori o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="partner_list",
    )


@role_required(User.Role.ADMIN)
def contract_list(request):
    q = request.GET.get("q", "").strip()
    contracts = Contract.objects.select_related("partner")
    if q:
        filters = Q(brand__icontains=q) | Q(partner__name__icontains=q)
        if q.isdigit():
            filters |= Q(pk=int(q))
        contracts = contracts.filter(filters)
    page = Paginator(contracts, 30).get_page(request.GET.get("page"))
    return render(request, "crm/contract_list.html", {"page": page, "q": q})


@role_required(User.Role.ADMIN)
def contract_create(request):
    form = ContractForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            contract = form.save(commit=False)
            contract.created_by = request.user
            contract.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Kelishuv", contract.pk,
                f"Yangi kelishuv: {contract.brand}",
            )
            messages.success(request, "Kelishuv qo'shildi")
            return form_success(request, reverse("contract_list"))
        return form_response(request, form, "Yangi kelishuv", invalid=True)
    return form_response(request, form, "Yangi kelishuv")


@role_required(User.Role.ADMIN)
def contract_edit(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    form = ContractForm(request.POST or None, instance=contract)
    title = "Kelishuvni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Kelishuv", contract.pk,
                f"Kelishuv tahrirlandi: {contract.brand}",
            )
            messages.success(request, "Kelishuv yangilandi")
            return form_reload(request, reverse("contract_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def contract_delete(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    if request.method == "POST":
        brand = contract.brand
        try:
            contract.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Kelishuv", pk, f"Kelishuv o'chirildi: {brand}")
            messages.success(request, "Kelishuv o'chirildi")
        except ProtectedError:
            messages.error(request, "Kelishuvga to'lov yoki yuk biriktirilgan")
        return form_reload(request, reverse("contract_list"))
    return render_confirm(
        request,
        "Kelishuvni o'chirish",
        f"“#{contract.pk} · {contract.brand}” o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="contract_list",
    )
