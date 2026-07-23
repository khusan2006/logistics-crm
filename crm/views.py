from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, ProtectedError, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User

from .exports import xlsx_response
from .forms import (
    ContractForm, ContractLineFormSet, CustomerForm, CustomerPaymentForm, PartnerForm, ReservationForm, ReturnForm,
    SaleCreateForm, SaleForm, SaleLotForm, ShipmentExpenseForm, ShipmentExtendForm, ShipmentForm, ShipmentLineFormSet,
    ShipmentLegForm, ShipmentStatusForm, SupplierPaymentForm,
)
from .models import (
    AuditLog, Contract, ContractLine, Currency, Customer, CustomerPayment, Partner,
    PaymentAllocation,
    PayMethod, Reservation, Return, Sale, Shipment, ShipmentDelay, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment, allocate_customer_payment,
    apply_customer_advance, arrived_lots, commission_total, fifo_lots,
    trim_sale_allocations,
)
from .utils import form_reload, form_response, form_success, is_ajax, render_confirm


def dashboard(request):
    if not request.user.is_admin_role:
        return redirect("shipment_list")
    shipments = Shipment.objects.select_related("contract__partner", "status")
    contracts = Contract.objects.select_related("partner")
    total_kg = ContractLine.objects.aggregate(s=Sum("kg"))["s"] or 0
    shipped_kg = ShipmentLine.objects.aggregate(s=Sum("kg"))["s"] or 0
    arrived_kg = ShipmentLine.objects.filter(
        shipment__arrived__isnull=False).aggregate(s=Sum("kg"))["s"] or 0
    paid_total = SupplierPayment.objects.aggregate(s=Sum("amount"))["s"] or 0
    debt_total = sum((c.debt for c in contracts), Decimal("0"))
    overdue = [s for s in shipments.filter(arrived__isnull=True, eta__isnull=False)
               if s.is_overdue]
    status_counts = (ShipmentStatus.objects
                     .annotate(n=Count("shipments"))
                     .filter(n__gt=0))

    arrived_lots = shipments.filter(arrived__isnull=False)
    stock_kg = sum((s.available_kg for s in arrived_lots), Decimal("0"))
    customer_debt_total = sum(
        (c.balance for c in Customer.objects.all() if c.balance > 0), Decimal("0"))
    sales_profit_total = sum((s.profit for s in Sale.objects.all()), Decimal("0"))

    return render(request, "crm/dashboard.html", {
        "total_kg": total_kg, "shipped_kg": shipped_kg, "arrived_kg": arrived_kg,
        "paid_total": paid_total, "debt_total": debt_total, "overdue": overdue,
        "contracts": contracts[:8], "status_counts": status_counts,
        "stock_kg": stock_kg, "customer_debt_total": customer_debt_total,
        "sales_profit_total": sales_profit_total,
    })


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
def customer_list(request):
    q = request.GET.get("q", "").strip()
    customers = Customer.objects.all()
    if q:
        customers = customers.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(address__icontains=q)
        )
    page = Paginator(customers, 30).get_page(request.GET.get("page"))
    return render(request, "crm/customer_list.html", {"page": page, "q": q})


@role_required(User.Role.ADMIN)
def customer_create(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            customer = form.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Mijoz", customer.pk, f"Yangi mijoz: {customer.name}"
            )
            messages.success(request, "Mijoz qo'shildi")
            return form_success(request, reverse("customer_list"))
        return form_response(request, form, "Yangi mijoz", invalid=True)
    return form_response(request, form, "Yangi mijoz")


@require_POST
@role_required(User.Role.ADMIN)
def customer_quick_create(request):
    """Create a customer inline (from the sale/other modals) and return it as JSON,
    so the operator never has to leave the form. Reuses a same-name customer instead
    of duplicating."""
    name = (request.POST.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Ism kiriting"}, status=400)
    customer = Customer.objects.filter(name__iexact=name).first()
    created = False
    if customer is None:
        customer = Customer.objects.create(name=name, phone=(request.POST.get("phone") or "").strip())
        created = True
        AuditLog.record(request.user, AuditLog.Action.CREATE, "Mijoz", customer.pk,
                        f"Tez qo'shildi: {name}")
    return JsonResponse({"id": customer.pk, "text": str(customer), "created": created})


@role_required(User.Role.ADMIN)
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = CustomerForm(request.POST or None, instance=customer)
    title = "Mijozni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Mijoz", customer.pk, f"Mijoz tahrirlandi: {customer.name}"
            )
            messages.success(request, "Mijoz yangilandi")
            return form_reload(request, reverse("customer_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def customer_delete(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == "POST":
        name = customer.name
        try:
            customer.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Mijoz", pk, f"Mijoz o'chirildi: {name}")
            messages.success(request, "Mijoz o'chirildi")
        except ProtectedError:
            messages.error(request, "Mijozga savdo biriktirilgan — o'chirib bo'lmaydi")
        return form_reload(request, reverse("customer_list"))
    return render_confirm(
        request,
        "Mijozni o'chirish",
        f"“{customer.name}” mijozi o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="customer_list",
    )


# To'lov holati of a kelishuv, read off debt = shipped_value − paid_total. A
# kelishuv with nothing shipped yet has no payable, so it matches none of these —
# it only appears under "Hammasi" (calling it unpaid would invent a debt).
# Keyed to the kelishuv's own value: paying before a yuk is sent is normal, so
# chips that keyed off shipped value left every prepaid kelishuv matching none.
CONTRACT_PAY_FILTERS = {
    "paid": lambda c: c.total_value > 0 and c.payable_left <= 0,
    "partial": lambda c: 0 < c.paid_total < c.total_value,
    "unpaid": lambda c: c.total_value > 0 and c.paid_total == 0,
}
CONTRACT_PAY_LABELS = [("", "Hammasi"), ("paid", "To'langan"),
                       ("partial", "Qisman to'langan"), ("unpaid", "To'lanmagan")]


def _contract_code_filter(q):
    """Match a kelishuv code: `sobir-3` pins one kelishuv, a bare `3` finds every
    kelishuv numbered 3. Returns an empty Q for anything else, which OR's away."""
    if q.isdigit():
        return Q(code_number=int(q))
    slug, _, number = q.rpartition("-")
    if slug and number.isdigit():
        return Q(code_slug=slug, code_number=int(number))
    return Q()


@role_required(User.Role.ADMIN)
def contract_list(request):
    """Kelishuvlar: search plus hamkor / to'lov holati / yetkazish / muddat filters.
    Hamkor narrows in SQL; the rest read computed properties (debt, remaining_kg),
    so they run in Python over prefetched rows — the loads and the payments come in
    one query each instead of two per kelishuv."""
    q = request.GET.get("q", "").strip()
    pay = request.GET.get("pay", "").strip()
    partner_id = request.GET.get("partner", "").strip()
    # Unfinished business is the working view, so it is what you land on; "Hammasi"
    # (delivery="all") is the deliberate step out of it, not the default.
    delivery = request.GET.get("delivery", "open").strip()
    overdue = request.GET.get("overdue") == "1"

    # lines__shipment_lines feeds kg/shipped_kg/shipped_value off one query each,
    # instead of two per product per kelishuv as the filters walk every row.
    contracts = (Contract.objects.select_related("partner")
                 .prefetch_related("lines__shipment_lines", "supplier_payments"))
    if q:
        # lines__brand spans a multi-valued relation, so a kelishuv whose products
        # both match would otherwise come back twice.
        filters = (Q(lines__brand__icontains=q) | Q(partner__name__icontains=q)
                   | Q(code_slug__icontains=q) | _contract_code_filter(q))
        contracts = contracts.filter(filters).distinct()
    if partner_id.isdigit():
        contracts = contracts.filter(partner_id=int(partner_id))

    rows = list(contracts)
    if delivery == "sent":
        rows = [c for c in rows if c.is_settled]
    elif delivery == "open":
        # Qolgan = still owed goods OR still owed money — a kelishuv shipped in full
        # but not paid off is unfinished business too.
        rows = [c for c in rows if not c.is_settled]
    if overdue:
        today = timezone.localdate()
        rows = [c for c in rows if c.deadline < today and c.remaining_kg > 0]

    # Chip counts are faceted: computed before the payment filter narrows the rows,
    # so each chip shows what picking it would yield.
    pay_tabs = [{"key": key, "label": label,
                 "count": (len(rows) if not key
                           else sum(1 for c in rows if CONTRACT_PAY_FILTERS[key](c)))}
                for key, label in CONTRACT_PAY_LABELS]
    if pay in CONTRACT_PAY_FILTERS:
        rows = [c for c in rows if CONTRACT_PAY_FILTERS[pay](c)]

    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    return render(request, "crm/contract_list.html", {
        "page": page, "q": q, "pay": pay, "partner_id": partner_id,
        "delivery": delivery, "overdue": overdue, "pay_tabs": pay_tabs,
        "partners": Partner.objects.all(),
        "has_filters": bool(pay or partner_id or delivery != "open" or overdue),
    })


def _save_lines(formset, parent):
    """Persist a product formset and keep its display order matching the screen."""
    formset.instance = parent
    lines = formset.save(commit=False)
    for obj in formset.deleted_objects:
        obj.delete()
    for position, form in enumerate(formset.forms):
        if form.instance.pk or form.instance in lines:
            form.instance.position = position
    for obj in lines:
        obj.save()
    formset.save_m2m()


@role_required(User.Role.ADMIN)
def contract_create(request):
    form = ContractForm(request.POST or None)
    lines = ContractLineFormSet(request.POST or None)
    if request.method == "POST":
        if form.is_valid() and lines.is_valid():
            with transaction.atomic():
                contract = form.save(commit=False)
                contract.created_by = request.user
                contract.save()
                _save_lines(lines, contract)
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Kelishuv", contract.pk,
                f"Yangi kelishuv: {contract.code} · {contract.brand_summary}",
            )
            messages.success(request, "Kelishuv qo'shildi")
            return form_success(request, reverse("contract_list"))
        return _contract_form_response(request, form, lines, "Yangi kelishuv", invalid=True)
    return _contract_form_response(request, form, lines, "Yangi kelishuv")


def _contract_form_response(request, form, lines, title, invalid=False):
    return form_response(request, form, title, invalid=invalid,
                         extra_context={"lines": lines, "lines_legend": "Mahsulotlar"})


@role_required(User.Role.ADMIN)
def contract_edit(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    form = ContractForm(request.POST or None, instance=contract)
    lines = ContractLineFormSet(request.POST or None, instance=contract)
    title = "Kelishuvni tahrirlash"
    if request.method == "POST":
        if form.is_valid() and lines.is_valid():
            with transaction.atomic():
                form.save()
                _save_lines(lines, contract)
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Kelishuv", contract.pk,
                f"Kelishuv tahrirlandi: {contract.code} · {contract.brand_summary}",
            )
            messages.success(request, "Kelishuv yangilandi")
            return form_reload(request, reverse("contract_list"))
        return _contract_form_response(request, form, lines, title, invalid=True)
    return _contract_form_response(request, form, lines, title)


@role_required(User.Role.ADMIN)
def contract_delete(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    if request.method == "POST":
        label = f"{contract.code} · {contract.brand_summary}"
        try:
            contract.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Kelishuv", pk,
                            f"Kelishuv o'chirildi: {label}")
            messages.success(request, "Kelishuv o'chirildi")
        except ProtectedError:
            messages.error(request, "Kelishuvga to'lov yoki yuk biriktirilgan")
        return form_reload(request, reverse("contract_list"))
    return render_confirm(
        request,
        "Kelishuvni o'chirish",
        f"“{contract.code} · {contract.brand_summary}” o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
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


def _parse_alloc_picks(post):
    """Read manual allocation picks from POST fields named alloc_<sale_id>,
    ignoring blanks and zeros."""
    picks = []
    for key, value in post.items():
        if not key.startswith("alloc_"):
            continue
        value = (value or "").strip()
        if not value:
            continue
        try:
            sale_id = int(key[len("alloc_"):])
            amount = Decimal(value)
        except (ValueError, ArithmeticError):
            continue
        if amount > 0:
            picks.append((sale_id, amount))
    return picks


@role_required(User.Role.ADMIN)
def customer_payment_list(request):
    payments = CustomerPayment.objects.select_related("customer")
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        payments = payments.filter(customer_id=customer_id)
    page = Paginator(payments, 30).get_page(request.GET.get("page"))
    return render(request, "crm/customer_payment_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def customer_payment_create(request):
    initial = {}
    customer_id = request.GET.get("customer")
    customer = None
    if customer_id and customer_id.isdigit():
        initial["customer"] = int(customer_id)
        customer = Customer.objects.filter(pk=customer_id).first()
    form = CustomerPaymentForm(request.POST or None, initial=initial)
    alloc_sales = [s for s in customer.sales.all() if s.remaining > 0] if customer else None
    if request.method == "POST":
        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user
            payment.save()
            picks = _parse_alloc_picks(request.POST) if customer else None
            allocate_customer_payment(payment, picks)
            AuditLog.record(
                request.user, AuditLog.Action.PAYMENT, "Mijoz to'lovi", payment.pk,
                f"To'lov: {payment.amount}$ · mijoz {payment.customer.name}",
            )
            messages.success(request, "To'lov qo'shildi")
            return form_success(request, reverse("customer_payment_list"))
        return form_response(request, form, "Yangi to'lov", invalid=True,
                             extra_context={"alloc_sales": alloc_sales})
    return form_response(request, form, "Yangi to'lov", extra_context={"alloc_sales": alloc_sales})


@role_required(User.Role.ADMIN)
def customer_payment_edit(request, pk):
    payment = get_object_or_404(CustomerPayment, pk=pk)
    form = CustomerPaymentForm(request.POST or None, instance=payment)
    title = "To'lovni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            payment = form.save()
            payment.allocations.all().delete()
            allocate_customer_payment(payment)
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Mijoz to'lovi", payment.pk,
                f"To'lov tahrirlandi: {payment.amount}$ · mijoz {payment.customer.name}",
            )
            messages.success(request, "To'lov yangilandi")
            return form_reload(request, reverse("customer_payment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def customer_payment_delete(request, pk):
    payment = get_object_or_404(CustomerPayment, pk=pk)
    if request.method == "POST":
        amount, customer_name = payment.amount, payment.customer.name
        payment.delete()  # CASCADE clears its allocations
        AuditLog.record(
            request.user, AuditLog.Action.DELETE, "Mijoz to'lovi", pk,
            f"To'lov o'chirildi: {amount}$ · mijoz {customer_name}",
        )
        messages.success(request, "To'lov o'chirildi")
        return form_reload(request, reverse("customer_payment_list"))
    return render_confirm(
        request,
        "To'lovni o'chirish",
        f"“{payment.amount}$” to'lovi o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="customer_payment_list",
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
            return form_reload(request, reverse("status_list"))
        pk_, name = status.pk, status.name
        try:
            status.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Holat", pk_, f"Holat o'chirildi: {name}")
            messages.success(request, "Holat o'chirildi")
        except ProtectedError:
            messages.error(request, "Holatga yuk biriktirilgan — o'chirib bo'lmaydi")
        return form_reload(request, reverse("status_list"))
    return render_confirm(
        request,
        "Holatni o'chirish",
        f"“{status.name}” holati o'chiriladi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="status_list",
    )


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
    """ACTIVE loads (not yet arrived), grouped by kelishuv, with status tabs (in
    pipeline order) to switch the view. Tabs filter client-side; each row carries
    its status + overdue flag. Arrived loads live on the Yakunlangan page."""
    q = request.GET.get("q", "").strip()
    shipments = (Shipment.objects.filter(arrived__isnull=True)
                 .select_related("contract__partner", "status")
                 .prefetch_related("delays", "legs", "expenses"))
    if q:
        shipments = shipments.filter(
            Q(transport__icontains=q) | Q(container__icontains=q)
            | Q(contract__lines__brand__icontains=q) | Q(contract__partner__name__icontains=q))
    shipments = list(shipments)

    counts = {}
    overdue_count = 0
    for s in shipments:
        counts[s.status_id] = counts.get(s.status_id, 0) + 1
        if s.is_overdue:
            overdue_count += 1

    # Group the rows under their kelishuv (newest contract first, newest load first
    # inside — same recency feel as the flat list had).
    groups = []
    by_contract = {}
    for s in sorted(shipments, key=lambda s: -s.contract_id):
        g = by_contract.get(s.contract_id)
        if g is None:
            g = by_contract[s.contract_id] = {"contract": s.contract, "shipments": []}
            groups.append(g)
        g["shipments"].append(s)
    for g in groups:
        g["shipments"].sort(key=lambda s: s.created_at, reverse=True)

    statuses = list(ShipmentStatus.objects.all())  # ordered by (order, id)
    # No tab for the arrival status — reaching it moves the load to Yakunlangan.
    tabs = [{"status": st, "count": counts.get(st.pk, 0)}
            for st in statuses if not st.is_arrival]
    done_count = Shipment.objects.filter(arrived__isnull=False).count()
    return render(request, "crm/shipment_list.html", {
        "shipments": shipments, "groups": groups, "statuses": statuses, "tabs": tabs,
        "total": len(shipments), "overdue_count": overdue_count,
        "done_count": done_count, "q": q,
    })


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_done_list(request):
    """Yakunlangan yuklar: loads that reached the ombor (arrived), newest first."""
    q = request.GET.get("q", "").strip()
    shipments = (Shipment.objects.filter(arrived__isnull=False)
                 .select_related("contract__partner", "status")
                 .order_by("-arrived", "-id"))
    if q:
        shipments = shipments.filter(
            Q(transport__icontains=q) | Q(container__icontains=q)
            | Q(contract__lines__brand__icontains=q) | Q(contract__partner__name__icontains=q))
    page = Paginator(shipments, 30).get_page(request.GET.get("page"))
    return render(request, "crm/shipment_done_list.html", {"page": page, "q": q})


@role_required(User.Role.ADMIN)
def ombor(request):
    """Ombor by MARKA, one row per granula. The same marka can arrive on several
    lots at different landed costs; showing those as separate rows made the stock
    look like different products, so they merge here and the lots live inside the
    row — each still sellable on its own (a lot's own tan narx follows the sale)."""
    q = request.GET.get("q", "").strip()
    # Oldest arrival first — the FIFO consumption order sales draw from.
    lots = (arrived_lots()
            .prefetch_related("shipment__expenses", "reservations", "sales__returns")
            .order_by("shipment__arrived", "id"))
    if q:
        filters = (Q(contract_line__brand__icontains=q)
                   | Q(shipment__contract__partner__name__icontains=q))
        if q.isdigit():
            filters |= Q(shipment__contract_id=int(q))
        lots = lots.filter(filters)

    groups = []
    by_brand = {}
    for lot in lots:
        brand = lot.brand
        g = by_brand.get(brand)
        if g is None:
            g = by_brand[brand] = {"brand": brand, "lots": [], "partners": [],
                                   "kirim": Decimal("0"), "sold": Decimal("0"),
                                   "reserved": Decimal("0"), "available": Decimal("0")}
            groups.append(g)
        g["lots"].append(lot)
        g["kirim"] += lot.kg
        g["sold"] += lot.sold_kg
        g["reserved"] += lot.reserved_kg
        g["available"] += lot.available_kg
        partner = lot.shipment.contract.partner.name
        if partner not in g["partners"]:
            g["partners"].append(partner)
    for g in groups:
        costs = [lot.landed_cost_per_kg for lot in g["lots"]]
        g["cost_min"], g["cost_max"] = min(costs), max(costs)
        g["arrived_last"] = max(lot.arrived for lot in g["lots"])

    page = Paginator(groups, 30).get_page(request.GET.get("page"))
    return render(request, "crm/ombor.html", {"page": page, "q": q})


def _shipment_form_response(request, form, lines, title, invalid=False):
    return form_response(request, form, title, invalid=invalid,
                         extra_context={"lines": lines, "lines_legend": "Mahsulotlar"})


@role_required(User.Role.ADMIN)
def shipment_create(request):
    form = ShipmentForm(request.POST or None)
    lines = ShipmentLineFormSet(request.POST or None)
    if request.method == "POST":
        if form.is_valid() and lines.is_valid():
            with transaction.atomic():
                shipment = form.save(commit=False)
                shipment.created_by = request.user
                if shipment.status.is_arrival:
                    shipment.arrived = timezone.localdate()
                shipment.save()
                _save_lines(lines, shipment)
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Yuk", shipment.pk,
                f"Yangi yuk: {shipment.brand_summary} · {shipment.kg} kg",
            )
            messages.success(request, "Yuk qo'shildi")
            return form_success(request, reverse("shipment_list"))
        return _shipment_form_response(request, form, lines, "Yangi yuk", invalid=True)
    return _shipment_form_response(request, form, lines, "Yangi yuk")


@role_required(User.Role.ADMIN)
def shipment_edit(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    form = ShipmentForm(request.POST or None, instance=shipment)
    lines = ShipmentLineFormSet(request.POST or None, instance=shipment)
    title = "Yukni tahrirlash"
    if request.method == "POST":
        if form.is_valid() and lines.is_valid():
            with transaction.atomic():
                form.save()
                _save_lines(lines, shipment)
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Yuk", shipment.pk,
                f"Yuk tahrirlandi: {shipment.brand_summary} · {shipment.kg} kg",
            )
            messages.success(request, "Yuk yangilandi")
            return form_reload(request, reverse("shipment_list"))
        return _shipment_form_response(request, form, lines, title, invalid=True)
    return _shipment_form_response(request, form, lines, title)


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_detail(request, pk):
    shipment = get_object_or_404(
        Shipment.objects.select_related("contract__partner", "status"), pk=pk)
    return render(request, "crm/shipment_detail.html", {"shipment": shipment})


# --- Route legs (Yo'nalish bosqichlari) — physical movement, no money, so both
#     admins and translators manage them (translators coordinate the drivers). ---

@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def leg_create(request):
    shipment = get_object_or_404(Shipment, pk=request.GET.get("shipment") or request.POST.get("shipment"))
    form = ShipmentLegForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        leg = form.save(commit=False)
        leg.shipment = shipment
        leg.created_by = request.user
        leg.order = (shipment.legs.aggregate(m=Max("order"))["m"] or 0) + 1
        leg.save()
        AuditLog.record(request.user, AuditLog.Action.CREATE, "Yo'nalish", shipment.pk,
                        f"Bosqich: {leg.from_location} → {leg.to_location}")
        messages.success(request, "Bosqich qo'shildi")
        # reload whichever page it was opened from (loads list or the load detail)
        return form_reload(request, reverse("shipment_detail", args=[shipment.pk]))
    return form_response(request, form, "Yangi bosqich", invalid=request.method == "POST")


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def leg_edit(request, pk):
    leg = get_object_or_404(ShipmentLeg, pk=pk)
    form = ShipmentLegForm(request.POST or None, instance=leg)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.record(request.user, AuditLog.Action.UPDATE, "Yo'nalish", leg.shipment_id,
                        f"Bosqich tahrirlandi: {leg.from_location} → {leg.to_location}")
        messages.success(request, "Bosqich yangilandi")
        return form_reload(request, reverse("shipment_detail", args=[leg.shipment_id]))
    return form_response(request, form, "Bosqichni tahrirlash", invalid=request.method == "POST")


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def leg_delete(request, pk):
    leg = get_object_or_404(ShipmentLeg, pk=pk)
    shipment_id = leg.shipment_id
    if request.method == "POST":
        label = f"{leg.from_location} → {leg.to_location}"
        leg.delete()
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Yo'nalish", shipment_id,
                        f"Bosqich o'chirildi: {label}")
        messages.success(request, "Bosqich o'chirildi")
        return form_reload(request, reverse("shipment_detail", args=[shipment_id]))
    return render_confirm(
        request, "Bosqichni o'chirish",
        f"“{leg.from_location} → {leg.to_location}” bosqichi o'chiriladi.",
        "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="shipment_list")


@require_POST
@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def leg_move(request, pk):
    """Reorder a leg up/down — this is how an unplanned stop gets slotted between
    existing legs."""
    leg = get_object_or_404(ShipmentLeg, pk=pk)
    legs = list(leg.shipment.legs.all())
    index = next((i for i, x in enumerate(legs) if x.pk == leg.pk), None)
    neighbor_index = index - 1 if request.POST.get("dir") == "up" else index + 1
    if index is not None and 0 <= neighbor_index < len(legs):
        neighbor = legs[neighbor_index]
        leg.order, neighbor.order = neighbor.order, leg.order
        leg.save(update_fields=["order"])
        neighbor.save(update_fields=["order"])
        AuditLog.record(request.user, AuditLog.Action.UPDATE, "Yo'nalish", leg.shipment_id,
                        "Bosqich tartibi o'zgardi")
    return redirect(request.POST.get("next") or reverse("shipment_detail", args=[leg.shipment_id]))


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
            # Reload in place (list or detail — wherever the modal was opened from)
            # instead of redirecting to the list, since extend is often opened
            # from shipment_detail.
            return form_reload(request, reverse("shipment_list"))
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
    if is_ajax(request):
        # The list JS updates the row in place (or drops it, if the load just
        # arrived and moved to Yakunlangan) — no page reload, never stale.
        return JsonResponse({"status_id": status.pk, "arrived": shipment.arrived is not None})
    messages.success(request, "Holat yangilandi")
    return redirect(request.POST.get("next") or "shipment_list")


@role_required(User.Role.ADMIN)
def shipment_delete(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    if request.method == "POST":
        label = f"{shipment.brand_summary} · {shipment.kg} kg"
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
        f"“{shipment.brand_summary} · {shipment.kg} kg” yuki o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
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
            # reload whichever page it was opened from (loads list or the load detail)
            return form_reload(request, reverse("shipment_detail", args=[expense.shipment_id]))
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


@role_required(User.Role.ADMIN)
def sale_list(request):
    q = request.GET.get("q", "").strip()
    sales = Sale.objects.select_related("customer", "line__contract_line", "line__shipment__contract__partner")
    if q:
        filters = (Q(customer__name__icontains=q) | Q(line__contract_line__brand__icontains=q))
        if q.isdigit():
            filters |= Q(line__shipment_id=int(q))
        sales = sales.filter(filters)
    page = Paginator(sales, 30).get_page(request.GET.get("page"))
    return render(request, "crm/sale_list.html", {"page": page, "q": q})


@role_required(User.Role.ADMIN)
def sale_create(request):
    """Sale by brand: the entered kg is consumed from the oldest arrived lots
    first (FIFO), one Sale row per lot slice so each slice keeps its own lot's
    landed cost. `?lot=` (opening one lot from inside a marka in the ombor) sells
    from THAT lot instead — see sale_create_lot."""
    lot_id = request.GET.get("lot") or request.POST.get("lot")
    if lot_id and str(lot_id).isdigit():
        return sale_create_lot(request, int(lot_id))

    initial = {}
    brand = (request.GET.get("brand") or "").strip()   # marka row's Sotish shortcut
    if brand:
        initial["brand"] = brand
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        initial["customer"] = int(customer_id)
    form = SaleCreateForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            remaining = data["kg"]
            slices = []
            with transaction.atomic():
                for lot in fifo_lots(data["brand"]):
                    if remaining <= 0:
                        break
                    take = min(lot.available_kg, remaining)
                    sale = Sale.objects.create(
                        customer=data["customer"], line=lot, kg=take,
                        price=data["price"], cost_price=lot.landed_cost_per_kg,
                        date=data["date"], debt_deadline=data["debt_deadline"],
                        note=data["note"], created_by=request.user,
                    )
                    slices.append(sale)
                    remaining -= take
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Sotuv", slices[0].pk if slices else 0,
                f"Yangi sotuv (FIFO): {data['kg']} kg {data['brand']} · "
                f"{data['customer'].name} · {len(slices)} lot",
            )
            for sale in slices:  # a pre-existing advance auto-applies, oldest slice first
                apply_customer_advance(sale)
            messages.success(
                request,
                f"Sotuv qo'shildi ({len(slices)} lotdan)" if len(slices) > 1 else "Sotuv qo'shildi")
            return form_success(request, reverse("sale_list"))
        return form_response(request, form, "Yangi sotuv", invalid=True)
    return form_response(request, form, "Yangi sotuv")


def sale_create_lot(request, lot_id):
    """Sale from one chosen lot (the Sotish inside a marka in the ombor). FIFO is
    deliberately bypassed: the operator opened this lot because it is the one being
    sold — with several lots of the same marka at different landed costs, FIFO would
    silently bill a different lot's cost."""
    lot = get_object_or_404(ShipmentLine, pk=lot_id, shipment__arrived__isnull=False)
    initial = {"lot": lot.pk}
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        initial["customer"] = int(customer_id)
    title = f"Sotish · {lot.brand} (lot #{lot.pk})"
    # The lot is settled by the URL/hidden field before the form is bound, so a post
    # that lost the query string (the modal posts to a bare path) still hits the
    # same lot, and the body can never redirect the sale to another one.
    data = None
    if request.method == "POST":
        data = request.POST.copy()
        data["lot"] = lot.pk
    form = SaleLotForm(data, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            sale = Sale.objects.create(
                customer=data["customer"], line=data["lot"], kg=data["kg"],
                price=data["price"], cost_price=data["lot"].landed_cost_per_kg,
                date=data["date"], debt_deadline=data["debt_deadline"],
                note=data["note"], created_by=request.user,
            )
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Sotuv", sale.pk,
                f"Yangi sotuv (lot #{sale.line_id}): {sale.kg} kg "
                f"{sale.line.brand} · {sale.customer.name}",
            )
            apply_customer_advance(sale)
            messages.success(request, "Sotuv qo'shildi")
            return form_success(request, reverse("sale_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def sale_edit(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    form = SaleForm(request.POST or None, instance=sale)
    title = "Sotuvni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            sale = form.save(commit=False)
            sale.cost_price = sale.line.landed_cost_per_kg
            sale.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Sotuv", sale.pk,
                f"Sotuv tahrirlandi: {sale.kg} kg · {sale.customer.name}",
            )
            messages.success(request, "Sotuv yangilandi")
            return form_reload(request, reverse("sale_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def sale_delete(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    if request.method == "POST":
        label = f"{sale.kg} kg · {sale.customer.name}"
        try:
            sale.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Sotuv", pk, f"Sotuv o'chirildi: {label}")
            messages.success(request, "Sotuv o'chirildi")
        except ProtectedError:
            messages.error(request, "Sotuvga bog'liq ma'lumot bor — o'chirib bo'lmaydi")
        return form_reload(request, reverse("sale_list"))
    return render_confirm(
        request,
        "Sotuvni o'chirish",
        f"“{sale.kg} kg · {sale.customer.name}” sotuvi o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="sale_list",
    )


@role_required(User.Role.ADMIN)
def sale_detail(request, pk):
    sale = get_object_or_404(
        Sale.objects.select_related("customer", "line__contract_line", "line__shipment__contract__partner"), pk=pk)
    return render(request, "crm/sale_detail.html", {"sale": sale})


@role_required(User.Role.ADMIN)
def reservation_list(request):
    reservations = Reservation.objects.select_related("customer", "line__contract_line", "line__shipment__contract__partner")
    page = Paginator(reservations, 30).get_page(request.GET.get("page"))
    return render(request, "crm/reservation_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def reservation_create(request):
    initial = {}
    lot_id = request.GET.get("lot")
    if lot_id and lot_id.isdigit():
        initial["line"] = int(lot_id)
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        initial["customer"] = int(customer_id)
    form = ReservationForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            reservation = form.save(commit=False)
            reservation.created_by = request.user
            reservation.save()
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Bron", reservation.pk,
                f"Yangi bron: {reservation.kg} kg · {reservation.customer.name}",
            )
            messages.success(request, "Bron qo'shildi")
            return form_success(request, reverse("reservation_list"))
        return form_response(request, form, "Yangi bron", invalid=True)
    return form_response(request, form, "Yangi bron")


@role_required(User.Role.ADMIN)
def reservation_cancel(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk)
    if request.method == "POST":
        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status"])
        AuditLog.record(
            request.user, AuditLog.Action.STATUS, "Bron", reservation.pk,
            f"Bron bekor qilindi: {reservation.kg} kg · {reservation.customer.name}",
        )
        messages.success(request, "Bron bekor qilindi")
        return form_reload(request, reverse("reservation_list"))
    return render_confirm(
        request,
        "Bronni bekor qilish",
        f"“{reservation.kg} kg · {reservation.customer.name}” broni bekor qilinadi.",
        "Ha, bekor qilish",
        confirm_class="btn-danger",
        cancel_url_name="reservation_list",
    )


@require_POST
@role_required(User.Role.ADMIN)
def reservation_convert(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk)
    if reservation.line.arrived is None:
        messages.error(request, "Lot hali omborga yetib kelmagan")
        return form_reload(request, reverse("reservation_list"))
    price = reservation.price
    if price is None:
        raw_price = request.POST.get("price")
        try:
            price = Decimal(raw_price) if raw_price else None
        except (ValueError, ArithmeticError):
            price = None
    if price is None:
        messages.error(request, "Narx ko'rsatilishi kerak")
        return form_reload(request, reverse("reservation_list"))
    # Defense-in-depth: reserved kg was validated at reservation time and convert is
    # net-neutral, but refuse if the lot has since become over-committed (e.g. its kg
    # was edited down) so a convert can never oversell.
    if reservation.line.available_kg < 0:
        messages.error(request, "Lot kg yetarli emas")
        return form_reload(request, reverse("reservation_list"))
    sale = Sale.objects.create(
        customer=reservation.customer, line=reservation.line, kg=reservation.kg,
        price=price, cost_price=reservation.line.landed_cost_per_kg,
        date=timezone.localdate(), reservation=reservation, created_by=request.user,
    )
    reservation.status = Reservation.Status.CONVERTED
    reservation.save(update_fields=["status"])
    AuditLog.record(
        request.user, AuditLog.Action.CREATE, "Bron", reservation.pk,
        f"Bron sotuvga aylandi: {reservation.kg} kg · {reservation.customer.name}",
    )
    apply_customer_advance(sale)
    messages.success(request, "Bron sotuvga aylantirildi")
    return form_reload(request, reverse("reservation_list"))


@role_required(User.Role.ADMIN)
def return_create(request):
    sale = get_object_or_404(Sale, pk=request.GET.get("sale") or request.POST.get("sale"))
    form = ReturnForm(request.POST or None, sale=sale)
    if request.method == "POST":
        if form.is_valid():
            ret = form.save(commit=False)
            ret.created_by = request.user
            ret.save()
            # The return shrank the sale's net_total; trim any now-excess allocation
            # so the freed money becomes a reachable advance again.
            trim_sale_allocations(sale)
            AuditLog.record(
                request.user, AuditLog.Action.RETURN, "Qaytarish", ret.pk,
                f"Qaytarish: {ret.kg} kg · sotuv #{sale.pk} · {sale.customer.name}",
            )
            messages.success(request, "Qaytarish qo'shildi")
            return form_success(request, reverse("sale_detail", args=[sale.pk]))
        return form_response(request, form, "Qaytarish", invalid=True)
    return form_response(request, form, "Qaytarish")


@role_required(User.Role.ADMIN)
def return_delete(request, pk):
    ret = get_object_or_404(Return, pk=pk)
    sale = ret.sale
    if request.method == "POST":
        label = f"{ret.kg} kg · sotuv #{sale.pk}"
        ret.delete()
        # net_total rose again; soak any freed advance back onto the restored debt.
        apply_customer_advance(sale)
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Qaytarish", pk,
                        f"Qaytarish o'chirildi: {label}")
        messages.success(request, "Qaytarish o'chirildi")
        return form_reload(request, reverse("sale_detail", args=[sale.pk]))
    return render_confirm(
        request,
        "Qaytarishni o'chirish",
        f"“{ret.kg} kg” qaytarish o'chiriladi. Bu amalni qaytarib bo'lmaydi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="sale_list",
    )


@role_required(User.Role.ADMIN)
def debt_list(request):
    debtors = [c for c in Customer.objects.all() if c.balance > 0]
    rows = []
    for c in debtors:
        overdue_count = sum(1 for s in c.sales.all() if s.is_overdue)
        rows.append({"customer": c, "overdue_count": overdue_count})
    rows.sort(key=lambda r: r["customer"].balance, reverse=True)
    return render(request, "crm/debt_list.html", {"rows": rows})


@role_required(User.Role.ADMIN)
def debt_customer(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    sales = [s for s in customer.sales.select_related("line__contract_line").all() if s.remaining > 0]
    return render(request, "crm/debt_customer.html", {"customer": customer, "sales": sales})


@role_required(User.Role.ADMIN)
def kassa(request):
    """The till, client-crm style: a current-state hero (Kassadagi pul + what we
    owe hamkorlar), per-method USD balances for the selected period, and two
    Excel-like ledgers side by side — Kirim (customer payments) and Chiqim
    (supplier payments + shipment expenses). Purely derived; ?from&to narrows
    the period section, the hero is all-time."""
    date_from = (request.GET.get("from") or "").strip()
    date_to = (request.GET.get("to") or "").strip()

    def _range(qs):
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        return qs

    def _sum(qs):
        return qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    # Joriy holat (all-time, filter-independent): money physically in the till.
    cash_total = (_sum(CustomerPayment.objects.all())
                  - _sum(SupplierPayment.objects.all())
                  - commission_total(SupplierPayment.objects.all())
                  - _sum(ShipmentExpense.objects.all()))

    cust_pays = _range(CustomerPayment.objects.select_related("customer"))
    sup_pays = _range(SupplierPayment.objects.select_related("contract__partner"))
    expenses = _range(ShipmentExpense.objects.select_related("shipment__contract"))

    balances = {}
    net_in = net_out = Decimal("0")
    for value, label in PayMethod.choices:
        m_in = _sum(cust_pays.filter(method=value))
        m_out = (_sum(sup_pays.filter(method=value))
                 + commission_total(sup_pays.filter(method=value))
                 + _sum(expenses.filter(method=value)))
        balances[value] = {"label": label, "in": m_in, "out": m_out, "balance": m_in - m_out}
        net_in += m_in
        net_out += m_out

    # Kirim ledger: payments received from customers, newest first.
    income_rows = sorted(cust_pays, key=lambda p: (p.date, p.pk), reverse=True)

    # Chiqim ledger: money out — supplier payments and per-load expenses.
    outflow_rows = []
    for p in sup_pays:
        outflow_rows.append({
            "kind": "supplier", "pk": p.pk, "date": p.date, "obj": p,
            # The hamkor is already inside the code, so the brand is the useful half here
            "title": f"Kelishuv {p.contract.code} · {p.contract.brand_summary}",
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_original": p.amount_original, "amount": p.amount,
        })
    for p in sup_pays:
        if not p.commission_amount:
            continue
        outflow_rows.append({
            "kind": "commission", "pk": p.pk, "date": p.date, "obj": p,
            "title": (f"Vositachi ({p.commission_percent}%) · "
                      f"kelishuv {p.contract.code}"),
            "method_code": p.method, "method": p.get_method_display(),
            "currency": Currency.USD, "exchange_rate": Decimal("0"),
            "amount_original": p.commission_amount, "amount": p.commission_amount,
        })
    for e in expenses:
        outflow_rows.append({
            "kind": "expense", "pk": e.pk, "date": e.date, "obj": e,
            "title": f"{e.get_category_display()} · yuk #{e.shipment_id}",
            "method_code": e.method, "method": e.get_method_display(),
            "currency": e.currency, "exchange_rate": e.exchange_rate,
            "amount_original": e.amount_original, "amount": e.amount,
        })
    outflow_rows.sort(key=lambda r: (r["date"], r["pk"]), reverse=True)

    # What we owe hamkorlar RIGHT NOW (not date-filtered — a current-state figure):
    # per contract the debt accrues per shipped truck (shipped value − paid).
    payables = {}
    for c in Contract.objects.select_related("partner").prefetch_related("shipments"):
        d = c.debt
        if d > 0:
            payables[c.partner] = payables.get(c.partner, Decimal("0")) + d
    partner_debts = sorted(payables.items(), key=lambda kv: kv[1], reverse=True)
    payable_total = sum((d for _, d in partner_debts), Decimal("0"))

    # Quick period presets for the filter bar.
    today = timezone.localdate()
    presets = [
        ("Bugun", today.isoformat(), today.isoformat()),
        ("7 kun", (today - timedelta(days=6)).isoformat(), today.isoformat()),
        ("30 kun", (today - timedelta(days=29)).isoformat(), today.isoformat()),
        ("Hammasi", "", ""),
    ]

    return render(request, "crm/kassa.html", {
        "cash_total": cash_total,
        "balances": balances, "net_in": net_in, "net_out": net_out,
        "net_total": net_in - net_out,
        "income_rows": income_rows, "outflow_rows": outflow_rows,
        "partner_debts": partner_debts, "payable_total": payable_total,
        "date_from": date_from, "date_to": date_to, "presets": presets,
    })


def _report_filters(request):
    """Parse the shared reports/exports querystring filters (?from&to&partner&brand&status)."""
    return {
        "date_from": (request.GET.get("from") or "").strip(),
        "date_to": (request.GET.get("to") or "").strip(),
        "partner_id": (request.GET.get("partner") or "").strip(),
        "brand": (request.GET.get("brand") or "").strip(),
        "status_id": (request.GET.get("status") or "").strip(),
    }


def _report_querysets(request):
    """Build the filtered contracts/shipments/supplier-payments/sales/customer-payments
    querysets shared by the reports dashboard and the xlsx exports."""
    f = _report_filters(request)
    date_from, date_to = f["date_from"], f["date_to"]
    partner_id, brand, status_id = f["partner_id"], f["brand"], f["status_id"]

    contracts = Contract.objects.select_related("partner")
    if partner_id:
        contracts = contracts.filter(partner_id=partner_id)
    if brand:
        contracts = contracts.filter(lines__brand=brand).distinct()
    if date_from:
        contracts = contracts.filter(created__gte=date_from)
    if date_to:
        contracts = contracts.filter(created__lte=date_to)

    shipments = Shipment.objects.select_related("contract__partner", "status").filter(
        contract__in=contracts
    )
    if status_id:
        shipments = shipments.filter(status_id=status_id)
    if date_from:
        shipments = shipments.filter(eta__gte=date_from)
    if date_to:
        shipments = shipments.filter(eta__lte=date_to)

    sup_pays = SupplierPayment.objects.select_related("contract__partner").filter(contract__in=contracts)
    if date_from:
        sup_pays = sup_pays.filter(date__gte=date_from)
    if date_to:
        sup_pays = sup_pays.filter(date__lte=date_to)

    sales = Sale.objects.select_related("customer", "line__contract_line", "line__shipment__contract__partner")
    if date_from:
        sales = sales.filter(date__gte=date_from)
    if date_to:
        sales = sales.filter(date__lte=date_to)
    if partner_id:
        sales = sales.filter(line__shipment__contract__partner_id=partner_id)
    if brand:
        sales = sales.filter(line__contract_line__brand=brand)

    cust_pays = CustomerPayment.objects.select_related("customer")
    if date_from:
        cust_pays = cust_pays.filter(date__gte=date_from)
    if date_to:
        cust_pays = cust_pays.filter(date__lte=date_to)

    return {
        "filters": f, "contracts": contracts, "shipments": shipments,
        "sup_pays": sup_pays, "sales": sales, "cust_pays": cust_pays,
    }


@role_required(User.Role.ADMIN)
def reports(request):
    """Hisobotlar: whole-business KPI + table dashboard. Filters (?from&to&partner&
    brand&status) narrow contracts/shipments (partner/brand/status/date-created-or-eta)
    and sales/payments (date). Everything below is derived — no new model."""
    q = _report_querysets(request)
    date_from, date_to = q["filters"]["date_from"], q["filters"]["date_to"]
    partner_id, brand, status_id = q["filters"]["partner_id"], q["filters"]["brand"], q["filters"]["status_id"]
    contracts, shipments = q["contracts"], q["shipments"]
    sup_pays, sales, cust_pays = q["sup_pays"], q["sales"], q["cust_pays"]

    def _sum(qs, field="amount"):
        return qs.aggregate(s=Sum(field))["s"] or Decimal("0")

    # KPIs
    kelishilgan_kg = _sum(ContractLine.objects.filter(contract__in=contracts), "kg")
    yuborilgan_kg = _sum(ShipmentLine.objects.filter(shipment__in=shipments), "kg")
    omborga_kelgan_kg = _sum(ShipmentLine.objects.filter(
        shipment__in=shipments.filter(arrived__isnull=False)), "kg")
    kontrakt_summasi = sum((c.total_value for c in contracts), Decimal("0"))
    hamkorga_tolangan = _sum(sup_pays)
    hamkor_qarzi = sum((c.debt for c in contracts), Decimal("0"))
    mijoz_qarzi = sum((c.balance for c in Customer.objects.all() if c.balance > 0), Decimal("0"))
    profit_total = sum((s.profit for s in sales), Decimal("0"))
    late_shipments = [s for s in shipments.filter(arrived__isnull=True, eta__isnull=False) if s.is_overdue]
    kechikkan_soni = len(late_shipments)

    # Per-partner table
    partner_rows = []
    for partner in Partner.objects.filter(contracts__in=contracts).distinct():
        p_contracts = contracts.filter(partner=partner)
        partner_rows.append({
            "partner": partner,
            "contracts_count": p_contracts.count(),
            "kg": _sum(ContractLine.objects.filter(contract__in=p_contracts), "kg"),
            "kontrakt_summasi": sum((c.total_value for c in p_contracts), Decimal("0")),
            "tolangan": _sum(sup_pays.filter(contract__partner=partner)),
            "qarz": sum((c.debt for c in p_contracts), Decimal("0")),
        })
    partner_rows.sort(key=lambda r: r["qarz"], reverse=True)

    # Per-customer table
    customer_rows = []
    customer_ids = sales.values_list("customer_id", flat=True).distinct()
    for customer in Customer.objects.filter(pk__in=customer_ids):
        c_sales = sales.filter(customer=customer)
        # net (post-returns) so the row reconciles with the net-based qarz column
        sotildi = sum((s.net_total for s in c_sales), Decimal("0"))
        tolandi = _sum(cust_pays.filter(customer=customer))
        qarz = customer.balance if customer.balance > 0 else Decimal("0")
        customer_rows.append({
            "customer": customer, "sotildi": sotildi, "tolandi": tolandi, "qarz": qarz,
        })
    customer_rows.sort(key=lambda r: r["qarz"], reverse=True)

    return render(request, "crm/reports.html", {
        "kelishilgan_kg": kelishilgan_kg, "yuborilgan_kg": yuborilgan_kg,
        "omborga_kelgan_kg": omborga_kelgan_kg, "kontrakt_summasi": kontrakt_summasi,
        "hamkorga_tolangan": hamkorga_tolangan, "hamkor_qarzi": hamkor_qarzi,
        "mijoz_qarzi": mijoz_qarzi, "profit_total": profit_total,
        "kechikkan_soni": kechikkan_soni, "late_shipments": late_shipments,
        "partner_rows": partner_rows, "customer_rows": customer_rows,
        "partners": Partner.objects.all(), "brands": ContractLine.objects.values_list(
            "brand", flat=True).distinct().order_by("brand"),
        "statuses": ShipmentStatus.objects.all(),
        "date_from": date_from, "date_to": date_to,
        "partner_id": partner_id, "brand": brand, "status_id": status_id,
    })


@role_required(User.Role.ADMIN)
def export_contracts(request):
    contracts = _report_querysets(request)["contracts"]
    headers = ["Kelishuv", "Sana", "Hamkor", "Marka", "Kg", "Narx", "Jami", "Yuborilgan kg",
               "To'langan", "Qarz"]
    # One row per product, so a multi-product kelishuv is readable in Excel. The
    # money columns are per kelishuv, so they repeat down its rows.
    rows = (
        [c.code, c.created, c.partner.name, ln.brand, ln.kg, ln.price, ln.total_value,
         ln.shipped_kg, c.paid_total, c.debt]
        for c in contracts.prefetch_related("lines__shipment_lines", "supplier_payments")
        for ln in c.lines.all()
    )
    return xlsx_response("kelishuvlar.xlsx", headers, rows)


@role_required(User.Role.ADMIN)
def export_supplier_payments(request):
    sup_pays = _report_querysets(request)["sup_pays"]
    headers = ["Sana", "Kelishuv", "Hamkor", "Hamkorga", "Vositachi %", "Vositachi",
               "Kassadan", "Usul"]
    rows = (
        [p.date, p.contract.code, p.contract.partner.name, p.amount, p.commission_percent,
         p.commission_amount, p.total_out, p.get_method_display()]
        for p in sup_pays
    )
    return xlsx_response("hamkor-tolovlari.xlsx", headers, rows)


@role_required(User.Role.ADMIN)
def export_shipments(request):
    shipments = _report_querysets(request)["shipments"]
    headers = [
        "Yuk ID", "Kelishuv", "Hamkor", "Marka", "Kg", "Holat", "Jo'natilgan", "Reja kelish",
        "Yetib kelgan", "Transport", "Konteyner",
    ]
    rows = (
        [s.pk, s.contract.code, s.contract.partner.name, ln.brand, ln.kg, s.status.name,
         s.sent, s.eta, s.arrived, s.transport, s.container]
        for s in shipments.prefetch_related("lines__contract_line")
        for ln in s.lines.all()
    )
    return xlsx_response("yuklar.xlsx", headers, rows)


@role_required(User.Role.ADMIN)
def export_sales(request):
    sales = _report_querysets(request)["sales"]
    headers = ["Sana", "Mijoz", "Lot ID", "Marka", "Kg", "Tan narx", "Sotuv narx", "Jami", "Foyda", "Qoldiq"]
    rows = (
        [s.date, s.customer.name, s.line_id, s.line.brand, s.kg, s.cost_price,
         s.price, s.total, s.profit, s.remaining]
        for s in sales
    )
    return xlsx_response("sotuvlar.xlsx", headers, rows)


@role_required(User.Role.ADMIN)
def export_debts(request):
    headers = ["Mijoz", "Telefon", "Jami savdo", "To'langan", "Qarz"]
    rows = (
        [c.name, c.phone, c.sales_total, c.paid_total, c.balance]
        for c in Customer.objects.all() if c.balance > 0
    )
    return xlsx_response("qarzdorlar.xlsx", headers, rows)
