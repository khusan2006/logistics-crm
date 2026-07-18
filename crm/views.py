from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Max, ProtectedError, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User

from .forms import (
    ContractForm, PartnerForm, ShipmentExpenseForm, ShipmentExtendForm, ShipmentForm,
    ShipmentStatusForm, SupplierPaymentForm,
)
from .models import (
    AuditLog, Contract, Partner, Shipment, ShipmentDelay, ShipmentExpense, ShipmentStatus,
    SupplierPayment,
)
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


@role_required(User.Role.ADMIN)
def supplier_payment_list(request):
    payments = SupplierPayment.objects.select_related("contract__partner")
    contract_id = request.GET.get("contract")
    if contract_id and contract_id.isdigit():
        payments = payments.filter(contract_id=contract_id)
    page = Paginator(payments, 30).get_page(request.GET.get("page"))
    return render(request, "crm/supplier_payment_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def supplier_payment_create(request):
    initial = {}
    contract_id = request.GET.get("contract")
    if contract_id and contract_id.isdigit():
        initial["contract"] = int(contract_id)
    form = SupplierPaymentForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user
            payment.save()
            AuditLog.record(
                request.user, AuditLog.Action.PAYMENT, "Hamkor to'lovi", payment.pk,
                f"To'lov: {payment.amount}$ · kelishuv #{payment.contract_id}",
            )
            messages.success(request, "To'lov qo'shildi")
            return form_success(request, reverse("supplier_payment_list"))
        return form_response(request, form, "Yangi to'lov", invalid=True)
    return form_response(request, form, "Yangi to'lov")


@role_required(User.Role.ADMIN)
def supplier_payment_edit(request, pk):
    payment = get_object_or_404(SupplierPayment, pk=pk)
    form = SupplierPaymentForm(request.POST or None, instance=payment)
    title = "To'lovni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Hamkor to'lovi", payment.pk,
                f"To'lov tahrirlandi: {payment.amount}$ · kelishuv #{payment.contract_id}",
            )
            messages.success(request, "To'lov yangilandi")
            return form_reload(request, reverse("supplier_payment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def supplier_payment_delete(request, pk):
    payment = get_object_or_404(SupplierPayment, pk=pk)
    if request.method == "POST":
        amount, contract_id = payment.amount, payment.contract_id
        payment.delete()
        AuditLog.record(
            request.user, AuditLog.Action.DELETE, "Hamkor to'lovi", pk,
            f"To'lov o'chirildi: {amount}$ · kelishuv #{contract_id}",
        )
        messages.success(request, "To'lov o'chirildi")
        return form_reload(request, reverse("supplier_payment_list"))
    return render_confirm(
        request,
        "To'lovni o'chirish",
        f"“{payment.amount}$” to'lovi o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="supplier_payment_list",
    )


@role_required(User.Role.ADMIN)
def status_list(request):
    statuses = ShipmentStatus.objects.all()
    return render(request, "crm/status_list.html", {"statuses": statuses})


@role_required(User.Role.ADMIN)
def status_create(request):
    form = ShipmentStatusForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            status = form.save(commit=False)
            max_order = ShipmentStatus.objects.aggregate(m=Max("order"))["m"] or 0
            status.order = max_order + 1
            status.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Holat", status.pk, f"Yangi holat: {status.name}"
            )
            messages.success(request, "Holat qo'shildi")
            return form_success(request, reverse("status_list"))
        return form_response(request, form, "Yangi holat", invalid=True)
    return form_response(request, form, "Yangi holat")


@role_required(User.Role.ADMIN)
def status_edit(request, pk):
    status = get_object_or_404(ShipmentStatus, pk=pk)
    form = ShipmentStatusForm(request.POST or None, instance=status)
    title = "Holatni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Holat", status.pk, f"Holat tahrirlandi: {status.name}"
            )
            messages.success(request, "Holat yangilandi")
            return form_reload(request, reverse("status_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def status_delete(request, pk):
    status = get_object_or_404(ShipmentStatus, pk=pk)
    if request.method == "POST":
        if status.is_arrival:
            messages.error(request, "Omborga kelish holatini o'chirib bo'lmaydi")
            return redirect("status_list")
        pk_, name = status.pk, status.name
        try:
            status.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Holat", pk_, f"Holat o'chirildi: {name}")
            messages.success(request, "Holat o'chirildi")
        except ProtectedError:
            messages.error(request, "Holatga yuk biriktirilgan — o'chirib bo'lmaydi")
        return redirect("status_list")
    return redirect("status_list")


@role_required(User.Role.ADMIN)
def status_move(request, pk):
    status = get_object_or_404(ShipmentStatus, pk=pk)
    if request.method == "POST":
        direction = request.POST.get("dir")
        statuses = list(ShipmentStatus.objects.all())
        index = next((i for i, s in enumerate(statuses) if s.pk == status.pk), None)
        if index is not None:
            neighbor_index = index - 1 if direction == "up" else index + 1
            if 0 <= neighbor_index < len(statuses):
                neighbor = statuses[neighbor_index]
                status.order, neighbor.order = neighbor.order, status.order
                status.save(update_fields=["order"])
                neighbor.save(update_fields=["order"])
                AuditLog.record(
                    request.user, AuditLog.Action.UPDATE, "Holat", status.pk,
                    f"Holat tartibi o'zgartirildi: {status.name}",
                )
    return redirect("status_list")


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_list(request):
    q = request.GET.get("q", "").strip()
    status_id = request.GET.get("status", "")
    shipments = Shipment.objects.select_related("contract__partner", "status")
    if q:
        shipments = shipments.filter(
            Q(transport__icontains=q) | Q(container__icontains=q)
            | Q(contract__brand__icontains=q) | Q(contract__partner__name__icontains=q))
    if status_id:
        shipments = shipments.filter(status_id=status_id)
    page = Paginator(shipments, 30).get_page(request.GET.get("page"))
    return render(request, "crm/shipment_list.html", {
        "page": page, "q": q, "status_id": status_id,
        "statuses": ShipmentStatus.objects.all(),
    })


@role_required(User.Role.ADMIN)
def shipment_create(request):
    form = ShipmentForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            shipment = form.save(commit=False)
            shipment.created_by = request.user
            if shipment.status.is_arrival:
                shipment.arrived = timezone.localdate()
            shipment.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Yuk", shipment.pk,
                f"Yangi yuk: {shipment.contract.brand} · {shipment.kg} kg",
            )
            messages.success(request, "Yuk qo'shildi")
            return form_success(request, reverse("shipment_list"))
        return form_response(request, form, "Yangi yuk", invalid=True)
    return form_response(request, form, "Yangi yuk")


@role_required(User.Role.ADMIN)
def shipment_edit(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    form = ShipmentForm(request.POST or None, instance=shipment)
    title = "Yukni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Yuk", shipment.pk,
                f"Yuk tahrirlandi: {shipment.contract.brand} · {shipment.kg} kg",
            )
            messages.success(request, "Yuk yangilandi")
            return form_reload(request, reverse("shipment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_detail(request, pk):
    shipment = get_object_or_404(
        Shipment.objects.select_related("contract__partner", "status"), pk=pk)
    return render(request, "crm/shipment_detail.html", {"shipment": shipment})


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_extend(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    form = ShipmentExtendForm(request.POST or None)
    title = f"Yuk #{shipment.pk} — muddatni uzaytirish"
    if request.method == "POST":
        if form.is_valid():
            new_eta = form.cleaned_data["new_eta"]
            reason = form.cleaned_data["reason"]
            ShipmentDelay.objects.create(
                shipment=shipment, old_eta=shipment.eta, new_eta=new_eta,
                reason=reason, created_by=request.user)
            shipment.eta = new_eta
            shipment.save(update_fields=["eta"])
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Yuk", shipment.pk,
                f"Muddat uzaytirildi: {new_eta} ({reason})",
            )
            messages.success(request, "Kelish sanasi uzaytirildi")
            return form_success(request, reverse("shipment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@require_POST
@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_set_status(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("status"), pk=pk)
    status = get_object_or_404(ShipmentStatus, pk=request.POST.get("status"))
    if status.is_arrival and not request.user.is_admin_role:
        raise PermissionDenied
    old_name = shipment.status.name
    shipment.status = status
    shipment.arrived = (shipment.arrived or timezone.localdate()) if status.is_arrival else None
    shipment.save(update_fields=["status", "arrived"])
    AuditLog.record(request.user, AuditLog.Action.STATUS, "Yuk", shipment.pk,
                    f"{old_name} → {status.name}")
    messages.success(request, "Holat yangilandi")
    return redirect(request.POST.get("next") or "shipment_list")


@role_required(User.Role.ADMIN)
def shipment_delete(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    if request.method == "POST":
        label = f"{shipment.contract.brand} · {shipment.kg} kg"
        try:
            shipment.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Yuk", pk, f"Yuk o'chirildi: {label}")
            messages.success(request, "Yuk o'chirildi")
        except ProtectedError:
            messages.error(request, "Yukka bog'liq ma'lumot bor — o'chirib bo'lmaydi")
        return form_reload(request, reverse("shipment_list"))
    return render_confirm(
        request,
        "Yukni o'chirish",
        f"“{shipment.contract.brand} · {shipment.kg} kg” yuki o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="shipment_list",
    )


@role_required(User.Role.ADMIN)
def expense_create(request):
    initial = {"shipment": request.GET.get("shipment")}
    form = ShipmentExpenseForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            expense = form.save(commit=False)
            expense.created_by = request.user
            expense.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Yuk xarajati", expense.pk,
                f"Yangi xarajat: {expense.amount}$ · yuk #{expense.shipment_id}",
            )
            messages.success(request, "Xarajat qo'shildi")
            return form_success(request, reverse("shipment_detail", args=[expense.shipment_id]))
        return form_response(request, form, "Yangi xarajat", invalid=True)
    return form_response(request, form, "Yangi xarajat")


@role_required(User.Role.ADMIN)
def expense_edit(request, pk):
    expense = get_object_or_404(ShipmentExpense, pk=pk)
    form = ShipmentExpenseForm(request.POST or None, instance=expense)
    title = "Xarajatni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Yuk xarajati", expense.pk,
                f"Xarajat tahrirlandi: {expense.amount}$ · yuk #{expense.shipment_id}",
            )
            messages.success(request, "Xarajat yangilandi")
            return form_reload(request, reverse("shipment_detail", args=[expense.shipment_id]))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def expense_delete(request, pk):
    expense = get_object_or_404(ShipmentExpense, pk=pk)
    if request.method == "POST":
        amount, shipment_id = expense.amount, expense.shipment_id
        expense.delete()
        AuditLog.record(
            request.user, AuditLog.Action.DELETE, "Yuk xarajati", pk,
            f"Xarajat o'chirildi: {amount}$ · yuk #{shipment_id}",
        )
        messages.success(request, "Xarajat o'chirildi")
        return form_reload(request, reverse("shipment_detail", args=[shipment_id]))
    return render_confirm(
        request,
        "Xarajatni o'chirish",
        f"“{expense.amount}$” xarajati o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="shipment_list",
    )
