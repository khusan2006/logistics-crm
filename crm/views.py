from datetime import date as _date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, ProtectedError, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User

from .exports import xlsx_response
from .templatetags.crm_extras import som, usd
from .forms import (
    ContractForm, ContractLineFormSet, CustomerForm, TruckPlanForm, CustomerPaymentForm,
    contract_currency,
    CustomerPaymentFormSet, CustomerPaymentTargetForm, PartnerForm, ReservationForm, ReturnForm,
    ExpenseGridForm, LogistForm, LogistPaymentForm,
    SaleCreateForm, SaleForm, SaleLotForm, ShipmentExpenseForm,
    ShipmentExtendForm, ShipmentForm, ShipmentLineFormSet,
    ShipmentLegForm, ShipmentStatusForm, SupplierPaymentForm,
)
from .models import (
    AuditLog, Logist, LogistPayment, Contract, ContractLine, Currency, Customer, CustomerPayment, Partner,
    PaymentAllocation,
    PayMethod, Reservation, Return, Sale, Shipment, ShipmentDelay, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment, allocate_customer_payment,
    apply_customer_advance, arrived_lots, brand_on_hand_kg, brand_reserved_kg,
    _by_currency, bron_queue, commission_total, contract_value_by_currency,
    convert_pair,
    customer_paid_by_currency, customer_sales_by_currency,
    draw_down_bron, release_bron,
    logist_positions, payable_by_currency, supplier_paid_by_currency,
    customer_advance_by_currency, customer_advance_total, customer_balance_by_currency,
    customer_receivable_by_currency, customer_receivable_total, fifo_lots,
    kassa_cash_by_currency, partner_positions, partner_positions_by_currency,
    reconcile_customer_allocations, stock_value, transit_value,
    transit_value_by_currency, trim_sale_allocations,
    unspent_payment_amount, uzs_slice,
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
    # Per currency, like the kassa board: summing both stored columns counts every
    # dollar row a second time in so'm clothing, which is what put 3 758 mln so'm of
    # mijoz qarzi on a book that holds 1 128 mln.
    paid_split = supplier_paid_by_currency(SupplierPayment.objects.all())
    # Across every kelishuv, not only the goods already sent: this is the whole
    # remaining obligation to hamkorlar. Kassa keeps its own narrower figure for
    # what is due right now — that one is captioned as such.
    debt_split = payable_by_currency(
        contracts.prefetch_related("lines__shipment_lines", "supplier_payments"))
    overdue = [s for s in shipments.filter(arrived__isnull=True, eta__isnull=False)
               if s.is_overdue]
    # Each holat with how many trucks each hamkor has sitting in it. Listing the
    # loads themselves repeated the same kelishuv kod once per truck; the question
    # being asked is "whose trucks are on the road", which is a count per hamkor.
    by_status = {}
    for shipment in shipments:
        row = by_status.setdefault(shipment.status_id, {"total": 0, "partners": {}})
        row["total"] += 1
        name = shipment.contract.partner.name
        row["partners"][name] = row["partners"].get(name, 0) + 1
    status_rows = [
        {"status": st, "total": by_status[st.pk]["total"],
         # busiest hamkor first, ties by name
         "partners": sorted(by_status[st.pk]["partners"].items(), key=lambda kv: (-kv[1], kv[0]))}
        for st in ShipmentStatus.objects.all() if st.pk in by_status
    ]

    # What each hamkor still owes in trucks, summed across their kelishuvlar —
    # read the same way as Yuk holatlari: a hamkor and a count. Only kelishuvlar
    # that set a plan and have not met it yet count toward it.
    owed = {}
    for contract in contracts:
        sent, planned = contract.truck_progress
        if planned and planned > sent:
            name = contract.partner.name
            owed[name] = owed.get(name, 0) + (planned - sent)
    truck_plan_rows = sorted(owed.items(), key=lambda kv: (-kv[1], kv[0]))

    # The progress chart used to take the 8 newest kelishuvlar, so a run of fresh
    # agreements filled it with empty bars while the ones actually shipping sat
    # just below the cut — the dashboard read as "nothing is moving" next to a Yuk
    # holatlari card listing nine loads. Show what has moved, most shipped first.
    CHART_LIMIT = 8
    chart_contracts = sorted(
        contracts, key=lambda c: (c.shipped_kg > 0, c.shipped_kg), reverse=True)
    contracts_total = len(chart_contracts)
    chart_contracts = chart_contracts[:CHART_LIMIT]

    arrived_lots = shipments.filter(arrived__isnull=False)
    stock_kg = sum((s.available_kg for s in arrived_lots), Decimal("0"))
    customer_debt_split, _debtors = customer_receivable_by_currency()
    # Foyda stays a converted pair on purpose: it is measured against the landed
    # cost, and a tannarx blends a dollar mol with a so'm transport bill by design.
    sales_profit_total = sum((s.profit for s in Sale.objects.all()), Decimal("0"))
    sales_profit_total_uzs = sum((s.profit_uzs for s in Sale.objects.all()), Decimal("0"))

    return render(request, "crm/dashboard.html", {
        "total_kg": total_kg, "shipped_kg": shipped_kg, "arrived_kg": arrived_kg,
        "paid_split": paid_split, "debt_split": debt_split, "overdue": overdue,
        "customer_debt_split": customer_debt_split,
        "sales_profit_total_uzs": sales_profit_total_uzs,
        "contracts": chart_contracts, "contracts_shown": len(chart_contracts),
        "contracts_total": contracts_total, "status_rows": status_rows,
        "truck_plan_rows": truck_plan_rows,
        "stock_kg": stock_kg,
        "sales_profit_total": sales_profit_total,
        "monthly": _monthly_rows(),
    })


def _monthly_rows(limit=12):
    """Per-month activity, newest first, skipping months where nothing happened.

    A truck counts under the month it LEFT for `sent` and the month it ARRIVED for
    everything else, so one load can appear in two rows — that is the point: it
    answers "how many arrived in July", not "how many that left in July arrived".

    Summed in Python over prefetched rows rather than in SQL: goods_value and a
    sale's profit both fall back to related rows (the kelishuv price, restocked
    returns), which the model properties already get right."""
    months = {}

    def bucket(day):
        key = day.replace(day=1)
        return months.setdefault(key, {
            "month": key, "sent": 0, "arrived": 0, "kg": Decimal("0"),
            "value": Decimal("0"), "sales": Decimal("0"), "profit": Decimal("0"),
            "value_uzs": Decimal("0"), "sales_uzs": Decimal("0"),
            "profit_uzs": Decimal("0"),
        })

    for shipment in Shipment.objects.prefetch_related("lines__contract_line"):
        if shipment.sent:
            bucket(shipment.sent)["sent"] += 1
        if shipment.arrived:
            row = bucket(shipment.arrived)
            row["arrived"] += 1
            row["kg"] += shipment.kg
            row["value"] += shipment.goods_value
            row["value_uzs"] += shipment.goods_value_uzs

    for sale in Sale.objects.prefetch_related("returns"):
        row = bucket(sale.date)
        row["sales"] += sale.net_total
        row["profit"] += sale.profit
        row["sales_uzs"] += sale.net_total_uzs
        row["profit_uzs"] += sale.profit_uzs

    return sorted(months.values(), key=lambda r: r["month"], reverse=True)[:limit]


@role_required(User.Role.ADMIN)
def audit_list(request):
    page = Paginator(AuditLog.objects.select_related("user"), 20).get_page(request.GET.get("page"))
    return render(request, "crm/audit_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def partner_list(request):
    q = request.GET.get("q", "").strip()
    # The kelishuvlar come along so each hamkor's qolgan to'lov can be totalled per
    # currency off the prefetch, instead of two queries per row as the page walks it.
    partners = Partner.objects.prefetch_related(
        "contracts__lines__shipment_lines", "contracts__supplier_payments")
    if q:
        partners = partners.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(city__icontains=q))
    page = Paginator(partners, 20).get_page(request.GET.get("page"))
    for partner in page.object_list:
        partner.payable = payable_by_currency(partner.contracts.all())
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
    # The sotuvlar and to'lovlar come along so each mijoz's ostatka can be totalled
    # per currency off the prefetch instead of two queries per row.
    customers = Customer.objects.prefetch_related(
        "sales__allocations", "customer_payments__allocations")
    if q:
        customers = customers.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(address__icontains=q)
        )
    page = Paginator(customers, 20).get_page(request.GET.get("page"))
    for customer in page.object_list:
        customer.positions = customer_balance_by_currency(customer)
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
# All three read the SAME figure, in the kelishuv's own currency, so they partition
# the list instead of overlapping it. Mixing measures — `paid` off payable_left while
# `partial`/`unpaid` went off total_value — put a kelishuv whose truck went cheaper
# than agreed into both To'langan and Qisman, and one whose truck went dearer into
# neither, so the chip counts disagreed with the rows behind them.
CONTRACT_PAY_FILTERS = {
    "paid": lambda c: c.payable_left_own <= 0,
    "partial": lambda c: c.payable_left_own > 0 and c.paid_total_own > 0,
    "unpaid": lambda c: c.payable_left_own > 0 and c.paid_total_own == 0,
}
CONTRACT_PAY_LABELS = [("", "Hammasi"), ("paid", "To'langan"),
                       ("partial", "Qisman to'langan"), ("unpaid", "To'lanmagan")]

# Sorted in Python, not SQL: jami and qolgan to'lov are computed properties, and
# the rows are already a list by this point because the holat/to'lov filters read
# the same properties. Each entry is (key, label, sort key, reverse).
CONTRACT_SORTS = [
    ("-created", "Sana — yangi avval", lambda c: (c.created, c.pk), True),
    ("created", "Sana — eski avval", lambda c: (c.created, c.pk), False),
    ("code", "Kod — A-Z", lambda c: (c.code_slug, c.code_number), False),
    ("-code", "Kod — Z-A", lambda c: (c.code_slug, c.code_number), True),
    ("partner", "Hamkor — A-Z",
     lambda c: (c.partner.name.casefold(), c.code_slug, c.code_number), False),
    ("-total", "Jami — kattadan", lambda c: (c.total_value, c.pk), True),
    ("total", "Jami — kichikdan", lambda c: (c.total_value, c.pk), False),
    ("-left", "Qolgan to'lov — kattadan", lambda c: (c.payable_left, c.pk), True),
    ("-kg", "Qolgan kg — kattadan", lambda c: (c.remaining_kg, c.pk), True),
]
CONTRACT_SORT_DEFAULT = "-created"


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
    # Unfinished business is the working view, so it is what you land on; Hammasi
    # is the deliberate step out of it, not the default.
    state = request.GET.get("state", "open").strip()
    sort = request.GET.get("sort", "").strip()
    if sort not in {key for key, *_ in CONTRACT_SORTS}:
        sort = CONTRACT_SORT_DEFAULT

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
    # Tugallanmagan = still owed goods OR still owed money; a kelishuv shipped in
    # full but not paid off is unfinished business too.
    if state == "done":
        rows = [c for c in rows if c.is_settled]
    elif state == "open":
        rows = [c for c in rows if not c.is_settled]

    # A Tugallangan kelishuv is fully paid by definition, so the to'lov axis has
    # only one non-empty bucket there — the filter is hidden and ignored.
    pay_applies = state != "done"
    # Counts are faceted: computed before the payment filter narrows the rows, so
    # each option shows what picking it would yield.
    pay_tabs = [{"key": key, "label": label,
                 "count": (len(rows) if not key
                           else sum(1 for c in rows if CONTRACT_PAY_FILTERS[key](c)))}
                for key, label in CONTRACT_PAY_LABELS] if pay_applies else []
    if pay_applies and pay in CONTRACT_PAY_FILTERS:
        rows = [c for c in rows if CONTRACT_PAY_FILTERS[pay](c)]

    _, _, sort_key, sort_reverse = next(e for e in CONTRACT_SORTS if e[0] == sort)
    rows.sort(key=sort_key, reverse=sort_reverse)

    # One row per kelishuv, in the order the sort left them. Grouping them under a
    # hamkor heading put the hamkor's name on the screen twice — the kod already
    # opens with it — and reordered the page behind the reader's back, since a
    # hamkor's older kelishuv was pulled up beside their newest.
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    return render(request, "crm/contract_list.html", {
        "page": page, "rows": rows,
        "q": q, "pay": pay, "partner_id": partner_id,
        "state": state, "pay_tabs": pay_tabs, "pay_applies": pay_applies,
        "sort": sort, "sort_options": [(key, label) for key, label, *_ in CONTRACT_SORTS],
        "partners": Partner.objects.all(),
        "has_filters": bool((pay and pay_applies) or partner_id or state != "open"),
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
    # The rows are priced in the header's currency, and they are built before the
    # header has been validated — so it is read off the raw POST (contract_currency).
    lines = ContractLineFormSet(
        request.POST or None,
        form_kwargs={"currency": contract_currency(request.POST or None)})
    plan = TruckPlanForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid() and lines.is_valid() and plan.is_valid():
            with transaction.atomic():
                contract = form.save(commit=False)
                contract.created_by = request.user
                contract.planned_trucks = plan.cleaned_data["planned_trucks"]
                contract.save()
                _save_lines(lines, contract)
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Kelishuv", contract.pk,
                f"Yangi kelishuv: {contract.code} · {contract.brand_summary}",
            )
            messages.success(request, "Kelishuv qo'shildi")
            return form_success(request, reverse("contract_list"))
        return _contract_form_response(request, form, lines, plan, "Yangi kelishuv",
                                       invalid=True)
    return _contract_form_response(request, form, lines, plan, "Yangi kelishuv")


def _contract_form_response(request, form, lines, plan, title, invalid=False):
    return form_response(request, form, title, invalid=invalid,
                         extra_context={"lines": lines, "lines_legend": "Mahsulotlar",
                                        "lines_after": plan})


@role_required(User.Role.ADMIN)
def contract_edit(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    form = ContractForm(request.POST or None, instance=contract)
    lines = ContractLineFormSet(
        request.POST or None, instance=contract,
        form_kwargs={"currency": contract_currency(request.POST or None, contract)})
    plan = TruckPlanForm(request.POST or None, instance=contract)
    title = "Kelishuvni tahrirlash"
    if request.method == "POST":
        if form.is_valid() and lines.is_valid() and plan.is_valid():
            with transaction.atomic():
                form.save()
                plan.save()
                _save_lines(lines, contract)
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Kelishuv", contract.pk,
                f"Kelishuv tahrirlandi: {contract.code} · {contract.brand_summary}",
            )
            messages.success(request, "Kelishuv yangilandi")
            return form_reload(request, reverse("contract_list"))
        return _contract_form_response(request, form, lines, plan, title, invalid=True)
    return _contract_form_response(request, form, lines, plan, title)


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


# Sorted in SQL — every key here is a real column, unlike the kelishuv list.
SUPPLIER_PAYMENT_SORTS = [
    ("-date", "Sana — yangi avval", ["-date", "-created_at"]),
    ("date", "Sana — eski avval", ["date", "created_at"]),
    ("-amount", "Summa — kattadan", ["-amount", "-date"]),
    ("amount", "Summa — kichikdan", ["amount", "-date"]),
    ("partner", "Hamkor — A-Z", ["contract__partner__name", "-date"]),
]
SUPPLIER_PAYMENT_SORT_DEFAULT = "-date"


@role_required(User.Role.ADMIN)
def supplier_payment_list(request):
    q = request.GET.get("q", "").strip()
    partner_id = request.GET.get("partner", "").strip()
    method = request.GET.get("method", "").strip()
    date_from = _date_param(request, "date_from")
    date_to = _date_param(request, "date_to")
    sort = request.GET.get("sort", "").strip()
    if sort not in {key for key, *_ in SUPPLIER_PAYMENT_SORTS}:
        sort = SUPPLIER_PAYMENT_SORT_DEFAULT

    payments = SupplierPayment.objects.select_related("contract__partner")
    contract_id = request.GET.get("contract")
    if contract_id and contract_id.isdigit():
        payments = payments.filter(contract_id=contract_id)
    if q:
        payments = payments.filter(
            Q(contract__code_slug__icontains=q) | Q(contract__partner__name__icontains=q)
            | Q(note__icontains=q) | Q(contract__lines__brand__icontains=q)
            | _payment_code_filter(q)).distinct()
    if partner_id.isdigit():
        payments = payments.filter(contract__partner_id=int(partner_id))
    if method in dict(PayMethod.choices):
        payments = payments.filter(method=method)
    if date_from:
        payments = payments.filter(date__gte=date_from)
    if date_to:
        payments = payments.filter(date__lte=date_to)

    ordering = next(e[2] for e in SUPPLIER_PAYMENT_SORTS if e[0] == sort)
    payments = payments.order_by(*ordering)

    # Totals for what the filters left, not just this page — the reason to filter
    # is usually to add something up.
    #
    # Bucketed by the currency each to'lov was actually made in, one line per bucket.
    # The two are not added: a so'm to'lov's dollar twin is derived at that day's
    # kurs, so a single figure would be part real and part conversion, and would drift
    # against the rows it claims to total.
    rows = list(payments)
    totals = []
    for currency, _label in Currency.choices:
        made = [p for p in rows if p.currency == currency]
        if made:
            totals.append({
                "currency": currency,
                "paid": sum((_own_amount(p) for p in made), Decimal("0")),
                "out": sum((_own_total_out(p) for p in made), Decimal("0")),
                "count": len(made),
            })

    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    return render(request, "crm/supplier_payment_list.html", {
        "page": page, "q": q, "partner_id": partner_id, "method": method,
        "date_from": date_from, "date_to": date_to, "sort": sort,
        "sort_options": [(key, label) for key, label, *_ in SUPPLIER_PAYMENT_SORTS],
        "methods": PayMethod.choices, "partners": Partner.objects.all(),
        "totals": totals,
        "has_filters": bool(q or partner_id or method or date_from or date_to),
    })


def _own_amount(payment):
    """What the hamkor received, in the currency the to'lov was actually made in."""
    return payment.amount_uzs if payment.is_som else payment.amount


def _own_total_out(payment):
    """The same for what left the kassa — the summa plus the cuts riding on top."""
    return payment.total_out_uzs if payment.is_som else payment.total_out


def _payment_code_filter(q):
    """`sobir-3` in the search box means that kelishuv."""
    slug, _, number = q.rpartition("-")
    if slug and number.isdigit():
        return Q(contract__code_slug=slug, contract__code_number=int(number))
    return Q()


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
    ignoring blanks and zeros. Returns (sale_id, USD amount) pairs.

    The operator types each figure in the SOTUV's own currency — the Taqsimlash
    table prints a so'm sotuv's qoldiq in so'm, so the box beside it takes so'm —
    while PaymentAllocation.amount is the dollar column. The conversion uses the
    SOTUV's kurs, not the to'lov's: the qoldiq on screen was rated at the sotuv's
    kurs, so anything else would leave a residue on a debt the operator just
    cleared to the tiyin.

    The currency is read from the sotuv rather than from a posted field, so a
    tampered form cannot make a so'm figure be taken as dollars."""
    typed = {}
    for key, value in post.items():
        if not key.startswith("alloc_"):
            continue
        value = (value or "").strip()
        if not value:
            continue
        try:
            typed[int(key[len("alloc_"):])] = Decimal(value)
        except (ValueError, ArithmeticError):
            continue
    if not typed:
        return []

    picks = []
    sales = Sale.objects.in_bulk(typed)
    for sale_id, amount in typed.items():
        if amount <= 0:
            continue
        sale = sales.get(sale_id)
        # A stale or tampered id is kept as-is and skipped downstream by
        # allocate_customer_payment, which is the one place that checks ownership.
        if sale is not None and sale.is_som:
            try:
                amount, _ = convert_pair(amount, Currency.UZS, sale.exchange_rate)
            except ValueError:
                # No usable kurs, so this so'm figure cannot be turned into the
                # dollar column. Dropping the pick lets FIFO place the money
                # instead; passing it through would book so'm as dollars.
                continue
        picks.append((sale_id, amount))
    return picks


@role_required(User.Role.ADMIN)
def customer_payment_list(request):
    payments = CustomerPayment.objects.select_related("customer")
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        payments = payments.filter(customer_id=customer_id)
    page = Paginator(payments, 20).get_page(request.GET.get("page"))
    return render(request, "crm/customer_payment_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def customer_payment_create(request):
    """Several to'lovlar at once: a mijoz settling 10 000$ commonly hands over part
    in dollars and the rest in so'm, sometimes naqd against perechisleniya. Each
    arrival is its own row — its own valyuta, kurs, usul and foiz — because they
    convert and charge differently; the mijoz and the sana are shared."""
    target = CustomerPaymentTargetForm(request.POST or None,
                                       initial={"customer": request.GET.get("customer")})
    rows = CustomerPaymentFormSet(request.POST or None,
                                  queryset=CustomerPayment.objects.none())

    def respond(invalid=False):
        # On a re-render the mijoz is whatever the header holds, not the query
        # string: the modal posts to a bare path, so ?customer= is gone by then.
        customer = _bound_customer(target, request)
        # Measured in each sotuv's own currency: a so'm sotuv settled in so'm is done,
        # whatever its dollar twin still reads.
        alloc_sales = [s for s in customer.sales.all()
                       if s.remaining_own > 0] if customer else None
        return form_response(request, target, "Yangi to'lov", invalid=invalid,
                             extra_context={"lines": rows, "lines_legend": "To'lovlar",
                                            "lines_class": "lineset--money lineset--payment",
                                            "lines_add_label": "+ To'lov qo'shish",
                                            "alloc_sales": alloc_sales})

    if request.method == "POST":
        if target.is_valid() and rows.is_valid():
            customer = target.cleaned_data["customer"]
            # One pick list for the whole settlement. Applying it to each row in turn
            # is what makes that correct: every allocation lowers the sotuv's qoldiq,
            # so the second row picks up the same pick where the first ran out.
            picks = _parse_alloc_picks(request.POST)
            saved = []
            with transaction.atomic():
                for form in rows.forms:
                    if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                        continue
                    payment = form.save(commit=False)
                    payment.customer = customer
                    payment.date = target.cleaned_data["date"]
                    payment.created_by = request.user
                    payment.save()
                    allocate_customer_payment(payment, picks)
                    saved.append(payment)
                # A pick can leave an earlier sotuv short while this settlement still
                # has money in hand — the sweep places whatever the rows did not.
                reconcile_customer_allocations(customer)
            total = sum((p.amount for p in saved), Decimal("0"))
            AuditLog.record(
                request.user, AuditLog.Action.PAYMENT, "Mijoz to'lovi",
                saved[0].pk if saved else None,
                f"To'lov: {len(saved)} ta · {total}$ · mijoz {customer.name}",
            )
            messages.success(
                request,
                f"{len(saved)} ta to'lov qo'shildi" if len(saved) > 1 else "To'lov qo'shildi")
            return form_success(request, reverse("customer_payment_list"))
        return respond(invalid=True)
    return respond()


def _bound_customer(target, request):
    """The mijoz the Taqsimlash table should list sotuvlar for — the posted one on a
    re-render, the ?customer= one when the modal first opens."""
    if target.is_bound:
        if target.is_valid():
            return target.cleaned_data["customer"]
        return None
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        return Customer.objects.filter(pk=customer_id).first()
    return None


@role_required(User.Role.ADMIN)
def customer_payment_edit(request, pk):
    payment = get_object_or_404(CustomerPayment, pk=pk)
    previous_customer_id = payment.customer_id
    form = CustomerPaymentForm(request.POST or None, instance=payment)
    title = "To'lovni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            payment = form.save()
            payment.allocations.all().delete()
            allocate_customer_payment(payment)
            # Re-spreading THIS to'lov can leave a sotuv it used to cover short while
            # another to'lov's avans sits unspent; the sweep pairs the two back up.
            # Run it for the previous mijoz too when the edit moved the to'lov away
            # from them — their sotuvlar just lost the money.
            reconcile_customer_allocations(payment.customer)
            if previous_customer_id != payment.customer_id:
                previous = Customer.objects.filter(pk=previous_customer_id).first()
                if previous:
                    reconcile_customer_allocations(previous)
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Mijoz to'lovi", payment.pk,
                f"To'lov tahrirlandi: {payment.amount}$ · mijoz {payment.customer.name}",
            )
            messages.success(request, "To'lov yangilandi")
            return form_reload(request, reverse("customer_payment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def customer_payment_detail(request, pk):
    """Where one to'lov actually went — the sotuvlar it paid down and what is left.

    The Kirim ledger's Qarzga ta'sir column says HOW MUCH of a to'lov came off a
    qarz; this says which qarz. It exists because that column raises the question
    the moment the two figures differ, and the answer used to be reachable only by
    opening the mijoz and reading their sotuvlar back against the to'lovlar.

    Read-only, and deliberately not a form: the allocation is derived (FIFO, or the
    picks made when the to'lov was entered), so the way to change it is to edit the
    to'lov and let it re-spread."""
    payment = get_object_or_404(
        CustomerPayment.objects.select_related("customer")
        .prefetch_related("allocations__sale__line__contract_line"), pk=pk)
    # Each slice read in the SOTUV's currency, not the to'lov's: the row is answering
    # what the qarz it cleared was measured in, and a so'm sotuv is owed in so'm
    # however the money that settled it arrived.
    rows = [{"sale": alloc.sale,
             "amount": alloc.in_currency(alloc.sale.currency),
             "currency": alloc.sale.currency}
            for alloc in payment.allocations.all()]
    rows.sort(key=lambda r: r["sale"].date)
    context = {"payment": payment, "rows": rows,
               "title": f"To'lov · {payment.customer.name}"}
    template = ("crm/_payment_detail_modal.html" if is_ajax(request)
                else "crm/payment_detail.html")
    return render(request, template, context)


@role_required(User.Role.ADMIN)
def customer_payment_delete(request, pk):
    payment = get_object_or_404(CustomerPayment, pk=pk)
    if request.method == "POST":
        amount, customer_name = payment.amount, payment.customer.name
        customer = payment.customer
        payment.delete()  # CASCADE clears its allocations
        # The sotuvlar it covered are open again — let the mijoz's other avans in.
        reconcile_customer_allocations(customer)
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
    """Loads grouped by kelishuv, with status tabs (in pipeline order) to switch
    the view. Tabs filter client-side; each row carries its status + overdue flag.

    Two modes: the default shows only loads still moving, while `?all=1` (Hammasi)
    adds the arrived ones and paginates, since that set only grows."""
    q = request.GET.get("q", "").strip()
    show_all = request.GET.get("all") == "1"
    shipments = (Shipment.objects
                 .select_related("contract__partner", "status")
                 .prefetch_related("delays", "legs", "expenses"))
    if not show_all:
        shipments = shipments.filter(arrived__isnull=True)
    if q:
        shipments = shipments.filter(
            Q(transport__icontains=q) | Q(container__icontains=q)
            | Q(contract__lines__brand__icontains=q) | Q(contract__partner__name__icontains=q)
            | Q(driver_name__icontains=q) | Q(responsible__icontains=q)).distinct()
    shipments = list(shipments)

    counts = {}
    overdue_count = 0
    for s in shipments:
        counts[s.status_id] = counts.get(s.status_id, 0) + 1
        if s.is_overdue:
            overdue_count += 1

    # Group under the kelishuv (newest contract first, newest load first inside —
    # same recency feel as the flat list had). Built from every row, before any
    # paging: a kelishuv is the unit this page is read in.
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

    # Hammasi can grow without bound, so page it — by KELISHUV, not by yuk. Paging
    # the flat list cut a kelishuv wherever its 20th load happened to fall, leaving
    # the rest of that kelishuv's yuklar under a second copy of the same header a
    # page later. And because the list runs newest-first, that cut landed almost
    # exactly along the status line: the moving loads on one page, the arrived ones
    # on the next, which is what made a kelishuv look split by holat.
    #
    # The active view stays whole, as the pipeline is meant to be scanned.
    page = Paginator(groups, 10).get_page(request.GET.get("page")) if show_all else None
    if page is not None:
        groups = list(page.object_list)
    rows = [s for g in groups for s in g["shipments"]]

    statuses = list(ShipmentStatus.objects.all())  # ordered by (order, id)
    # The arrival status only earns a tab in Hammasi — in the active view nothing
    # can be sitting in it.
    tabs = [{"status": st, "count": counts.get(st.pk, 0)}
            for st in statuses if show_all or not st.is_arrival]
    # The active view opens on Yo'lda — the loads actually moving are what the
    # logist watches. Resolved by name (statuses are editable) and simply absent
    # if renamed away. Hammasi opens unfiltered: it was asked for to show
    # everything, so preselecting a tab would defeat it.
    default_tab = None if show_all else next(
        (t["status"].pk for t in tabs if t["status"].name.casefold() == "yo'lda"), None)
    return render(request, "crm/shipment_list.html", {
        "shipments": rows, "groups": groups, "statuses": statuses, "tabs": tabs,
        "total": len(shipments), "overdue_count": overdue_count,
        "q": q, "default_tab": default_tab, "show_all": show_all, "page": page,
    })


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_done_list(request):
    """Kept so old links and bookmarks still land somewhere: Yakunlangan is now
    the Hammasi view, which lists arrived loads alongside the moving ones."""
    return redirect(f"{reverse('shipment_list')}?all=1")


@role_required(User.Role.ADMIN)
def ombor(request):
    """Ombor by MARKA, one row per granula. The same marka can arrive on several
    lots at different landed costs; showing those as separate rows made the stock
    look like different products, so they merge here and the lots live inside the
    row — each still sellable on its own (a lot's own tan narx follows the sale)."""
    q = request.GET.get("q", "").strip()
    # Oldest arrival first — the FIFO consumption order sales draw from.
    lots = (arrived_lots()
            .prefetch_related("shipment__expenses", "sales__returns")
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
                                   "brons": [],
                                   "kirim": Decimal("0"), "sold": Decimal("0"),
                                   "reserved": Decimal("0"), "available": Decimal("0")}
            groups.append(g)
        g["lots"].append(lot)

        g["kirim"] += lot.kg
        g["sold"] += lot.sold_kg
        g["on_hand"] = g.get("on_hand", Decimal("0")) + lot.available_kg
        partner = lot.shipment.contract.partner.name
        if partner not in g["partners"]:
            g["partners"].append(partner)
    for g in groups:
        # The so'm range is taken from the same lots rather than converting the
        # dollar range, so each end is stated at the kurs its own lot was booked at.
        costed = [(lot.landed_cost_per_kg, lot.landed_cost_per_kg_uzs) for lot in g["lots"]]
        g["cost_min"], g["cost_min_uzs"] = min(costed)
        g["cost_max"], g["cost_max_uzs"] = max(costed)
        g["arrived_last"] = max(lot.arrived for lot in g["lots"])
        # The bron queue for this marka, oldest first — and the arithmetic that says
        # where the shelf kg went, so nobody has to work out why Sotish mumkin is
        # smaller than Kirim.
        g["brons"] = bron_queue(g["brand"])
        g["reserved"] = sum((r.remaining_kg for r in g["brons"]), Decimal("0"))
        g["available"] = max(g["on_hand"] - g["reserved"], Decimal("0"))
        g["short"] = max(g["reserved"] - g["on_hand"], Decimal("0"))

    page = Paginator(groups, 20).get_page(request.GET.get("page"))
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
                # The advance goes out with the truck, so it is recorded by the
                # dispatch form rather than waiting for somebody to remember it as
                # an xarajat later.
                form.sync_driver_advance(shipment, request.user)
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
                form.sync_driver_advance(shipment, request.user)
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

    # Arrival is the moment a bron on this load becomes sellable, and whoever
    # marks the truck in is rarely the person who sells. So the count rides back
    # with the response rather than waiting to be stumbled on — counted across ALL
    # the yuk's product lines, since a bron hangs off a line, not the load.
    brons = 0
    if status.is_arrival:
        # A bron is a claim on a MARKA, so the loads that matter are the ones
        # carrying a marka somebody is waiting for — not brons "belonging" to this
        # truck, which no longer exist.
        markalar = {line.brand for line in shipment.lines.all()}
        brons = sum(1 for b in bron_queue() if b.brand in markalar)
    bron_url = f"{reverse('reservation_list')}?status=active&lot=ready"

    if is_ajax(request):
        # The list JS updates the row in place (or drops it, if the load just
        # arrived and moved to Yakunlangan) — no page reload, never stale. The
        # date cell is re-rendered server-side so the exact "Yetib keldi" date
        # (or, on move-back, the returned ETA) always matches the page load.
        return JsonResponse({
            "status_id": status.pk,
            "arrived": shipment.arrived is not None,
            "date_html": render_to_string("crm/_load_date_cell.html", {"s": shipment}),
            "bron_count": brons,
            "bron_url": bron_url,
            "shipment_id": shipment.pk,
        })
    messages.success(request, "Holat yangilandi")
    if brons:
        messages.info(request, f"Bu yukda {brons} ta faol bron bor — sotuvga aylantirish mumkin")
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
    """Every turkum as its own box, filled in one pass — a yuk collects bojxona,
    deklarant and a couple of others on the same day, and the operator knows them as
    a set rather than as rows to be added one at a time.

    Opened on a yuk that already has xarajatlar, the boxes come up filled with them,
    so the modal is that yuk's xarajatlar rather than a queue of new ones — hence
    Saqlash may add, rewrite AND remove rows in one go."""
    # The yuk drives the boxes' contents, so it has to be known before the form is
    # built — on a failed submit too, or the modal would come back stripped of the
    # notes saying which figures are already in the books.
    asked = (request.POST.get("shipment") if request.method == "POST"
             else request.GET.get("shipment")) or ""
    shipment = Shipment.objects.filter(pk=asked).first() if asked.isdigit() else None
    form = ExpenseGridForm(request.POST or None, shipment=shipment,
                           initial={"shipment": request.GET.get("shipment")})
    title = "Yuk xarajatlari" if form.recorded else "Yangi xarajat"

    def respond(invalid=False):
        return form_response(request, form, title, invalid=invalid,
                             modal_template="crm/_expense_grid_modal.html")

    if request.method == "POST":
        if form.is_valid():
            with transaction.atomic():
                created, updated, deleted = form.save(request.user)
            shipment = form.cleaned_data["shipment"]
            done = []
            if created:
                total = sum((e.amount for e in created), Decimal("0"))
                AuditLog.record(
                    request.user, AuditLog.Action.CREATE, "Yuk xarajati", created[0].pk,
                    f"Yangi xarajat: {len(created)} ta · {total}$ · yuk #{shipment.pk}")
                done.append(f"{len(created)} ta xarajat qo'shildi")
            if updated:
                AuditLog.record(
                    request.user, AuditLog.Action.UPDATE, "Yuk xarajati", updated[0].pk,
                    f"Xarajat tahrirlandi: {len(updated)} ta · yuk #{shipment.pk}")
                done.append(f"{len(updated)} ta yangilandi")
            if deleted:
                AuditLog.record(
                    request.user, AuditLog.Action.DELETE, "Yuk xarajati", None,
                    f"Xarajat o'chirildi: {len(deleted)} ta · yuk #{shipment.pk}")
                done.append(f"{len(deleted)} ta o'chirildi")
            if done:
                messages.success(request, " · ".join(done))
            else:
                messages.info(request, "O'zgarish yo'q")
            # reload whichever page it was opened from (loads list or the load detail)
            return form_reload(request, reverse("shipment_detail", args=[shipment.pk]))
        return respond(invalid=True)
    return respond()


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
    page = Paginator(sales, 20).get_page(request.GET.get("page"))
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
                        # every FIFO slice inherits the one narx that was agreed,
                        # in the currency it was agreed in
                        **form.money_kwargs(),
                        date=data["date"], debt_deadline=data["debt_deadline"],
                        note=data["note"], created_by=request.user,
                    )
                    slices.append(sale)
                    remaining -= take
                    # Serving this mijoz makes their own promise smaller, whichever
                    # lot the granula came off. Per slice and in order, so the bron
                    # is drawn down by exactly what was sold.
                    draw_down_bron(sale)
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
                **form.money_kwargs(),
                date=data["date"], debt_deadline=data["debt_deadline"],
                note=data["note"], created_by=request.user,
            )
            draw_down_bron(sale)
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
    previous_customer_id = sale.customer_id
    form = SaleForm(request.POST or None, instance=sale)
    title = "Sotuvni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            # The bron this sotuv drew from is holding kg that are about to change
            # (or move to another mijoz). Put them back before the edit lands, then
            # draw again from whatever the sotuv now is — releasing afterwards would
            # give back the NEW kg, which is not what was taken.
            release_bron(sale)
            sale = form.save()
            if sale.reservation_id:
                sale.reservation = None
                sale.save(update_fields=["reservation"])
            draw_down_bron(sale)
            moved = sale.customer_id != previous_customer_id
            if moved:
                # The allocations are slices of the PREVIOUS mijoz's to'lovlar. They
                # cannot follow the sotuv to somebody else: the money would show as
                # paid here while still counting against the mijoz who handed it over.
                sale.allocations.all().delete()
            # kg or narx may have moved either way: drop allocation the sotuv can no
            # longer hold, then let avans in if it grew.
            trim_sale_allocations(sale)
            reconcile_customer_allocations(sale.customer)
            if moved:
                previous = Customer.objects.filter(pk=previous_customer_id).first()
                if previous:
                    reconcile_customer_allocations(previous)
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
        customer = sale.customer
        try:
            # The promise this sotuv settled is unkept again, so its kg go back on
            # the bron and a bron it closed reopens.
            release_bron(sale)
            sale.delete()
            # Its allocations went with it (CASCADE); that money is avans again, and
            # the mijoz's other open sotuvlar have first claim on it.
            reconcile_customer_allocations(customer)
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


#: Holat tabs, in the order a bron moves through them. Faol leads because an open
#: bron is the only kind there is anything left to do about.
RESERVATION_STATUS_LABELS = [
    ("active", "Faol"), ("converted", "Sotuvga aylandi"),
    ("cancelled", "Bekor qilindi"), ("", "Hammasi"),
]

# Sorted in Python: jami is kg × narx and lot state reads through two relations,
# neither of which is a column. Each entry is (key, label, sort key, reverse).
RESERVATION_SORTS = [
    # Navbat first: FIFO order is the rule the screen exists to enforce, so it is
    # what you should be looking at unless you deliberately ask for something else.
    ("queue", "Navbat bo'yicha", lambda r: (r.brand, r.created_at, r.pk), False),
    ("-created", "Sana — yangi avval", lambda r: (r.created_at, r.pk), True),
    ("created", "Sana — eski avval", lambda r: (r.created_at, r.pk), False),
    ("customer", "Mijoz — A-Z", lambda r: (r.customer.name.casefold(), r.pk), False),
    ("-kg", "Kg — kattadan", lambda r: (r.kg, r.pk), True),
    ("-total", "Jami — kattadan",
     lambda r: (r.kg * (r.price or Decimal("0")), r.pk), True),
]
RESERVATION_SORT_DEFAULT = "queue"


@role_required(User.Role.ADMIN)
def reservation_list(request):
    """Bronlar, kelishuvlar-style: search plus mijoz / holat / lot filters and a
    sort, with the holat counts faceted so each option shows what picking it
    yields. Everything past the mijoz filter reads computed properties, so the
    rows become a list once and are narrowed in Python from there."""
    q = request.GET.get("q", "").strip()
    customer_id = request.GET.get("customer", "").strip()
    # "ready" = at the head of its marka's queue with stock on the shelf; "waiting"
    # = open but blocked, either by the queue or by an empty ombor.
    lot = request.GET.get("lot", "").strip()
    # Faol is the working view — an open bron is the only kind with anything left
    # to act on, so it is where you land rather than the whole history.
    status = request.GET.get("status", "active").strip()
    sort = request.GET.get("sort", "").strip()
    if sort not in {key for key, *_ in RESERVATION_SORTS}:
        sort = RESERVATION_SORT_DEFAULT

    reservations = Reservation.objects.select_related("customer")
    if q:
        reservations = reservations.filter(
            Q(customer__name__icontains=q) | Q(brand__icontains=q)
            | Q(note__icontains=q))
    if customer_id.isdigit():
        reservations = reservations.filter(customer_id=int(customer_id))

    rows = list(reservations)
    # Queue position and what is on the shelf for that marka right now — the two
    # facts that decide whether a bron can be filled today. Computed once per marka
    # rather than per row: the queue walk is the same for every bron of a marka.
    shelf, queues = {}, {}
    for brand in {r.brand for r in rows}:
        shelf[brand] = brand_on_hand_kg(brand)
        queues[brand] = [b.pk for b in bron_queue(brand)]
    for row in rows:
        order = queues.get(row.brand, [])
        row.queue_pos = order.index(row.pk) + 1 if row.pk in order else None
        row.brand_on_hand = shelf.get(row.brand, Decimal("0"))
        row.servable_kg = (min(row.remaining_kg, row.brand_on_hand)
                           if row.queue_pos == 1 else Decimal("0"))
        row.blocked_by = (
            Reservation.objects.filter(pk=order[0]).select_related("customer").first()
            if row.queue_pos and row.queue_pos > 1 else None)
    if lot == "ready":
        rows = [r for r in rows if r.servable_kg > 0]
    elif lot == "waiting":
        rows = [r for r in rows if r.is_open and r.servable_kg <= 0]
    # Counted before the holat filter narrows anything, so the numbers describe
    # the other tabs rather than the one already chosen.
    status_tabs = [{"key": key, "label": label,
                    "count": (len(rows) if not key
                              else sum(1 for r in rows if r.status == key))}
                   for key, label in RESERVATION_STATUS_LABELS]
    if status:
        rows = [r for r in rows if r.status == status]

    _, _, sort_key, sort_reverse = next(e for e in RESERVATION_SORTS if e[0] == sort)
    rows.sort(key=sort_key, reverse=sort_reverse)

    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    return render(request, "crm/reservation_list.html", {
        "page": page, "q": q, "customer_id": customer_id, "status": status,
        "lot": lot, "status_tabs": status_tabs, "sort": sort,
        "sort_options": [(key, label) for key, label, *_ in RESERVATION_SORTS],
        "customers": Customer.objects.all(),
        "has_filters": bool(customer_id or lot or status != "active"),
    })


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
def reservation_edit(request, pk):
    """Only an active bron is editable. A converted one has already become a sotuv
    that snapshotted its kg and narx — editing the bron behind it would leave the
    two disagreeing with no way to tell which is real."""
    reservation = get_object_or_404(Reservation, pk=pk)
    if reservation.status != Reservation.Status.ACTIVE:
        messages.error(request, "Faqat faol bronni tahrirlash mumkin")
        return form_reload(request, reverse("reservation_list"))
    form = ReservationForm(request.POST or None, instance=reservation)
    title = "Bronni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Bron", reservation.pk,
                f"Bron tahrirlandi: {reservation.kg} kg · {reservation.customer.name}",
            )
            messages.success(request, "Bron yangilandi")
            return form_reload(request, reverse("reservation_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def reservation_delete(request, pk):
    """Hard-delete a mistyped bron. A converted one is refused: its sotuv points
    back here (SET_NULL), so deleting it would quietly cut the sotuv loose from the
    bron it came from instead of failing."""
    reservation = get_object_or_404(Reservation, pk=pk)
    label = f"{reservation.kg} kg · {reservation.customer.name}"
    if reservation.status == Reservation.Status.CONVERTED:
        messages.error(request, "Sotuvga aylangan bronni o'chirib bo'lmaydi")
        return form_reload(request, reverse("reservation_list"))
    if request.method == "POST":
        reservation.delete()
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Bron", pk,
                        f"Bron o'chirildi: {label}")
        messages.success(request, "Bron o'chirildi")
        return form_reload(request, reverse("reservation_list"))
    return render_confirm(
        request,
        "Bronni o'chirish",
        f"“{label}” broni butunlay o'chiriladi.",
        "Ha, o'chirish",
        confirm_class="btn-danger",
        cancel_url_name="reservation_list",
    )


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
    """Fill a bron from whatever of its marka is on the shelf, oldest lot first.

    Two rules the mijoz is entitled to. FIFO: an older open bron for the same marka
    must be served first, so nobody is jumped in the queue. Partial: if only some of
    the kg has landed, that part becomes a sotuv now and the bron stays open for the
    rest — loads arrive in pieces and making the mijoz wait for a full truck would
    hold their granula hostage to a later one."""
    reservation = get_object_or_404(Reservation, pk=pk)
    if not reservation.is_open:
        messages.error(request, "Bu bron ochiq emas")
        return form_reload(request, reverse("reservation_list"))

    queue = bron_queue(reservation.brand)
    if queue and queue[0].pk != reservation.pk:
        first = queue[0]
        messages.error(
            request,
            f"Navbat buzilmaydi: {first.brand} bo'yicha birinchi navbatda "
            f"{first.customer.name} turibdi ({_kg(first.remaining_kg)} kg). "
            "Avval o'shani bering yoki bronini bekor qiling.")
        return form_reload(request, reverse("reservation_list"))

    on_shelf = brand_on_hand_kg(reservation.brand)
    take_total = min(reservation.remaining_kg, on_shelf)
    if take_total <= 0:
        messages.error(request, f"{reservation.brand} omborda yo'q — yuk kutilmoqda")
        return form_reload(request, reverse("reservation_list"))

    # The bron's own money shape carries over whole — currency, kurs and both
    # values. A bron struck in so'm must not become a dollar sotuv re-rated at
    # today's kurs, which would quietly change the price the mijoz agreed to.
    price, price_uzs = reservation.price, reservation.price_uzs
    if price is None:
        raw_price = request.POST.get("price")
        try:
            typed = Decimal(raw_price) if raw_price else None
        except (ValueError, ArithmeticError):
            typed = None
        if typed is not None and typed > 0:
            # An unpriced bron is settled now, in the currency the bron was taken in.
            price, price_uzs = convert_pair(
                typed, reservation.currency, reservation.exchange_rate, "0.0001")
    if price is None:
        messages.error(request, "Narx ko'rsatilishi kerak")
        return form_reload(request, reverse("reservation_list"))
    if price_uzs is None:
        # Reservation.price_uzs is nullable (an unpriced bron), Sale.price_uzs is
        # not — a bron carrying a narx but no so'm twin still has to become a
        # complete sotuv, so derive the missing side at the bron's own kurs.
        _, price_uzs = convert_pair(price, Currency.USD, reservation.exchange_rate, "0.0001")

    remaining = take_total
    slices = []
    with transaction.atomic():
        # One Sale per lot slice, exactly as a by-brand sotuv does: each slice keeps
        # its own lot's landed cost, which differs per truck.
        for lot in fifo_lots(reservation.brand):
            if remaining <= 0:
                break
            take = min(lot.available_kg, remaining)
            if take <= 0:
                continue
            slices.append(Sale.objects.create(
                customer=reservation.customer, line=lot, kg=take,
                price=price, price_uzs=price_uzs, currency=reservation.currency,
                exchange_rate=reservation.exchange_rate, date=timezone.localdate(),
                reservation=reservation, created_by=request.user,
            ))
            remaining -= take
        reservation.fulfilled_kg += take_total - remaining
        if reservation.remaining_kg <= 0:
            reservation.status = Reservation.Status.CONVERTED
        reservation.save(update_fields=["fulfilled_kg", "status"])

    left = reservation.remaining_kg
    AuditLog.record(
        request.user, AuditLog.Action.CREATE, "Bron", reservation.pk,
        f"Brondan sotuv: {_kg(take_total - remaining)} kg {reservation.brand} · "
        f"{reservation.customer.name}"
        + (f" · {_kg(left)} kg navbatda qoldi" if left > 0 else " · bron yopildi"),
    )
    for sale in slices:  # a pre-existing advance auto-applies, oldest slice first
        apply_customer_advance(sale)
    if left > 0:
        messages.success(
            request,
            f"{_kg(take_total - remaining)} kg sotuvga aylandi — "
            f"{_kg(left)} kg navbatda qoldi")
    else:
        messages.success(request, "Bron to'liq sotuvga aylantirildi")
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
            # so the freed money becomes a reachable advance again, then let the
            # mijoz's other open sotuvlar claim it.
            trim_sale_allocations(sale)
            reconcile_customer_allocations(sale.customer)
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
    # A mijoz is a debtor if ANY currency they deal in is owed — a dollar avans does
    # not cancel a so'm qarz, so netting the two would hide a debt that is real.
    rows = []
    for c in Customer.objects.prefetch_related("sales__allocations",
                                               "customer_payments__allocations"):
        positions = customer_balance_by_currency(c)
        owed = [(currency, amount) for currency, amount in positions if amount > 0]
        if not owed:
            continue
        due = [s.debt_deadline for s in c.sales.all() if s.is_due]
        rows.append({
            "customer": c,
            "positions": owed,
            # Only for ordering the page: the biggest debt first needs one number,
            # and no figure on screen is built from it.
            "size": max(amount for _currency, amount in owed),
            "overdue_count": sum(1 for s in c.sales.all() if s.is_overdue),
            "due_count": len(due),
            "earliest_due": min(due) if due else None,
        })
    # Whoever has to pay NOW comes first — oldest muddat at the very top, so the
    # longest-waiting debt leads. Sorting the whole list by size of qarz instead
    # buried a mijoz due today under bigger debts that are not owed yet.
    chase = [r for r in rows if r["earliest_due"]]
    later = [r for r in rows if not r["earliest_due"]]
    chase.sort(key=lambda r: (r["earliest_due"], -r["size"]))
    later.sort(key=lambda r: -r["size"])
    page = Paginator(chase + later, 20).get_page(request.GET.get("page"))
    return render(request, "crm/debt_list.html", {"page": page})


@role_required(User.Role.ADMIN)
def debt_customer(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    sales = [s for s in customer.sales.select_related("line__contract_line")
             .prefetch_related("allocations").all() if s.remaining_own > 0]
    return render(request, "crm/debt_customer.html", {
        "customer": customer, "sales": sales,
        "positions": customer_balance_by_currency(customer)})


#: How the outflow rows collapse into waterfall steps. Bojxona and transport carry
#: the load on their own (88% of yuk spend in July), so they get a bar each and the
#: rest share one — six bars a reader can hold in their head beats twelve they cannot.
WATERFALL_EXPENSE_GROUPS = [
    ("customs", "Bojxona"),
    ("transport", "Transport"),
]
WATERFALL_EXPENSE_OTHER = "Boshqa xarajatlar"


def _kg(value):
    """1000.000 → "1 000", 1234.500 → "1 234.5" — kg read the way the money does,
    space-grouped and without the column's padded decimals."""
    text = f"{Decimal(value or 0):,.3f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ")


def _money_line(pairs):
    """A [(currency, amount)] figure as one line of plain text — "$500 · 6 000 000 so'm".

    The tile captions are built as strings here rather than as markup in the
    template, so they reach for the formatters directly. Joined with a raised dot
    and never a plus: the two sides sit beside each other, they do not add up."""
    return " · ".join(som(amount) if currency == Currency.UZS else usd(amount)
                      for currency, amount in pairs)


def _waterfall(opening, opening_uzs, steps):
    """Lay out a waterfall: turn (label, delta) steps into bars positioned against a
    shared scale, so the template does no arithmetic.

    Each step's bar spans from the running total before it to the running total
    after, which is what makes a waterfall readable — the bar IS the movement, and
    where it sits shows the level it moved from. The opening and closing bars are
    different animals: they measure from zero, because they are levels, not moves.

    The scale spans every level the balance passes through, so a run that dips
    negative still fits and the zero line lands where it belongs."""
    running = opening
    running_uzs = opening_uzs
    levels = [opening]
    rows = []
    for label, delta, delta_uzs in steps:
        before = running
        running += delta
        running_uzs += delta_uzs
        levels.append(running)
        rows.append({"label": label, "amount": delta, "amount_uzs": delta_uzs,
                     "kind": "in" if delta >= 0 else "out",
                     "span": (min(before, running), max(before, running)),
                     "running": running, "running_uzs": running_uzs})

    low, high = min(levels + [Decimal("0")]), max(levels + [Decimal("0")])
    span = high - low
    def place(lo, hi):
        """(left %, width %) — a zero-width bar still gets a hairline to sit on."""
        if span <= 0:
            return 0.0, 100.0
        return (float((lo - low) / span * 100),
                max(float((hi - lo) / span * 100), 0.35))

    bars = [{"label": "Boshlang'ich qoldiq", "amount": opening, "amount_uzs": opening_uzs,
             "kind": "total", "running": opening, "running_uzs": opening_uzs,
             "span": (min(Decimal("0"), opening), max(Decimal("0"), opening))}]
    bars += rows
    bars.append({"label": "Qoldiq", "amount": running, "amount_uzs": running_uzs,
                 "kind": "total", "running": running, "running_uzs": running_uzs,
                 "span": (min(Decimal("0"), running), max(Decimal("0"), running))})
    for bar in bars:
        bar["left"], bar["width"] = place(*bar["span"])
    return bars, place(Decimal("0"), Decimal("0"))[0]


@role_required(User.Role.ADMIN)
def kassa(request):
    """The till, client-crm style: a current-state hero (Kassadagi pul + what we
    owe hamkorlar), per-method USD balances for the selected period, and two
    Excel-like ledgers side by side — Kirim (customer payments) and Chiqim
    (supplier payments + shipment expenses). Purely derived; ?from&to narrows
    the period section, the hero is all-time."""
    date_from = _date_param(request, "from")
    date_to = _date_param(request, "to")

    def _range(qs):
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        return qs

    # Summed per row, not in SQL: what reaches the kassa is net of the bank's foiz
    # on the way in and gross of it (plus the vositachi cut) on the way out, and
    # both are Python properties. Summing rows also keeps every total equal to the
    # figures printed beside them — one SQL expression would round once at the end.
    def _in(qs):
        return sum((p.net_amount for p in qs), Decimal("0"))

    def _out(qs):
        return sum((r.total_out for r in qs), Decimal("0"))

    def _in_uzs(qs):
        return sum((p.net_amount_uzs for p in qs), Decimal("0"))

    def _out_uzs(qs):
        return sum((r.total_out_uzs for r in qs), Decimal("0"))

    # Joriy holat (all-time, filter-independent): money physically in the till.
    # ShipmentExpense.total_out is already zero for a logist-funded row, so the
    # whole queryset can be summed: the money left when we topped the logist up,
    # and LogistPayment below is where that shows.
    #
    # The converted pair is still what the Oqim waterfall closes on — it has to be,
    # since the waterfall is a single running line and cannot be two. The tiles
    # above it read the per-currency figures instead; see the `split` note there.
    cash_total = (_in(CustomerPayment.objects.all())
                  - _out(SupplierPayment.objects.all())
                  - _out(ShipmentExpense.objects.all())
                  - _out(LogistPayment.objects.all()))
    cash_total_uzs = (_in_uzs(CustomerPayment.objects.all())
                      - _out_uzs(SupplierPayment.objects.all())
                      - _out_uzs(ShipmentExpense.objects.all())
                      - _out_uzs(LogistPayment.objects.all()))

    # Not all of the till is ours. Money a mijoz has handed over that sits on no
    # sotuv is held, not earned — cancel the order and it goes back out.
    advance, advance_uzs = customer_advance_total()
    own_cash, own_cash_uzs = cash_total - advance, cash_total_uzs - advance_uzs

    # The same two facts as heaps rather than as one sum converted twice: so'm in
    # the safe is not dollars in the safe.
    cash_split = kassa_cash_by_currency()
    advance_split = customer_advance_by_currency()

    # Pozitsiya: what the cash figure MEANS. Cash alone reads as a disaster while
    # the money is sitting in trucks and mijoz qarzi; these are the lines that say
    # where it went. Current-state, so the date filter does not touch them.
    receivable, debtors = customer_receivable_by_currency()
    hamkor = partner_positions_by_currency()
    logist_held, logist_held_uzs, logist_owed, logist_owed_uzs = logist_positions()
    stock, stock_uzs, stock_kg = stock_value()
    transit, transit_kg, transit_loads = transit_value_by_currency()
    # A board of current facts, not a balance sheet. Each tile is one place money
    # or goods is sitting RIGHT NOW, says so in plain words, and carries the second
    # fact that makes it actionable — how many kg, how many mijoz, how many yuk.
    # Nothing is summed across tiles: adding cash to granula to somebody else's debt
    # produces a number that describes no actual thing.
    #
    # `split` is what makes a tile read per-currency: a list of (currency, amount)
    # drawn as one line each, never added up. Cash and every obligation carry one —
    # a dollar debt and a so'm debt are two debts, and the till holds two heaps.
    # `amount`/`amount_uzs` is the older converted pair, and the tiles still on it
    # are the ones whose figure is a COST: a kg has one landed cost even though the
    # mol was bought in dollars and the transport paid in so'm, which is the one
    # place currencies are deliberately blended (tests/test_cost_blends_currencies.py).
    tiles = [
        {"label": "Kassada", "split": cash_split,
         "note": "hozir qo'lda va hisobda turgan pul", "tone": "cash",
         "meta": (f"shundan mijoz avansi {_money_line(advance_split)}"
                  if advance_split else ""),
         "url": reverse("customer_payment_list")},
        {"label": "Mijozlar qarzi", "split": receivable,
         "note": "mol berilgan, puli hali olinmagan", "tone": "in",
         "meta": f"{debtors} ta mijozda" if debtors else "",
         "url": reverse("debt_list")},
        {"label": "Omborda", "split": None, "amount": stock, "amount_uzs": stock_uzs,
         "note": "kelgan, hali sotilmagan mol — tannarxda", "tone": "in",
         "meta": f"{_kg(stock_kg)} kg", "url": reverse("ombor")},
        {"label": "Yo'lda", "split": transit,
         "note": "jo'natilgan, hali yetib kelmagan mol", "tone": "in",
         "meta": f"{_kg(transit_kg)} kg · {transit_loads} ta yuk",
         "url": reverse("shipment_list")},
        {"label": "Hamkorlarda avansimiz", "split": hamkor["prepaid"], "tone": "in",
         "note": "yuk kelishidan oldin to'lab qo'yganimiz",
         "meta": f"{hamkor['contracts']} ta kelishuvda" if hamkor["contracts"] else "",
         "url": reverse("contract_list")},
        {"label": "Logistlarda", "split": None,
         "amount": logist_held, "amount_uzs": logist_held_uzs,
         "note": "haydovchilarga berish uchun yuborilgan, hali sarflanmagan",
         "tone": "in", "meta": "", "url": reverse("logist_list")},
        {"label": "Hamkorlarga qarzimiz", "split": hamkor["owed"], "tone": "out",
         "note": "kelgan yuk uchun hali to'lamaganimiz",
         "meta": f"{hamkor['partners']} ta hamkorga" if hamkor["partners"] else "",
         "url": reverse("supplier_payment_list")},
    ]
    # A logist who fronted their own cash is a debt we owe, and a debt nobody can
    # see is one nobody pays. Appended only when it exists: unlike every other tile
    # this one is usually zero, and a permanently empty tile is noise, not a fact.
    #
    # Still a converted pair: a logist's hisob is kept in dollars whatever they hand
    # a driver, so there is no per-currency split to draw yet.
    if logist_owed:
        tiles.append({
            "label": "Logistlarga qarzimiz", "split": None, "amount": -logist_owed,
            "amount_uzs": -logist_owed_uzs, "tone": "out", "meta": "",
            "note": "o'z pulidan haydovchiga bergani",
            "url": reverse("logist_list") + "?state=owed"})

    # The Kirim ledger asks each row what it settled and whether its kurs was chosen,
    # and both answers are read off the allocations — without the prefetch that is a
    # query per to'lov, then one per slice, for every row on the page.
    cust_pays = _range(CustomerPayment.objects.select_related("customer")
                       .prefetch_related("allocations__sale"))
    sup_pays = _range(SupplierPayment.objects.select_related("contract__partner"))
    expenses = _range(ShipmentExpense.objects.select_related("shipment__contract", "logist"))
    logist_pays = _range(LogistPayment.objects.select_related("logist"))

    balances = {}
    net_in = net_out = Decimal("0")
    net_in_uzs = net_out_uzs = Decimal("0")
    for value, label in PayMethod.choices:
        m_in = _in(cust_pays.filter(method=value))
        m_out = (_out(sup_pays.filter(method=value))
                 + _out(expenses.filter(method=value))
                 + _out(logist_pays.filter(method=value)))
        m_in_uzs = _in_uzs(cust_pays.filter(method=value))
        m_out_uzs = (_out_uzs(sup_pays.filter(method=value))
                     + _out_uzs(expenses.filter(method=value))
                     + _out_uzs(logist_pays.filter(method=value)))
        balances[value] = {"label": label, "in": m_in, "out": m_out,
                           "balance": m_in - m_out, "in_uzs": m_in_uzs,
                           "out_uzs": m_out_uzs, "balance_uzs": m_in_uzs - m_out_uzs}
        net_in += m_in
        net_out += m_out
        net_in_uzs += m_in_uzs
        net_out_uzs += m_out_uzs

    # Kirim ledger: payments received from customers, newest first.
    income_rows = sorted(cust_pays, key=lambda p: (p.date, p.pk), reverse=True)

    # Chiqim ledger: money out — supplier payments and per-load expenses.
    outflow_rows = []
    for p in sup_pays:
        outflow_rows.append({
            "kind": "supplier", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            # The hamkor is already inside the code, so the brand is the useful half here
            "title": f"Kelishuv {p.contract.code} · {p.contract.brand_summary}",
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": p.amount_uzs, "amount": p.amount,
        })
    for p in sup_pays:
        if not p.commission_amount:
            continue
        outflow_rows.append({
            "kind": "commission", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": (f"Vositachi ({p.commission_percent}%) · "
                      f"kelishuv {p.contract.code}"),
            "method_code": p.method, "method": p.get_method_display(),
            # The cut is a slice of the payment, so it inherits that row's kurs
            # rather than being re-rated at today's.
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": uzs_slice(p, p.commission_amount),
            "amount": p.commission_amount,
        })
    for p in sup_pays:
        if not p.fee_amount:
            continue
        outflow_rows.append({
            "kind": "fee_supplier", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": f"Perechisleniya foizi ({p.fee_percent}%) · kelishuv {p.contract.code}",
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": p.fee_amount_uzs, "amount": p.fee_amount,
        })
    for p in logist_pays:
        outflow_rows.append({
            "kind": "logist", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": f"Logist {p.logist.name}ga" + (f" · {p.note}" if p.note else ""),
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": p.amount_uzs, "amount": p.amount,
        })
    for p in logist_pays:
        if not p.fee_amount:
            continue
        outflow_rows.append({
            "kind": "fee_logist", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": f"Perechisleniya foizi ({p.fee_percent}%) · logist {p.logist.name}",
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": p.fee_amount_uzs, "amount": p.fee_amount,
        })
    # Logist-funded expenses are deliberately absent from this ledger: the cash they
    # cost left as the LogistPayment above, and listing them here would show the
    # same money going out twice.
    for e in expenses:
        if not e.from_kassa:
            continue
        outflow_rows.append({
            "kind": "expense", "pk": e.pk, "date": e.date, "obj": e,
            "crossed": e.crosses_currency,
            "title": f"{e.get_category_display()} · yuk #{e.shipment_id}",
            "method_code": e.method, "method": e.get_method_display(),
            "currency": e.currency, "exchange_rate": e.exchange_rate,
            "amount_uzs": e.amount_uzs, "amount": e.amount,
        })
    for e in expenses:
        if not e.fee_amount or not e.from_kassa:
            continue
        outflow_rows.append({
            "kind": "fee_expense", "pk": e.pk, "date": e.date, "obj": e,
            "crossed": e.crosses_currency,
            "title": f"Perechisleniya foizi ({e.fee_percent}%) · yuk #{e.shipment_id}",
            "method_code": e.method, "method": e.get_method_display(),
            "currency": e.currency, "exchange_rate": e.exchange_rate,
            "amount_uzs": e.fee_amount_uzs, "amount": e.fee_amount,
        })
    outflow_rows.sort(key=lambda r: (r["date"], r["pk"]), reverse=True)

    # Each ledger pages independently (?ipage / ?opage) so scrolling one doesn't
    # reset the other. The +/- totals above are the whole-period figures, not the
    # page's, so they stay computed from the full lists.
    income_page = Paginator(income_rows, 20).get_page(request.GET.get("ipage"))
    outflow_page = Paginator(outflow_rows, 20).get_page(request.GET.get("opage"))

    # What we owe hamkorlar RIGHT NOW (not date-filtered — a current-state figure):
    # per contract the debt accrues per shipped truck (shipped value − paid).
    payables = {}
    for c in Contract.objects.select_related("partner").prefetch_related("shipments"):
        d = c.debt
        if d > 0:
            # Not named `usd`: assigning that name anywhere in this function makes
            # it local for the WHOLE scope, so the `usd` formatter imported at module
            # level becomes unreachable everywhere above this line too.
            dollars, sums = payables.get(c.partner, (Decimal("0"), Decimal("0")))
            payables[c.partner] = (dollars + d, sums + c.debt_uzs)
    partner_debts = sorted(payables.items(), key=lambda kv: kv[1][0], reverse=True)
    payable_total = sum((d for _, (d, _u) in partner_debts), Decimal("0"))
    payable_total_uzs = sum((u for _, (_d, u) in partner_debts), Decimal("0"))

    # Oqim: the same money as the ledgers below, told as a sequence. The opening
    # balance is whatever the till had moved to before the period started — with no
    # filter that is zero, which is honest: the kassa has no kapital rows yet, so
    # the run genuinely starts from nothing. Adding them later adds a bar, nothing
    # else changes.
    if date_from:
        prior = (CustomerPayment.objects.filter(date__lt=date_from),
                 SupplierPayment.objects.filter(date__lt=date_from),
                 ShipmentExpense.objects.filter(date__lt=date_from),
                 LogistPayment.objects.filter(date__lt=date_from))
        opening = _in(prior[0]) - sum((_out(q) for q in prior[1:]), Decimal("0"))
        opening_uzs = _in_uzs(prior[0]) - sum((_out_uzs(q) for q in prior[1:]),
                                              Decimal("0"))
    else:
        opening = opening_uzs = Decimal("0")

    sup_amount = sum((p.amount for p in sup_pays), Decimal("0"))
    sup_amount_uzs = sum((p.amount_uzs for p in sup_pays), Decimal("0"))
    commission = commission_total(sup_pays)
    commission_uzs = sum((p.commission_amount_uzs for p in sup_pays), Decimal("0"))
    # Outgoing foiz only. A mijoz's perechisleniya foiz never reached the kassa —
    # `net_in` is already net of it — so billing it again here would take the same
    # money out twice and the waterfall would stop landing on the ledger total.
    outgoing = list(sup_pays) + list(expenses) + list(logist_pays)
    fees = sum((r.fee_amount for r in outgoing), Decimal("0"))
    fees_uzs = sum((r.fee_amount_uzs for r in outgoing), Decimal("0"))

    logist_amount = sum((p.amount for p in logist_pays), Decimal("0"))
    logist_amount_uzs = sum((p.amount_uzs for p in logist_pays), Decimal("0"))
    steps = [("Mijozlardan", net_in, net_in_uzs),
             ("Hamkorlarga", -sup_amount, -sup_amount_uzs),
             ("Vositachi ustamasi", -commission, -commission_uzs),
             ("Logistlarga", -logist_amount, -logist_amount_uzs)]
    grouped = Decimal("0")
    grouped_uzs = Decimal("0")
    kassa_expenses = [e for e in expenses if e.from_kassa]
    for key, label in WATERFALL_EXPENSE_GROUPS:
        rows = [e for e in kassa_expenses if e.category == key]
        steps.append((label, -sum((e.amount for e in rows), Decimal("0")),
                      -sum((e.amount_uzs for e in rows), Decimal("0"))))
    known = {key for key, _ in WATERFALL_EXPENSE_GROUPS}
    for expense in kassa_expenses:
        if expense.category not in known:
            grouped += expense.amount
            grouped_uzs += expense.amount_uzs
    steps.append((WATERFALL_EXPENSE_OTHER, -grouped, -grouped_uzs))
    steps.append(("Bank foizi", -fees, -fees_uzs))
    # A step worth nothing is a bar with no bar — drop it rather than draw a label
    # against empty space. Bank foizi is usually the one: it is barely used.
    steps = [s for s in steps if s[1]]
    waterfall, zero_line = _waterfall(opening, opening_uzs, steps)

    # Quick period presets for the filter bar.
    today = timezone.localdate()
    presets = [
        ("Bugun", today.isoformat(), today.isoformat()),
        ("7 kun", (today - timedelta(days=6)).isoformat(), today.isoformat()),
        ("30 kun", (today - timedelta(days=29)).isoformat(), today.isoformat()),
        ("Hammasi", "", ""),
    ]

    return render(request, "crm/kassa.html", {
        "cash_total": cash_total, "cash_total_uzs": cash_total_uzs,
        "advance": advance, "advance_uzs": advance_uzs,
        "own_cash": own_cash, "own_cash_uzs": own_cash_uzs,
        "tiles": tiles,
        "waterfall": waterfall, "zero_line": zero_line,
        "balances": balances, "net_in": net_in, "net_out": net_out,
        "net_total": net_in - net_out,
        "net_in_uzs": net_in_uzs, "net_out_uzs": net_out_uzs,
        "net_total_uzs": net_in_uzs - net_out_uzs,
        "income_page": income_page, "outflow_page": outflow_page,
        "partner_debts": partner_debts, "payable_total": payable_total,
        "payable_total_uzs": payable_total_uzs,
        "date_from": date_from, "date_to": date_to, "presets": presets,
    })


def _date_param(request, key):
    """A ?from / ?to querystring value, or "" when it is not a real YYYY-MM-DD date.

    These land straight in a `date__gte=` filter, and Django raises ValidationError
    on anything it cannot parse — which escapes the view as a 500. A querystring is
    typed by hand, kept in bookmarks and copied between screens, so a stale or
    mistyped one must narrow nothing rather than take the page down."""
    raw = (request.GET.get(key) or "").strip()
    if not raw:
        return ""
    try:
        return _date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def _report_filters(request):
    """Parse the shared reports/exports querystring filters (?from&to&partner&brand&status)."""
    return {
        "date_from": _date_param(request, "from"),
        "date_to": _date_param(request, "to"),
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
    # Every money KPI here is per currency, the same rule the kassa board follows:
    # summing both stored columns counts each dollar row a second time in so'm
    # clothing, which reported 3 758 mln so'm of mijoz qarzi against a real 1 128 mln.
    priced = contracts.prefetch_related("lines__shipment_lines", "supplier_payments")
    kontrakt_summasi = contract_value_by_currency(priced)
    hamkorga_tolangan = supplier_paid_by_currency(sup_pays)
    hamkor_qarzi = _by_currency((c.currency, c.debt_own) for c in priced)
    mijoz_qarzi, _debtors = customer_receivable_by_currency()
    # Foyda stays a converted pair: it is measured against the landed cost, and a
    # tannarx blends a dollar mol with a so'm transport bill by design.
    profit_total = sum((s.profit for s in sales), Decimal("0"))
    profit_total_uzs = sum((s.profit_uzs for s in sales), Decimal("0"))
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
            "kontrakt_summasi": contract_value_by_currency(p_contracts),
            "tolangan": supplier_paid_by_currency(
                sup_pays.filter(contract__partner=partner)),
            "qarz": _by_currency((c.currency, c.debt_own) for c in p_contracts),
        })
    # Ordered by the largest single side, since two currencies cannot be added into
    # one sort key. Nothing on screen is built from it.
    partner_rows.sort(key=lambda r: max([a for _c, a in r["qarz"]] or [Decimal("0")]),
                      reverse=True)

    # Per-customer table
    customer_rows = []
    customer_ids = sales.values_list("customer_id", flat=True).distinct()
    for customer in Customer.objects.filter(pk__in=customer_ids):
        c_sales = sales.filter(customer=customer)
        # net (post-returns) so the row reconciles with the net-based qarz column
        owed = [(currency, amount)
                for currency, amount in customer_balance_by_currency(customer)
                if amount > 0]
        customer_rows.append({
            "customer": customer,
            "sotildi": customer_sales_by_currency(c_sales),
            "tolandi": customer_paid_by_currency(cust_pays.filter(customer=customer)),
            "qarz": owed,
        })
    customer_rows.sort(key=lambda r: max([a for _c, a in r["qarz"]] or [Decimal("0")]),
                       reverse=True)

    return render(request, "crm/reports.html", {
        "kelishilgan_kg": kelishilgan_kg, "yuborilgan_kg": yuborilgan_kg,
        "omborga_kelgan_kg": omborga_kelgan_kg, "kontrakt_summasi": kontrakt_summasi,
        "hamkorga_tolangan": hamkorga_tolangan, "hamkor_qarzi": hamkor_qarzi,
        "mijoz_qarzi": mijoz_qarzi, "profit_total": profit_total,
        "profit_total_uzs": profit_total_uzs,
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
    headers = ["Kelishuv", "Sana", "Hamkor", "Marka", "Kg", "Valyuta", "Kurs",
               "Narx ($)", "Narx (so'm)", "Jami ($)", "Jami (so'm)", "Yuborilgan kg",
               "To'langan ($)", "To'langan (so'm)", "Qarz ($)", "Qarz (so'm)"]
    # One row per product, so a multi-product kelishuv is readable in Excel. The
    # money columns are per kelishuv, so they repeat down its rows. Both currencies
    # ship in every export — which one the reader wants is not knowable here, and a
    # spreadsheet cannot follow the app's toggle.
    rows = (
        [c.code, c.created, c.partner.name, ln.brand, ln.kg,
         ln.get_currency_display(), ln.exchange_rate, ln.price, ln.price_uzs,
         ln.total_value, ln.total_value_uzs, ln.shipped_kg,
         c.paid_total, c.paid_total_uzs, c.debt, c.debt_uzs]
        for c in contracts.prefetch_related("lines__shipment_lines", "supplier_payments")
        for ln in c.lines.all()
    )
    return xlsx_response("kelishuvlar.xlsx", headers, rows)


@role_required(User.Role.ADMIN)
def export_supplier_payments(request):
    sup_pays = _report_querysets(request)["sup_pays"]
    headers = ["Sana", "Kelishuv", "Hamkor", "Valyuta", "Kurs", "Hamkorga ($)",
               "Hamkorga (so'm)", "Vositachi %", "Vositachi ($)", "Perechisleniya %",
               "Perechisleniya ($)", "Kassadan ($)", "Kassadan (so'm)", "Usul"]
    rows = (
        [p.date, p.contract.code, p.contract.partner.name, p.get_currency_display(),
         p.exchange_rate, p.amount, p.amount_uzs, p.commission_percent,
         p.commission_amount, p.fee_percent, p.fee_amount,
         p.total_out, p.total_out_uzs, p.get_method_display()]
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
    headers = ["Sana", "Mijoz", "Lot ID", "Marka", "Kg", "Valyuta", "Kurs", "Tan narx ($)",
               "Sotuv narx ($)", "Sotuv narx (so'm)", "Jami ($)", "Jami (so'm)",
               "Foyda ($)", "Foyda (so'm)", "Qoldiq ($)", "Qoldiq (so'm)"]
    rows = (
        [s.date, s.customer.name, s.line_id, s.line.brand, s.kg,
         s.get_currency_display(), s.exchange_rate, s.cost_price,
         s.price, s.price_uzs, s.total, s.total_uzs,
         s.profit, s.profit_uzs, s.remaining, s.remaining_uzs]
        for s in sales
    )
    return xlsx_response("sotuvlar.xlsx", headers, rows)


@role_required(User.Role.ADMIN)
def export_debts(request):
    # A qarz column carries only what is owed IN that currency. Putting a dollar
    # sotuv's so'm face in the so'm column too counts it twice, and this figure
    # leaves the app — it is read in Excel with no row context to correct it.
    headers = ["Mijoz", "Telefon", "Jami savdo ($)", "Jami savdo (so'm)",
               "To'langan ($)", "To'langan (so'm)", "Qarz ($)", "Qarz (so'm)"]

    def _rows():
        customers = Customer.objects.prefetch_related(
            "sales__returns", "sales__allocations", "customer_payments__allocations")
        for customer in customers:
            owed = dict(customer_balance_by_currency(customer))
            usd, uzs = owed.get(Currency.USD, Decimal("0")), owed.get(Currency.UZS, Decimal("0"))
            if usd <= 0 and uzs <= 0:
                continue
            sold = dict(customer_sales_by_currency(customer.sales.all()))
            paid = dict(customer_paid_by_currency(customer.customer_payments.all()))
            yield [customer.name, customer.phone,
                   sold.get(Currency.USD, Decimal("0")), sold.get(Currency.UZS, Decimal("0")),
                   paid.get(Currency.USD, Decimal("0")), paid.get(Currency.UZS, Decimal("0")),
                   max(usd, Decimal("0")), max(uzs, Decimal("0"))]

    return xlsx_response("qarzdorlar.xlsx", headers, _rows())


# ── Logistlar ────────────────────────────────────────────────────────────────────
#
# A logist holds our money: we send them a lump, they hand each driver an advance
# when a yuk goes out. So the list is a balance sheet, not a directory — the name
# on its own tells you nothing you need.


@role_required(User.Role.ADMIN)
def logist_list(request):
    q = request.GET.get("q", "").strip()
    state = request.GET.get("state", "").strip()
    logists = Logist.objects.prefetch_related(
        "payments", "driver_advances__shipment", "shipments")
    if q:
        logists = logists.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    rows = list(logists)
    if state == "holding":
        rows = [x for x in rows if x.balance > 0]
    elif state == "owed":
        rows = [x for x in rows if x.balance < 0]
    elif state == "settled":
        rows = [x for x in rows if x.balance == 0]

    held, held_uzs, owed, owed_uzs = logist_positions()
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    return render(request, "crm/logist_list.html", {
        "page": page, "q": q, "state": state,
        "held": held, "held_uzs": held_uzs, "owed": owed, "owed_uzs": owed_uzs,
        "has_filters": bool(state),
    })


@role_required(User.Role.ADMIN)
def logist_detail(request, pk):
    """One logist's account: every top-up in, every driver advance out, newest
    first, with the running balance the list page shows."""
    logist = get_object_or_404(
        Logist.objects.prefetch_related("payments", "driver_advances__shipment"), pk=pk)
    rows = []
    for payment in logist.payments.all():
        rows.append({"kind": "in", "date": payment.date, "obj": payment,
                     "title": payment.note or "Bizdan olindi",
                     "amount": payment.net_amount, "amount_uzs": payment.net_amount_uzs,
                     "currency": payment.currency, "method": payment.get_method_display(),
                     "method_code": payment.method})
    for advance in logist.driver_advances.all():
        rows.append({"kind": "out", "date": advance.date, "obj": advance,
                     "title": f"Yuk #{advance.shipment_id} · {advance.get_category_display()}"
                              + (f" · {advance.note}" if advance.note else ""),
                     "amount": advance.amount, "amount_uzs": advance.amount_uzs,
                     "currency": advance.currency, "method": advance.get_method_display(),
                     "method_code": advance.method})
    rows.sort(key=lambda r: (r["date"], r["obj"].pk), reverse=True)
    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    return render(request, "crm/logist_detail.html", {"logist": logist, "page": page})


@role_required(User.Role.ADMIN)
def logist_create(request):
    form = LogistForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            logist = form.save()
            AuditLog.record(request.user, AuditLog.Action.CREATE, "Logist", logist.pk,
                            f"Yangi logist: {logist.name}")
            messages.success(request, "Logist qo'shildi")
            return form_success(request, reverse("logist_list"))
        return form_response(request, form, "Yangi logist", invalid=True)
    return form_response(request, form, "Yangi logist")


@role_required(User.Role.ADMIN)
def logist_edit(request, pk):
    logist = get_object_or_404(Logist, pk=pk)
    form = LogistForm(request.POST or None, instance=logist)
    title = "Logistni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(request.user, AuditLog.Action.UPDATE, "Logist", logist.pk,
                            f"Logist tahrirlandi: {logist.name}")
            messages.success(request, "Logist yangilandi")
            return form_reload(request, reverse("logist_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def logist_delete(request, pk):
    logist = get_object_or_404(Logist, pk=pk)
    if request.method == "POST":
        try:
            logist.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Logist", pk,
                            f"Logist o'chirildi: {logist.name}")
            messages.success(request, "Logist o'chirildi")
        except ProtectedError:
            # PROTECT on both sides: a logist with money or loads behind them is a
            # piece of the ledger, not a contact card to tidy away.
            messages.error(request, "Logistga to'lov yoki yuk biriktirilgan")
        return form_reload(request, reverse("logist_list"))
    return render_confirm(
        request, "Logistni o'chirish", f"“{logist.name}” o'chiriladi.",
        "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="logist_list")


@role_required(User.Role.ADMIN)
def logist_payment_create(request):
    initial = {}
    logist_id = request.GET.get("logist")
    if logist_id and logist_id.isdigit():
        initial["logist"] = int(logist_id)
    form = LogistPaymentForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user
            payment.save()
            AuditLog.record(
                request.user, AuditLog.Action.PAYMENT, "Logistga to'lov", payment.pk,
                f"Logistga to'lov: {payment.amount}$ · {payment.logist.name}")
            messages.success(request, "Logistga to'lov qo'shildi")
            return form_success(request, reverse("logist_list"))
        return form_response(request, form, "Logistga to'lov", invalid=True)
    return form_response(request, form, "Logistga to'lov")


@role_required(User.Role.ADMIN)
def logist_payment_edit(request, pk):
    payment = get_object_or_404(LogistPayment, pk=pk)
    form = LogistPaymentForm(request.POST or None, instance=payment)
    title = "To'lovni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Logistga to'lov", payment.pk,
                f"Logistga to'lov tahrirlandi: {payment.amount}$ · {payment.logist.name}")
            messages.success(request, "To'lov yangilandi")
            return form_reload(request, reverse("logist_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def logist_payment_delete(request, pk):
    payment = get_object_or_404(LogistPayment, pk=pk)
    if request.method == "POST":
        label = f"{payment.amount}$ · {payment.logist.name}"
        payment.delete()
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Logistga to'lov", pk,
                        f"Logistga to'lov o'chirildi: {label}")
        messages.success(request, "To'lov o'chirildi")
        return form_reload(request, reverse("logist_list"))
    return render_confirm(
        request, "To'lovni o'chirish",
        f"“{payment.amount}$ · {payment.logist.name}” to'lovi o'chiriladi.",
        "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="logist_list")
