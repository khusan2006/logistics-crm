from datetime import date as _date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlparse
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, ProtectedError, Q, Sum
from django.http import JsonResponse, QueryDict
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
    ContractForm, ContractLineFormSet, CustomerAvansForm, CustomerForm, TruckPlanForm,
    CustomerPaymentForm,
    contract_currency,
    CustomerPaymentFormSet, CustomerPaymentTargetForm, PartnerForm, ReservationForm, ReturnForm,
    CustomsAgentForm, CustomsPaymentForm,
    ExpenseGridForm, KapitalForm, LogistForm, LogistPaymentForm,
    SaleCreateForm, SaleForm, SaleLineFormSet, SaleLotForm, ShipmentExpenseForm,
    ShipmentDriverForm, ShipmentExtendForm, ShipmentForm, ShipmentLineFormSet,
    ShipmentLegForm, ShipmentQrForm, ShipmentStatusForm, SupplierPaymentForm,
)
from .models import (
    AuditLog, CustomsAgent, CustomsPayment, Kapital, KapitalKind,
    Logist, LogistPayment, Contract, ContractLine, Currency, Customer, CustomerPayment, Partner,
    PaymentAllocation,
    PayMethod, Reservation, Return, Sale, Shipment, ShipmentDelay, ShipmentExpense, ShipmentLeg,
    ShipmentLine, ShipmentStatus, SupplierPayment, allocate_customer_payment,
    apply_customer_advance, arrived_lots, brand_on_hand_kg, brand_reserved_kg,
    _by_currency, bron_queue, commission_total, contract_value_by_currency,
    convert_pair,
    customer_paid_by_currency, customer_sales_by_currency,
    draw_down_bron, release_bron,
    customs_positions, logist_positions, payable_by_currency, supplier_paid_by_currency,
    customer_advance_by_currency, customer_advance_total, customer_balance_by_currency,
    customer_receivable_by_currency, customer_receivable_total, fifo_lots,
    kapital_total_by_currency,
    kassa_cash_by_currency, own_side, partner_positions, partner_positions_by_currency,
    reconcile_customer_allocations, stock_value, transit_value,
    transit_value_by_currency, trim_sale_allocations,
    unspent_payment_amount, uzs_slice,
)
from .utils import form_reload, form_response, form_success, is_ajax, render_confirm


def _bar_pct(part, whole):
    """Fill percentage for a progress bar, clamped to the track.

    A kelishuv overshoots in both directions — a truck loaded over the agreed kg, an
    avans paid before a single load moves — and a fill wider than its track spills
    out of the card instead of reading as "done"."""
    if not whole:
        return 0
    return min(100, max(0, int(Decimal(part) * 100 / Decimal(whole))))


def dashboard(request):
    if not request.user.is_admin_role:
        # Everyone lands on the first page their role can actually open. A skladchi
        # sent to Yuklar would meet a 403 on login, since that page is not theirs.
        return redirect("ombor" if request.user.is_skladchi else "shipment_list")
    # `legs` for the kechikkan table: it names the transport carrying the load NOW,
    # which is the active leg's, and reading that per row is a query per row.
    shipments = (Shipment.objects.select_related("contract__partner", "status")
                 .prefetch_related("lines__contract_line", "legs"))
    # Prefetched here rather than per figure below: the chart reads every kelishuv's
    # lines, payments and yuklar, which is three queries per row without this.
    contracts = (Contract.objects.select_related("partner")
                 .prefetch_related("shipments", "lines__shipment_lines",
                                   "supplier_payments"))
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
    debt_split = payable_by_currency(contracts)
    overdue = [s for s in shipments.filter(arrived__isnull=True, eta__isnull=False)
               if s.is_overdue]
    # Each holat with how many trucks each hamkor has sitting in it. Listing the
    # loads themselves repeated the same kelishuv kod once per truck; the question
    # being asked is "whose trucks are on the road", which is a count per hamkor.
    #
    # Each hamkor then opens into WHAT is in those trucks — a marka and a count —
    # because "sobir 6 ta" says a great deal less than "sobir 6 ta: 2102 4 ta,
    # 7000 2 ta". A truck carrying two markalar is counted under both, so the marka
    # figures can add up past the hamkor's own: they answer what is moving, not how
    # the trucks divide.
    by_status = {}
    for shipment in shipments:
        row = by_status.setdefault(shipment.status_id, {"total": 0, "partners": {}})
        row["total"] += 1
        name = shipment.contract.partner.name
        partner = row["partners"].setdefault(name, {"count": 0, "brands": {}})
        partner["count"] += 1
        for brand in {line.brand for line in shipment.lines.all()}:
            partner["brands"][brand] = partner["brands"].get(brand, 0) + 1
    status_rows = [
        {"status": st, "total": by_status[st.pk]["total"],
         # busiest hamkor first, ties by name; the markalar under each read the same
         "partners": [
             {"name": name, "count": p["count"],
              "brands": sorted(p["brands"].items(), key=lambda kv: (-kv[1], kv[0]))}
             for name, p in sorted(by_status[st.pk]["partners"].items(),
                                   key=lambda kv: (-kv[1]["count"], kv[0]))]}
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

    # The progress chart is about business still in flight, so a Yopilgan kelishuv
    # drops off it: showing every kelishuv filled the card with finished
    # 120 000 / 120 000 bars and buried the one that was actually mid-delivery.
    #
    # Three figures per row, because a kelishuv is only done when all three are:
    # mashina (the headline — 2/4 trucks gone), yuk (kg delivered) and to'lov (paid
    # against what the kelishuv will really cost). The to'lov side is read in the
    # kelishuv's own currency — see Contract.is_settled for why the converted twin
    # would never agree with it.
    CHART_LIMIT = 8
    chart_contracts = []
    for contract in contracts:
        if contract.is_settled:
            continue
        sent, planned = contract.truck_progress
        chart_contracts.append({
            "contract": contract,
            "sent": sent, "planned": planned,
            # `planned` is None on a kelishuv that never set a target, so it has no
            # trucks-left to sort on and lands at the bottom with a count and no total.
            "trucks_left": planned - sent if planned else 0,
            "shipped_kg": contract.shipped_kg, "kg": contract.kg,
            "kg_pct": _bar_pct(contract.shipped_kg, contract.kg),
            "paid": contract.paid_total_own, "due": contract.expected_value_own,
            "pay_pct": _bar_pct(contract.paid_total_own, contract.expected_value_own),
        })
    contracts_total = len(chart_contracts)
    # Most trucks still to send first — the same reading as Yuboriladigan mashinalar.
    chart_contracts.sort(key=lambda r: (r["trucks_left"], r["sent"]), reverse=True)
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


def _partner_history(partner):
    """Everything that has passed between us and one hamkor, newest first.

    The three things that actually happen with a hamkor, on one timeline: a
    kelishuv is struck, money goes out against it, and goods come the other way.
    Read separately they never line up — the yuk that a to'lov paid for is on
    another screen — and lining them up is the whole point of the page.

    Each row is drawn in the currency that row moved in; a yuk carries no money of
    its own, so it reports kg and the template prints no figure for it."""
    events = []
    contracts = partner.contracts.prefetch_related("lines__shipment_lines",
                                                   "supplier_payments").all()
    for contract in contracts:
        events.append({
            "date": contract.created, "kind": "kelishuv", "label": "Kelishuv",
            "detail": f"{contract.code} · {contract.brand_summary}",
            "total": contract.total_value, "total_uzs": contract.total_value_uzs,
            "currency": contract.currency})
        for payment in contract.supplier_payments.all():
            # What the HAMKOR received, not what left the kassa. This page is about
            # our standing with them, so its rows have to reconcile with the qolgan
            # to'lov printed above: kelishuv value less these figures IS that number.
            # The vositachi cut and the bank's foiz ride on top and are money spent
            # on the transfer rather than paid to the hamkor, so they are named in
            # the detail instead of quietly inflating the column.
            extra = payment.total_out - payment.amount
            events.append({
                "date": payment.date, "kind": "tolov", "label": "To'lov",
                "detail": f"{contract.code} · {payment.get_method_display()}"
                          + (f" · ustiga {usd(extra)} xarajat" if extra else ""),
                "total": payment.amount, "total_uzs": payment.amount_uzs,
                "currency": payment.currency})
    for shipment in (Shipment.objects.filter(contract__partner=partner)
                     .select_related("contract").prefetch_related("lines")):
        # Sent is the date the hamkor acted on; a load still being loaded has none
        # yet, so it falls back to when the row was created rather than dropping off
        # the timeline entirely.
        events.append({
            "date": shipment.sent or shipment.created_at.date(),
            "kind": "yuk", "label": "Yuk",
            "detail": f"#{shipment.pk} · {shipment.contract.code} · "
                      f"{shipment.brand_summary} · {_kg(shipment.kg)} kg",
            "total": None, "total_uzs": None, "currency": shipment.contract.currency})
    events.sort(key=lambda e: (e["date"], e["label"]), reverse=True)
    return events


@role_required(User.Role.ADMIN)
def partner_detail(request, pk):
    """One hamkor's page: what we still owe them, and everything that has passed
    between us."""
    partner = get_object_or_404(Partner, pk=pk)
    return render(request, "crm/partner_detail.html", {
        "partner": partner,
        "payable": payable_by_currency(partner.contracts.all()),
        "history": _partner_history(partner)})


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
        # "When did they last take goods, and when did they last pay" — the two
        # dates that say whether a mijoz is live or has gone quiet, which the
        # balance beside them cannot: a standing qarz looks the same on the day it
        # was run up and a year later. Taken off the prefetch the balance already
        # walks rather than annotated, so the pair costs no extra query.
        customer.last_sale = max((s.date for s in customer.sales.all()), default=None)
        customer.last_payment = max((p.date for p in customer.customer_payments.all()),
                                    default=None)
    return render(request, "crm/customer_list.html", {"page": page, "q": q})


@role_required(User.Role.ADMIN)
def customer_create(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            with transaction.atomic():
                customer = form.save()
                avans = form.opening_payment(customer, request.user)
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Mijoz", customer.pk, f"Yangi mijoz: {customer.name}"
            )
            if avans:
                # Booked as its own line: money arriving is the fact an audit reader
                # looks for, and it did not arrive because a mijoz was named.
                AuditLog.record(
                    request.user, AuditLog.Action.PAYMENT, "Mijoz to'lovi", avans.pk,
                    f"Boshlang'ich avans: {avans.amount}$ · {customer.name}")
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
def customer_avans(request, pk):
    """Book an avans for a mijoz who already exists — the opening avans on the
    create form, reachable on every visit after the first."""
    customer = get_object_or_404(Customer, pk=pk)
    form = CustomerAvansForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            with transaction.atomic():
                rows = form.payments(customer, request.user)
                # An avans handed over by a mijoz who already owes us is not money we
                # are holding — it belongs to the sotuv that has been waiting for it.
                # The sweep places it oldest-sotuv-first like any other to'lov, and
                # only what is genuinely left over stays an avans.
                reconcile_customer_allocations(customer)
            AuditLog.record(
                request.user, AuditLog.Action.PAYMENT, "Mijoz to'lovi", rows[0].pk,
                f"Avans: {_money_line([(r.currency, r.amount_uzs if r.currency == Currency.UZS else r.amount) for r in rows])} · "
                f"mijoz {customer.name}")
            messages.success(request, "Avans qo'shildi")
            return form_reload(request, reverse("customer_list"))
        return form_response(request, form, f"Avans · {customer.name}", invalid=True)
    return form_response(request, form, f"Avans · {customer.name}")


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


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def contract_list(request):
    """Kelishuvlar: search plus hamkor / to'lov holati / yetkazish / muddat filters.
    Hamkor narrows in SQL; the rest read computed properties (debt, remaining_kg),
    so they run in Python over prefetched rows — the loads and the payments come in
    one query each instead of two per kelishuv.

    A tarjimon reads this page and can do nothing else on it: `contract_create`,
    `contract_edit` and `contract_delete` are all admin-only and each says so itself,
    so the template hiding those buttons is the courtesy, not the lock. The money
    columns are hidden from them too — the same rule the yuklar list already follows
    (tests/test_shipments.py::test_translator_sees_no_price_on_loads). What is left is
    the kelishuv as a logistics document: which marka, how many kg agreed, how many
    still to come."""
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


# Sorted in SQL, like the hamkor page — every key here is a real column.
CUSTOMER_PAYMENT_SORTS = [
    ("-date", "Sana — yangi avval", ["-date", "-created_at"]),
    ("date", "Sana — eski avval", ["date", "created_at"]),
    ("-amount", "Summa — kattadan", ["-amount", "-date"]),
    ("amount", "Summa — kichikdan", ["amount", "-date"]),
    ("customer", "Mijoz — A-Z", ["customer__name", "-date"]),
]
CUSTOMER_PAYMENT_SORT_DEFAULT = "-date"


@role_required(User.Role.ADMIN)
def customer_payment_list(request):
    q = request.GET.get("q", "").strip()
    customer_id = request.GET.get("customer", "").strip()
    method = request.GET.get("method", "").strip()
    date_from = _date_param(request, "date_from")
    date_to = _date_param(request, "date_to")
    sort = request.GET.get("sort", "").strip()
    if sort not in {key for key, *_ in CUSTOMER_PAYMENT_SORTS}:
        sort = CUSTOMER_PAYMENT_SORT_DEFAULT

    payments = CustomerPayment.objects.select_related("customer")
    if q:
        payments = payments.filter(Q(customer__name__icontains=q) | Q(note__icontains=q))
    if customer_id.isdigit():
        payments = payments.filter(customer_id=int(customer_id))
    if method in dict(PayMethod.choices):
        payments = payments.filter(method=method)
    if date_from:
        payments = payments.filter(date__gte=date_from)
    if date_to:
        payments = payments.filter(date__lte=date_to)

    ordering = next(e[2] for e in CUSTOMER_PAYMENT_SORTS if e[0] == sort)
    payments = payments.order_by(*ordering)

    # No per-currency totals here, unlike the hamkor page: this screen is read to find
    # a to'lov, not to add a column up. The queryset goes to the Paginator unevaluated
    # so only the 20 rows on the page are fetched.
    page = Paginator(payments, 20).get_page(request.GET.get("page"))
    return render(request, "crm/customer_payment_list.html", {
        "page": page, "q": q, "customer_id": customer_id, "method": method,
        "date_from": date_from, "date_to": date_to, "sort": sort,
        "sort_options": [(key, label) for key, label, *_ in CUSTOMER_PAYMENT_SORTS],
        "methods": PayMethod.choices, "customers": Customer.objects.all(),
        "has_filters": bool(q or customer_id or method or date_from or date_to),
    })


@role_required(User.Role.ADMIN)
def customer_payment_create(request):
    """Several to'lovlar at once: a mijoz settling 10 000$ commonly hands over part
    in dollars and the rest in so'm, sometimes naqd against perechisleniya. Each
    arrival is its own row — its own valyuta, kurs, usul and foiz — because they
    convert and charge differently; the mijoz and the sana are shared."""
    target = CustomerPaymentTargetForm(request.POST or None,
                                       initial={"customer": request.GET.get("customer")})
    # Read straight off POST rather than from cleaned_data: the rows have to be
    # BUILT knowing which qarz is being collected, because that is what decides
    # whether each one has to ask for a kurs, and the header is not clean yet.
    rows = CustomerPaymentFormSet(
        request.POST or None, queryset=CustomerPayment.objects.none(),
        form_kwargs={"target_currency": (request.POST.get("debt_currency") or "").strip()})

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
                    # Which qarz this settlement is collecting rides on every row, so
                    # the later sweeps — which run long after this modal closed — put
                    # the money back where the operator aimed it.
                    payment.target_currency = target.cleaned_data["debt_currency"]
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
    adds the arrived ones and paginates, since that set only grows.

    `?qr=bor|yoq` narrows to the loads whose driver carries a QR kod, or the ones
    whose driver does not. Server-side rather than a third client-side tab: it has to
    combine with the holat tabs instead of replacing whichever one is active, it has
    to reach past the Hammasi pager (a client filter only ever sees the rows on this
    page), and narrowing the queryset is what makes the tab counts beside it say how
    many of THOSE loads sit in each holat.

    The page opens unfiltered. `qr_waiting_count` is what stands in for a default:
    the loads whose planned QR day has come and gone with no kod, counted on the QR
    yo'q pill, so the ones worth chasing announce themselves without the list having
    to hide anything to say so."""
    q = request.GET.get("q", "").strip()
    show_all = request.GET.get("all") == "1"
    # Anything else in the URL means no QR filter at all — a typo should show every
    # yuk, not silently drop half of them.
    qr = request.GET.get("qr", "")
    if qr not in ("bor", "yoq"):
        qr = ""
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
    # Counted before the QR filter narrows anything, so the number on the pill keeps
    # meaning "waiting, among the yuklar you are looking at" — standing on QR bor
    # must not zero out the count of the loads you are not looking at.
    qr_waiting_count = shipments.filter(
        qr_given__isnull=True, qr_date__isnull=False,
        qr_date__lt=timezone.localdate()).count()
    if qr:
        # The date is what says the kod was handed over, so its absence is "yo'q".
        shipments = shipments.filter(qr_given__isnull=qr == "yoq")
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
        "q": q, "qr": qr, "qr_waiting_count": qr_waiting_count,
        "default_tab": default_tab, "show_all": show_all, "page": page,
    })


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_done_list(request):
    """Kept so old links and bookmarks still land somewhere: Yakunlangan is now
    the Hammasi view, which lists arrived loads alongside the moving ones."""
    return redirect(f"{reverse('shipment_list')}?all=1")


@role_required(User.Role.ADMIN, User.Role.SKLADCHI)
def ombor(request):
    """Ombor by MARKA, one row per granula. The same marka can arrive on several
    lots at different landed costs; showing those as separate rows made the stock
    look like different products, so they merge here and the lots live inside the
    row — each still sellable on its own (a lot's own tan narx follows the sale).

    A skladchi reads this page: the marka, its lots and the kg. The tannarx column
    and every Sotish/Bron button are drawn only for an admin — see the template."""
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
                                   "reserved": Decimal("0")}
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
        # Who has asked for this marka, oldest first. A bron holds nothing back, so
        # `on_hand` is both the physical count and what may be sold; `reserved` says
        # how much of it is spoken for and `short` when more is promised than has
        # landed. Both are there to be read before selling, not to refuse the sotuv.
        g["brons"] = bron_queue(g["brand"])
        g["reserved"] = sum((r.remaining_kg for r in g["brons"]), Decimal("0"))
        g["short"] = max(g["reserved"] - g["on_hand"], Decimal("0"))
        for bron in g["brons"]:
            # Every open bron can be served, not just the first — and each one only
            # up to what is still owed on it or still on the shelf.
            bron.servable_kg = min(bron.remaining_kg, g["on_hand"])

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
                shipment = form.save(commit=False)
                # The holat decides WHETHER a yuk has arrived; the date field only
                # says WHEN. Same rule shipment_set_status follows, and this screen
                # did not follow it at all: setting the holat to arrival here left
                # `arrived` empty, so the load claimed to have landed and never
                # appeared in the ombor — `arrived_lots` filters on the date, not the
                # status. Moving away from arrival left the date behind, which kept a
                # load on the shelf after it went back on the road.
                #
                # `or` and not a plain assignment: a date the operator just typed is
                # the whole point of the field, so it wins over today's date.
                if shipment.status.is_arrival:
                    shipment.arrived = shipment.arrived or timezone.localdate()
                else:
                    shipment.arrived = None
                shipment.save()
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


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_driver_edit(request, pk):
    """Haydovchi va konteyner — the one edit a tarjimon may make to a yuk.

    A view of its own rather than a mode of `shipment_edit`, because the limit is
    ShipmentDriverForm's field list and nothing else. A tarjimon can post whatever
    they like to this URL; only the four fields the form declares are bound, so the
    contract, the status, the dates and the logist are unreachable from here no matter
    what the request body says. Admins get the same screen as a quick way to correct a
    plate without opening the full dispatch modal.

    Everything else about a yuk — its kelishuv, its mahsulotlar, its holat, its
    muddat, its bosqichlar, its xarajatlari — is admin-only, and each of those views
    enforces that itself."""
    shipment = get_object_or_404(Shipment, pk=pk)
    form = ShipmentDriverForm(request.POST or None, instance=shipment)
    title = f"Yuk #{shipment.pk} — haydovchi va konteyner"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Yuk", shipment.pk,
                f"Haydovchi/konteyner yangilandi: "
                f"{shipment.driver_name or '—'} · {shipment.transport or '—'} · "
                f"{shipment.container or '—'}",
            )
            messages.success(request, "Haydovchi va konteyner yangilandi")
            # Reload in place: this modal opens from the list AND from the detail
            # page, and a redirect to the list would throw away wherever they were.
            return form_reload(request, reverse("shipment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


# --- Route legs (Yo'nalish bosqichlari). Admin-only: a leg rewrites the route and
#     the current transport, which is more of a yuk than a tarjimon may change. ---

@role_required(User.Role.ADMIN)
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


@role_required(User.Role.ADMIN)
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


@role_required(User.Role.ADMIN)
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
@role_required(User.Role.ADMIN)
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


@role_required(User.Role.ADMIN)
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
@role_required(User.Role.ADMIN)
def shipment_set_status(request, pk):
    # Admin-only outright. A tarjimon used to move a yuk between non-arrival statuses;
    # the holat drives the whole board — what counts as in transit, what is overdue,
    # when a bron becomes sellable — which is more than driver detail.
    shipment = get_object_or_404(Shipment.objects.select_related("status"), pk=pk)
    status = get_object_or_404(ShipmentStatus, pk=request.POST.get("status"))
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


def _qr_side_url(request, qr):
    """The yuklar list the load has just moved to, keeping whatever else was on the
    screen it was marked from.

    Rebuilt from the referring URL rather than hard-coded, so marking a kod while
    searching for a plate, or while standing in Hammasi, does not silently throw
    that away and land the operator somewhere they did not ask to be. `page` is
    dropped: page 3 of the old side means nothing on the new one."""
    params = QueryDict(urlparse(request.META.get("HTTP_REFERER") or "").query,
                       mutable=True)
    params["qr"] = qr
    params.pop("page", None)
    return f"{reverse('shipment_list')}?{params.urlencode()}"


@role_required(User.Role.ADMIN)
def shipment_set_qr(request, pk):
    """Mark this load's QR kod handed over — or take the mark back.

    A modal asking for the date, not the one-press toggle this used to be. The press
    wrote today, which is only right when the mark is entered on the day; entered on
    Monday for a kod handed over on Friday it recorded a date that was simply wrong,
    and `qr_given` is read as the fact of when it happened. Nothing else knows the
    real date, so it has to be asked.

    Still the same button and still reversible: submitting the date empty clears the
    mark, which is what a mis-click on a row of near-identical trucks needs. The
    field starts on whatever is already stored, or today for a load being marked for
    the first time — the common case is still "this happened today"."""
    shipment = get_object_or_404(Shipment, pk=pk)
    title = f"Yuk #{shipment.pk} — QR kod"
    initial = {"qr_given": shipment.qr_given or timezone.localdate()}
    form = ShipmentQrForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            given = form.cleaned_data["qr_given"]
            shipment.qr_given = given
            shipment.save(update_fields=["qr_given"])
            AuditLog.record(request.user, AuditLog.Action.UPDATE, "Yuk", shipment.pk,
                            f"QR kod berildi: {given}" if given else "QR kod bekor qilindi")
            messages.success(request, "QR kod berildi" if given else "QR kod belgisi olindi")
            # Marked from the list, the load follows its mark: the page swaps to the
            # side it now belongs to. Without this it simply vanished from under the
            # cursor — the list opens on QR bor, so a load marked from QR yo'q left
            # that set on reload with nothing to say where it went.
            #
            # Marked from a yuk's own page there is no side to move to, so that one
            # reloads in place instead of throwing the operator out to the list.
            referer = urlparse(request.META.get("HTTP_REFERER") or "").path
            if referer == reverse("shipment_list"):
                return form_success(request, _qr_side_url(request, "bor" if given else "yoq"))
            return form_reload(request, reverse("shipment_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


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


def _sale_groups(sales):
    """Fold the rows entered together into one block each — one row on Sotuvlar —
    keeping the list's order.

    A submission's rows sit next to each other in that order already — same sana,
    consecutive created_at — so a single walk over the page is enough and no row
    moves to reach its neighbours. Inside a block they go back into entry order:
    the page reads newest sotuv first, but the mahsulotlar of one trip to the
    counter were typed oldest first and should be read that way."""
    blocks = []
    for sale in sales:
        block = blocks[-1] if blocks else None
        if block is None or sale.group is None or block["group"] != sale.group:
            blocks.append({"group": sale.group, "sales": [sale]})
        else:
            block["sales"].append(sale)
    for block in blocks:
        rows = sorted(block["sales"], key=lambda s: s.pk)
        block["sales"] = rows
        block["first"] = rows[0]
        block["count"] = len(rows)
        block["kg"] = sum((s.kg for s in rows), Decimal("0"))
        # What the sotuv means as a whole. The per-kg figures stay per mahsulot in
        # their own column, but these are only ever read summed — the mijoz owes one
        # qarz, not one per lot the granula happened to come off.
        block["total"] = sum((s.total for s in rows), Decimal("0"))
        block["total_uzs"] = sum((s.total_uzs for s in rows), Decimal("0"))
        block["profit"] = sum((s.profit for s in rows), Decimal("0"))
        block["profit_uzs"] = sum((s.profit_uzs for s in rows), Decimal("0"))
        block["remaining"] = sum((s.remaining for s in rows), Decimal("0"))
        block["remaining_uzs"] = sum((s.remaining_uzs for s in rows), Decimal("0"))
        # Measured per row in each row's own currency, the way `is_paid` does it —
        # summing the two sides first would let a so'm sotuv's kurs drift decide it.
        block["owing"] = any(s.remaining_own > 0 for s in rows)
    return blocks


@role_required(User.Role.ADMIN, User.Role.SKLADCHI)
def sale_list(request):
    """A row per SOTUV, not per lot: the mahsulot columns stack inside their cells
    and jami/foyda/qarz are the sotuv's own totals.

    A skladchi reads this page for what left the shelf and to whom — the narx, jami,
    foyda and qarz columns are drawn only for an admin, and so is every action. The
    sotuv's own page stays admin-only, so their mijoz cell is not a link.

    Paging still counts rows, so a sotuv straddling the boundary shows the lots that
    fall on each page. Its figures are the ones on that page too — a total that
    counted rows the page is not showing would be the worse of the two lies."""
    q = request.GET.get("q", "").strip()
    sales = Sale.objects.select_related("customer", "line__contract_line", "line__shipment__contract__partner")
    if q:
        filters = (Q(customer__name__icontains=q) | Q(line__contract_line__brand__icontains=q))
        if q.isdigit():
            filters |= Q(line__shipment_id=int(q))
        sales = sales.filter(filters)
    page = Paginator(sales, 20).get_page(request.GET.get("page"))
    return render(request, "crm/sale_list.html",
                  {"page": page, "groups": _sale_groups(page.object_list), "q": q})


def _sale_form_response(request, form, lines, title, invalid=False):
    return form_response(request, form, title, invalid=invalid,
                         extra_context={"lines": lines, "lines_legend": "Mahsulotlar"})


@role_required(User.Role.ADMIN)
def sale_create(request):
    """Sale by brand, and by SEVERAL brands at once: one trip to the counter is one
    sotuv, however many markalar the mijoz took, rather than one modal per product.

    Each Mahsulot row's kg is consumed from the oldest arrived lots of that marka
    first (FIFO), one Sale row per lot slice so each slice keeps its own lot's landed
    cost. A row therefore becomes as many Sale objects as it takes lots to fill, and
    the whole submission becomes the sum of those.

    `?lot=` (opening one lot from inside a marka in the ombor) sells from THAT lot
    instead, and stays single-product — see sale_create_lot."""
    lot_id = request.GET.get("lot") or request.POST.get("lot")
    if lot_id and str(lot_id).isdigit():
        return sale_create_lot(request, int(lot_id))

    initial, row = {}, {}
    brand = (request.GET.get("brand") or "").strip()   # marka row's Sotish shortcut
    if brand:
        row["brand"] = brand
    customer_id = request.GET.get("customer")
    if customer_id and customer_id.isdigit():
        initial["customer"] = int(customer_id)
    # Serving a bron from Bronlar or Ombor: the narx and the valyuta were agreed
    # when the bron was struck and must not have to be retyped from memory —
    # retyping is how an agreed price quietly becomes a different one.
    currency = (request.GET.get("currency") or "").strip()
    if currency in dict(Currency.choices):
        initial["currency"] = currency
    price = (request.GET.get("price") or "").strip()
    if price:
        try:
            row["price"] = Decimal(price)
        except (ArithmeticError, ValueError):
            pass
    form = SaleCreateForm(request.POST or None, initial=initial)
    # The marka and its narx are a ROW now, so a shortcut that named one prefills the
    # first row rather than the header.
    lines = SaleLineFormSet(request.POST or None, prefix="lines",
                            initial=[row] if row else None)
    if request.method == "POST":
        if form.is_valid() and lines.is_valid():
            data = form.cleaned_data
            # One deal, one currency, one kurs — held on the header and applied to
            # every row, so a sotuv can never end up half in dollars.
            currency, rate = data["currency"], data["exchange_rate"]
            # One submission, one id on every row it produces — the markalar and the
            # FIFO slices under them — so Sotuvlar can band them back together as the
            # one trip to the counter they were.
            group = uuid4()
            slices, sold = [], []
            with transaction.atomic():
                for line in lines.rows():
                    take_from = line.cleaned_data
                    # The same conversion PriceEntryFormMixin does, at the header's
                    # kurs. Four decimals: rounding a $/kg to cents would move a
                    # 24-tonne lot by dollars.
                    usd, uzs = convert_pair(take_from["price"], currency, rate, "0.0001")
                    remaining = take_from["kg"]
                    for lot in fifo_lots(take_from["brand"]):
                        if remaining <= 0:
                            break
                        take = min(lot.available_kg, remaining)
                        sale = Sale.objects.create(
                            customer=data["customer"], line=lot, kg=take,
                            # every FIFO slice inherits the one narx agreed for that
                            # marka, in the currency the sotuv was agreed in
                            price=usd, price_uzs=uzs,
                            currency=currency, exchange_rate=rate, group=group,
                            date=data["date"], debt_deadline=data["debt_deadline"],
                            note=data["note"], created_by=request.user,
                        )
                        slices.append(sale)
                        remaining -= take
                        # Serving this mijoz normally makes their own promise smaller,
                        # whichever lot the granula came off — per slice and in order,
                        # so the bron falls by exactly what was sold. Unticked, the
                        # sotuv is something else they bought and the booking stands.
                        if data.get("draw_from_bron"):
                            draw_down_bron(sale)
                    sold.append(f"{take_from['kg']} kg {take_from['brand']}")
            AuditLog.record(
                request.user, AuditLog.Action.CREATE, "Sotuv", slices[0].pk if slices else 0,
                f"Yangi sotuv (FIFO): {', '.join(sold)} · "
                f"{data['customer'].name} · {len(slices)} lot",
            )
            for sale in slices:  # a pre-existing advance auto-applies, oldest slice first
                apply_customer_advance(sale)
            # Says what actually happened: several markalar, or one split across
            # lots, or the ordinary single row.
            if len(sold) > 1:
                note = f"Sotuv qo'shildi ({len(sold)} mahsulot, {len(slices)} lotdan)"
            elif len(slices) > 1:
                note = f"Sotuv qo'shildi ({len(slices)} lotdan)"
            else:
                note = "Sotuv qo'shildi"
            messages.success(request, note)
            return form_success(request, reverse("sale_list"))
        return _sale_form_response(request, form, lines, "Yangi sotuv", invalid=True)
    return _sale_form_response(request, form, lines, "Yangi sotuv")


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
            if data.get("draw_from_bron"):
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
            # Whether this sotuv came out of a bron is decided when it is created and
            # must survive an edit. Re-drawing unconditionally would quietly convert a
            # sotuv booked alongside a bron into one taken from it, the first time
            # anybody corrected a kg.
            was_from_bron = release_bron(sale) > 0
            sale = form.save()
            if sale.reservation_id:
                sale.reservation = None
                sale.save(update_fields=["reservation"])
            if was_from_bron:
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
    # The rest of what the mijoz took in the same go, summed — the page otherwise
    # shows one marka off one lot and says nothing about the trip it belonged to.
    group = sale.group_sales
    return render(request, "crm/sale_detail.html", {
        "sale": sale, "group": group,
        "group_kg": sum((s.kg for s in group), Decimal("0")),
        "group_total": sum((s.total for s in group), Decimal("0")),
        "group_total_uzs": sum((s.total_uzs for s in group), Decimal("0")),
    })


#: Holat tabs, in the order a bron moves through them. Faol leads because an open
#: bron is the only kind there is anything left to do about.
RESERVATION_STATUS_LABELS = [
    ("active", "Faol"), ("converted", "Sotuvga aylandi"),
    ("closed", "Tugatildi"), ("cancelled", "Bekor qilindi"), ("", "Hammasi"),
]

# Sorted in Python: jami is kg × narx and lot state reads through two relations,
# neither of which is a column. Each entry is (key, label, sort key, reverse).
RESERVATION_SORTS = [
    # Navbat first: who asked for each marka and in what order is the thing the
    # screen exists to show, so it is the default unless you ask for another sort.
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
    # "ready" = open with some of its marka on the shelf; "waiting" = open with an
    # empty ombor, which is now the only thing that can hold a hand-over up.
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
    # What is on the shelf for that marka right now, plus the position in the
    # booking order. Stock is the only thing that decides whether a bron can be
    # filled today; the position is shown so the operator can see who asked first,
    # and a bron behind another is still servable. Computed once per marka rather
    # than per row: the walk is the same for every bron of a marka.
    shelf, queues = {}, {}
    for brand in {r.brand for r in rows}:
        shelf[brand] = brand_on_hand_kg(brand)
        queues[brand] = [b.pk for b in bron_queue(brand)]
    for row in rows:
        order = queues.get(row.brand, [])
        row.queue_pos = order.index(row.pk) + 1 if row.pk in order else None
        row.brand_on_hand = shelf.get(row.brand, Decimal("0"))
        row.servable_kg = (min(row.remaining_kg, row.brand_on_hand)
                           if row.is_open else Decimal("0"))
        # Only who is ahead in the booking order — a label, not a block.
        row.ahead_of = (
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


@role_required(User.Role.ADMIN)
def reservation_close(request, pk):
    """Close a bron the mijoz is done with: they took what they took, and the rest
    is released.

    Not the same act as cancelling. A cancelled bron never happened; a closed one
    was served in part and ended by agreement, and reading back months later which
    of the two it was is the whole reason for the second status. Nothing is undone
    either way — the sotuvlar already drawn from it stand, and only the kg still
    promised go back on the shelf, which happens by itself because `is_open` and
    `brand_reserved_kg` both stop counting a bron that is no longer ACTIVE."""
    reservation = get_object_or_404(Reservation, pk=pk)
    if not reservation.is_open:
        messages.error(request, "Bu bron allaqachon yopilgan")
        return form_reload(request, reverse("reservation_list"))
    if request.method == "POST":
        freed = reservation.remaining_kg
        reservation.status = Reservation.Status.CLOSED
        reservation.save(update_fields=["status"])
        AuditLog.record(
            request.user, AuditLog.Action.STATUS, "Bron", reservation.pk,
            f"Bron tugatildi: {_kg(freed)} kg qaytdi · {reservation.customer.name}")
        messages.success(request, f"Bron tugatildi — {_kg(freed)} kg omborga qaytdi")
        return form_reload(request, reverse("reservation_list"))
    return render_confirm(
        request,
        "Bronni tugatish",
        f"“{reservation.customer.name}” broni tugatiladi. Berilgan "
        f"{_kg(reservation.fulfilled_kg)} kg o'z holicha qoladi, qolgan "
        f"{_kg(reservation.remaining_kg)} kg omborga qaytadi.",
        "Ha, tugatish",
        cancel_url_name="reservation_list",
    )


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


def _customer_history(customer):
    """Everything that has passed between us and one mijoz, newest first.

    One timeline rather than four tables, because the question the page answers —
    "what has gone on with this mijoz" — is chronological, and a sotuv followed by
    the to'lov that cleared it only reads as a pair when they sit next to each
    other. Each row is drawn in the currency that row actually moved in; nothing is
    converted, so a dollar sotuv and a so'm to'lov stay two separate facts.

    `total` is None on a bron with no narx agreed yet — that is a real state, and
    the template prints the kg alone rather than inventing a figure."""
    events = []
    for sale in customer.sales.select_related("line").all():
        events.append({
            "date": sale.date, "kind": "sotuv", "label": "Sotuv",
            "detail": f"{sale.line.brand} · {_kg(sale.kg)} kg",
            "total": sale.total, "total_uzs": sale.total_uzs,
            "currency": sale.currency})
    for payment in customer.customer_payments.all():
        events.append({
            "date": payment.date, "kind": "tolov", "label": "To'lov",
            "detail": payment.get_method_display(),
            "total": payment.amount, "total_uzs": payment.amount_uzs,
            "currency": payment.currency})
    for ret in (Return.objects.filter(sale__customer=customer)
                .select_related("sale__line")):
        events.append({
            "date": ret.date, "kind": "qaytarish", "label": "Qaytarish",
            "detail": f"{ret.sale.line.brand} · {_kg(ret.kg)} kg",
            "total": ret.amount, "total_uzs": ret.amount_uzs,
            "currency": ret.currency})
    for bron in customer.reservations.all():
        events.append({
            "date": bron.created_at.date(), "kind": "bron",
            "label": f"Bron · {bron.get_status_display()}",
            "detail": f"{bron.brand} · {_kg(bron.kg)} kg",
            "total": bron.price, "total_uzs": bron.price_uzs,
            "currency": bron.currency, "per_kg": True})
    # Newest first, and a stable tie-break so two events on one day do not swap
    # places between page loads.
    events.sort(key=lambda e: (e["date"], e["label"]), reverse=True)
    return events


@role_required(User.Role.ADMIN)
def debt_customer(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    sales = [s for s in customer.sales.select_related("line__contract_line")
             .prefetch_related("allocations").all() if s.remaining_own > 0]
    return render(request, "crm/debt_customer.html", {
        "customer": customer, "sales": sales,
        "history": _customer_history(customer),
        "positions": customer_balance_by_currency(customer)})


#: How the outflow rows collapse into waterfall steps. Bojxona and transport carry
#: the load on their own (88% of yuk spend in July), so they get a bar each and the
#: rest share one — six bars a reader can hold in their head beats twelve they cannot.
WATERFALL_EXPENSE_GROUPS = [
    ("customs", "Bojxona"),
    ("transport", "Transport"),
]
WATERFALL_EXPENSE_OTHER = "Boshqa xarajatlar"


def _typed_decimal(raw):
    """A number as the operator's field hands it over, or None if it is not one.

    The money inputs group thousands and take a comma for the decimal point, and
    their JS normally strips that back before submit. This does the same server-side
    so a hand-typed "12 000,5" is a number here rather than a confusing refusal."""
    text = (raw or "").strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except (ValueError, ArithmeticError):
        return None


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

    # Kapital carries its own direction, so it is summed rather than added or
    # subtracted by the caller — a ta'sischi who took money out is a negative kirim.
    def _kapital(qs):
        return sum((k.signed_amount for k in qs), Decimal("0"))

    def _kapital_uzs(qs):
        return sum((k.signed_amount_uzs for k in qs), Decimal("0"))

    # Joriy holat (all-time, filter-independent): money physically in the till.
    # ShipmentExpense.total_out is already zero for a logist-funded row, so the
    # whole queryset can be summed: the money left when we topped the logist up,
    # and LogistPayment below is where that shows.
    #
    # The converted pair is still what the Oqim waterfall closes on — it has to be,
    # since the waterfall is a single running line and cannot be two. The tiles
    # above it read the per-currency figures instead; see the `split` note there.
    cash_total = (_in(CustomerPayment.objects.all())
                  + _kapital(Kapital.objects.all())
                  - _out(SupplierPayment.objects.all())
                  - _out(ShipmentExpense.objects.all())
                  - _out(LogistPayment.objects.all())
                  - _out(CustomsPayment.objects.all()))
    cash_total_uzs = (_in_uzs(CustomerPayment.objects.all())
                      + _kapital_uzs(Kapital.objects.all())
                      - _out_uzs(SupplierPayment.objects.all())
                      - _out_uzs(ShipmentExpense.objects.all())
                      - _out_uzs(LogistPayment.objects.all())
                      - _out_uzs(CustomsPayment.objects.all()))

    # Not all of the till is ours. Money a mijoz has handed over that sits on no
    # sotuv is held, not earned — cancel the order and it goes back out.
    advance, advance_uzs = customer_advance_total()
    own_cash, own_cash_uzs = cash_total - advance, cash_total_uzs - advance_uzs

    # The same two facts as heaps rather than as one sum converted twice: so'm in
    # the safe is not dollars in the safe.
    cash_split = kassa_cash_by_currency()
    advance_split = customer_advance_by_currency()
    kapital_split = kapital_total_by_currency()

    # Pozitsiya: what the cash figure MEANS. Cash alone reads as a disaster while
    # the money is sitting in trucks and mijoz qarzi; these are the lines that say
    # where it went. Current-state, so the date filter does not touch them.
    receivable, debtors = customer_receivable_by_currency()
    hamkor = partner_positions_by_currency()
    logist_held, logist_held_uzs, logist_owed, logist_owed_uzs = logist_positions()
    customs_held, customs_owed = customs_positions()
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
        # Two different "not all of this is what it looks like" facts, so both are
        # named when both apply: an avans is money we are holding for somebody else,
        # kapital is money that was put in rather than earned.
        {"label": "Kassada", "split": cash_split,
         "note": "hozir qo'lda va hisobda turgan pul", "tone": "cash",
         "meta": " · ".join(
             part for part in (
                 (f"shundan mijoz avansi {_money_line(advance_split)}"
                  if advance_split else ""),
                 (f"shundan kapital {_money_line(kapital_split)}"
                  if kapital_split else ""))
             if part),
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
        # A split rather than the converted pair the logist tile above uses. A
        # logist's hisob is one heap restated; bojxona money genuinely moves in both
        # currencies, so it is two — the same reason Kassada and the hamkor tiles
        # carry one.
        {"label": "Bojxonada", "split": customs_held,
         "note": "yuklarni rasmiylashtirish uchun oldindan yuborilgan",
         "tone": "in", "meta": "", "url": reverse("customs_list")},
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
            "label": "Logistlarga qarzimiz", "split": None, "amount": logist_owed,
            "amount_uzs": logist_owed_uzs, "tone": "out", "meta": "",
            "note": "o'z pulidan haydovchiga bergani",
            "url": reverse("logist_list") + "?state=owed"})
    # Same rule for the bojxonachi who cleared a truck out of his own pocket because
    # what we sent ran short — usually zero, so it appears only when it is not.
    if customs_owed:
        tiles.append({
            "label": "Bojxonaga qarzimiz", "split": customs_owed,
            "tone": "out", "meta": "", "note": "o'z pulidan yukni rasmiylashtirgani",
            "url": reverse("customs_list") + "?state=owed"})

    # The Kirim ledger asks each row what it settled and whether its kurs was chosen,
    # and both answers are read off the allocations — without the prefetch that is a
    # query per to'lov, then one per slice, for every row on the page.
    cust_pays = _range(CustomerPayment.objects.select_related("customer")
                       .prefetch_related("allocations__sale"))
    sup_pays = _range(SupplierPayment.objects.select_related("contract__partner"))
    expenses = _range(ShipmentExpense.objects.select_related(
        "shipment__contract", "logist", "customs_agent"))
    logist_pays = _range(LogistPayment.objects.select_related("logist"))
    customs_pays = _range(CustomsPayment.objects.select_related("agent", "shipment"))
    kapital_rows = _range(Kapital.objects.all())
    # Split once here rather than per method below: the two directions land on
    # opposite sides of every total, and asking the question twice per PayMethod is
    # where a "+" gets typed for a "−".
    kapital_in = kapital_rows.filter(kind=KapitalKind.IN)
    kapital_out = kapital_rows.filter(kind=KapitalKind.OUT)

    balances = {}
    net_in = net_out = Decimal("0")
    net_in_uzs = net_out_uzs = Decimal("0")
    for value, label in PayMethod.choices:
        m_in = (_in(cust_pays.filter(method=value))
                + _kapital(kapital_in.filter(method=value)))
        m_out = (_out(sup_pays.filter(method=value))
                 + _out(expenses.filter(method=value))
                 + _out(logist_pays.filter(method=value))
                 + _out(customs_pays.filter(method=value))
                 # Already negative, so it is subtracted to land as an outflow.
                 - _kapital(kapital_out.filter(method=value)))
        m_in_uzs = (_in_uzs(cust_pays.filter(method=value))
                    + _kapital_uzs(kapital_in.filter(method=value)))
        m_out_uzs = (_out_uzs(sup_pays.filter(method=value))
                     + _out_uzs(expenses.filter(method=value))
                     + _out_uzs(logist_pays.filter(method=value))
                     + _out_uzs(customs_pays.filter(method=value))
                     - _kapital_uzs(kapital_out.filter(method=value)))
        balances[value] = {"label": label, "in": m_in, "out": m_out,
                           "balance": m_in - m_out, "in_uzs": m_in_uzs,
                           "out_uzs": m_out_uzs, "balance_uzs": m_in_uzs - m_out_uzs}
        net_in += m_in
        net_out += m_out
        net_in_uzs += m_in_uzs
        net_out_uzs += m_out_uzs

    # Kirim ledger: money in — mijoz to'lovlari and ta'sischi kapitali, newest first.
    # Dicts rather than the model objects this list used to hold: two unrelated models
    # share the table now, and a kapital row has no mijoz for the template to ask
    # about. Same shape as the Chiqim rows below, so both ledgers read alike.
    income_rows = []
    for p in cust_pays:
        income_rows.append({
            "kind": "customer", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": p.customer.name,
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "fee_percent": p.fee_percent,
            "amount": p.net_amount, "amount_uzs": p.net_amount_uzs,
            "edit_url": reverse("customer_payment_edit", args=[p.pk]),
            "detail_url": reverse("customer_payment_detail", args=[p.pk]),
        })
    # Only the money the ta'sischi put IN belongs on this side; what they took out is
    # a chiqim row below, so the two never cancel inside one ledger's total.
    for k in kapital_in:
        income_rows.append({
            "kind": "kapital", "pk": k.pk, "date": k.date, "obj": k,
            "crossed": k.crosses_currency,
            "title": "Ta'sischi kapitali" + (f" · {k.note}" if k.note else ""),
            "method_code": k.method, "method": k.get_method_display(),
            "currency": k.currency, "exchange_rate": k.exchange_rate,
            "fee_percent": k.fee_percent,
            "amount": k.net_amount, "amount_uzs": k.net_amount_uzs,
            "edit_url": reverse("kapital_edit", args=[k.pk]),
            "detail_url": "",
        })
    income_rows.sort(key=lambda r: (r["date"], r["pk"]), reverse=True)

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
    for p in customs_pays:
        target = f" · yuk #{p.shipment_id}" if p.shipment_id else ""
        outflow_rows.append({
            "kind": "customs", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": f"Bojxona · {p.agent.name}ga{target}"
                     + (f" · {p.note}" if p.note else ""),
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": p.amount_uzs, "amount": p.amount,
        })
    for p in customs_pays:
        if not p.fee_amount:
            continue
        outflow_rows.append({
            "kind": "fee_customs", "pk": p.pk, "date": p.date, "obj": p,
            "crossed": p.crosses_currency,
            "title": f"Perechisleniya foizi ({p.fee_percent}%) · bojxona {p.agent.name}",
            "method_code": p.method, "method": p.get_method_display(),
            "currency": p.currency, "exchange_rate": p.exchange_rate,
            "amount_uzs": p.fee_amount_uzs, "amount": p.fee_amount,
        })
    # Expenses a holder funded — a logist or a bojxonachi — are deliberately absent
    # from this ledger: the cash they cost left as the top-up above, and listing them
    # here would show the same money going out twice.
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
    # The ta'sischi drawing their own money back out. `net_amount` rather than the
    # signed figure: this ledger prints magnitudes and supplies the minus itself.
    for k in kapital_out:
        outflow_rows.append({
            "kind": "kapital", "pk": k.pk, "date": k.date, "obj": k,
            "crossed": k.crosses_currency,
            "title": "Ta'sischi oldi" + (f" · {k.note}" if k.note else ""),
            "method_code": k.method, "method": k.get_method_display(),
            "currency": k.currency, "exchange_rate": k.exchange_rate,
            "amount_uzs": k.net_amount_uzs, "amount": k.net_amount,
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
    # filter that is zero, which is honest: with no date filter there is no "before".
    # Kapital counts here like any other row that moved the till.
    if date_from:
        prior = (CustomerPayment.objects.filter(date__lt=date_from),
                 SupplierPayment.objects.filter(date__lt=date_from),
                 ShipmentExpense.objects.filter(date__lt=date_from),
                 LogistPayment.objects.filter(date__lt=date_from),
                 CustomsPayment.objects.filter(date__lt=date_from))
        prior_kapital = Kapital.objects.filter(date__lt=date_from)
        opening = (_in(prior[0]) + _kapital(prior_kapital)
                   - sum((_out(q) for q in prior[1:]), Decimal("0")))
        opening_uzs = (_in_uzs(prior[0]) + _kapital_uzs(prior_kapital)
                       - sum((_out_uzs(q) for q in prior[1:]), Decimal("0")))
    else:
        opening = opening_uzs = Decimal("0")

    sup_amount = sum((p.amount for p in sup_pays), Decimal("0"))
    sup_amount_uzs = sum((p.amount_uzs for p in sup_pays), Decimal("0"))
    commission = commission_total(sup_pays)
    commission_uzs = sum((p.commission_amount_uzs for p in sup_pays), Decimal("0"))
    # Outgoing foiz only. A mijoz's perechisleniya foiz never reached the kassa —
    # `net_in` is already net of it — so billing it again here would take the same
    # money out twice and the waterfall would stop landing on the ledger total.
    outgoing = list(sup_pays) + list(expenses) + list(logist_pays) + list(customs_pays)
    fees = sum((r.fee_amount for r in outgoing), Decimal("0"))
    fees_uzs = sum((r.fee_amount_uzs for r in outgoing), Decimal("0"))

    logist_amount = sum((p.amount for p in logist_pays), Decimal("0"))
    logist_amount_uzs = sum((p.amount_uzs for p in logist_pays), Decimal("0"))
    # Its own bar rather than folded into the Bojxona expense group below: that group
    # is what clearing COST, this is what we sent ahead of knowing — and the whole
    # point of the feature is that the two are not the same figure.
    customs_amount = sum((p.amount for p in customs_pays), Decimal("0"))
    customs_amount_uzs = sum((p.amount_uzs for p in customs_pays), Decimal("0"))
    steps = [("Mijozlardan", net_in, net_in_uzs),
             ("Hamkorlarga", -sup_amount, -sup_amount_uzs),
             ("Vositachi ustamasi", -commission, -commission_uzs),
             ("Logistlarga", -logist_amount, -logist_amount_uzs),
             ("Bojxonaga oldindan", -customs_amount, -customs_amount_uzs)]
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
    # `legs` prefetched on this narrow slice only — the table names the transport on
    # the load now, and the wider `shipments` above is walked by figures that never
    # touch a leg.
    late_shipments = [s for s in shipments.filter(arrived__isnull=True, eta__isnull=False)
                      .prefetch_related("legs") if s.is_overdue]
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
        "Yetib kelgan", "QR kod berilgan", "Transport", "Konteyner",
    ]
    rows = (
        [s.pk, s.contract.code, s.contract.partner.name, ln.brand, ln.kg, s.status.name,
         s.sent, s.eta, s.arrived, s.qr_given, s.transport, s.container]
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


def holder_loads(expenses, payments=()):
    """Which yuklar an outside party's money actually went to, newest first.

    The hisob varaqasi below it answers "what moved, and when". This answers "on
    WHAT" — and a row of it is a load, so it carries the facts a load is recognised
    by rather than the ones a transaction is: the kelishuv it belongs to, the marka
    on the truck, the kg, where it has got to. The same leading columns as Yuklar,
    for the same reason that page leads with them.

    `payments` is only ever non-empty for a bojxonachi: a CustomsPayment names the
    yuk it was sent for, so their table can set what went out for a truck against
    what clearing it cost. A LogistPayment names none — a logist's funding is a lump
    against no load — so theirs shows what they paid and nothing to set it against.
    """
    loads = {}

    def row_for(shipment):
        return loads.setdefault(
            shipment.pk, {"shipment": shipment, "paid": [], "sent": []})

    for expense in expenses:
        row_for(expense.shipment)["paid"].append(
            (expense.currency, own_side(expense, expense.amount, expense.amount_uzs)))
    for payment in payments:
        if payment.shipment_id is None:
            continue
        row_for(payment.shipment)["sent"].append(
            (payment.currency,
             own_side(payment, payment.net_amount, payment.net_amount_uzs)))

    rows = []
    for row in loads.values():
        shipment = row["shipment"]
        paid, sent = _by_currency(row["paid"]), _by_currency(row["sent"])
        rows.append({
            "shipment": shipment, "paid": paid, "sent": sent,
            "diff": _by_currency([*sent, *((c, -a) for c, a in paid)]),
            # How far the load has actually got: arrived if it is in, otherwise the
            # day it left. One date rather than three — this table is read to find a
            # load, not to chase its schedule, which the yuk's own page carries.
            "date": shipment.arrived or shipment.sent,
        })
    # Newest first, ON THE DATE THE ROW SHOWS. Sorting by `sent` while the column
    # printed `arrived` put 19.07 above 22.07 above 16.07 — three descending numbers
    # that are not descending, which reads as a table with no order at all.
    #
    # A load with neither date has not left yet, so it sorts to the top: it is the
    # one still to happen, not the oldest thing on the list.
    rows.sort(key=lambda r: (r["date"] or _date.max, r["shipment"].pk), reverse=True)
    return rows


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
    """One logist's history: every top-up in, every driver advance out, and every
    yuk they arranged, newest first, with the running balance the list page shows.

    The loads sit on the same timeline as the money rather than in a table of their
    own, because the question the page answers is "what has this logist been doing
    for us" — and an advance paid out three days after a truck was handed to them
    only reads as one story when the two are next to each other. A yuk moves no
    money of ours on its own, so its Kirim and Chiqim cells stay empty."""
    logist = get_object_or_404(
        Logist.objects.prefetch_related(
            "payments",
            "driver_advances__shipment__contract__partner",
            "driver_advances__shipment__lines",
            "driver_advances__shipment__status"), pk=pk)
    rows = []
    for shipment in (logist.shipments.select_related("contract")
                     .prefetch_related("lines")):
        rows.append({
            "kind": "yuk",
            # Sent is the date they acted on; a load still being put together has
            # none yet, so it falls back to when the row was made rather than
            # dropping off the timeline.
            "date": shipment.sent or shipment.created_at.date(),
            "obj": shipment,
            "title": f"Yuk #{shipment.pk} · {shipment.contract.code} · "
                     f"{shipment.brand_summary} · {_kg(shipment.kg)} kg",
        })
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
    return render(request, "crm/logist_detail.html", {
        "logist": logist, "page": page,
        "loads": holder_loads(logist.driver_advances.all()),
    })


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


# ── Kapital ──────────────────────────────────────────────────────────────────────
#
# The ta'sischi's own money. Admin-only like every other money screen: a tarjimon
# sees the Yuklar list and nothing that moves the kassa.


def _kapital_label(entry):
    """"Kiritildi · 5 000$" — the direction and the figure, which is what every
    message and audit line about a Kapital row needs to say."""
    return f"{entry.get_kind_display()} · {entry.amount}$"


@role_required(User.Role.ADMIN)
def kapital_create(request):
    form = KapitalForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            entry = form.save(commit=False)
            entry.created_by = request.user
            entry.save()
            AuditLog.record(request.user, AuditLog.Action.PAYMENT, "Kapital", entry.pk,
                            f"Kapital: {_kapital_label(entry)}")
            messages.success(request, "Kapital qo'shildi")
            return form_success(request, reverse("kassa"))
        return form_response(request, form, "Kapital", invalid=True)
    return form_response(request, form, "Kapital")


@role_required(User.Role.ADMIN)
def kapital_edit(request, pk):
    entry = get_object_or_404(Kapital, pk=pk)
    form = KapitalForm(request.POST or None, instance=entry)
    title = "Kapitalni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(request.user, AuditLog.Action.UPDATE, "Kapital", entry.pk,
                            f"Kapital tahrirlandi: {_kapital_label(entry)}")
            messages.success(request, "Kapital yangilandi")
            return form_reload(request, reverse("kassa"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def kapital_delete(request, pk):
    entry = get_object_or_404(Kapital, pk=pk)
    if request.method == "POST":
        label = _kapital_label(entry)
        entry.delete()
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Kapital", pk,
                        f"Kapital o'chirildi: {label}")
        messages.success(request, "Kapital o'chirildi")
        return form_reload(request, reverse("kassa"))
    return render_confirm(
        request, "Kapitalni o'chirish",
        f"“{_kapital_label(entry)}” yozuvi o'chiriladi.",
        "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="kassa")


# ── Bojxona ──────────────────────────────────────────────────────────────────────
#
# A bojxonachi holds our money the same way a logist does, with one difference that
# shapes every screen here: the money goes out per load as an ESTIMATE. We send
# ~40 mln so a truck clears and only afterwards learn it cost 37, or 39, or 40. So
# there are two questions, not one — how much is sitting with them (the list), and
# which load has money still unaccounted for (the reconciliation) — and each gets
# its own page rather than one page trying to answer both.


def _payment_label(payment):
    """A bojxona to'lov named the way it was made — "40 000 000 so'm · Bahrom aka".

    Its OWN side, never the stored twin: printing the so'm column of a $4 000 to'lov
    would put a figure in the audit trail that nobody typed, at a kurs the reader
    has no way to see."""
    return (f"{_money_line([(payment.currency, own_side(payment, payment.amount, payment.amount_uzs))])}"
            f" · {payment.agent.name}")


@role_required(User.Role.ADMIN)
def customs_list(request):
    q = request.GET.get("q", "").strip()
    state = request.GET.get("state", "").strip()
    agents = CustomsAgent.objects.prefetch_related("payments", "expenses__shipment")
    if q:
        agents = agents.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    rows = list(agents)
    # Asked of the heaps, not of one converted figure: somebody holding leftover
    # so'm while a dollar clearing ran short is both a holder and a creditor, and
    # they belong in both lists rather than in whichever one the netted number fell
    # on. "Settled" is the genuinely empty case — every heap at zero.
    for agent in rows:
        agent.held = agent.held_by_currency()
        agent.owed = agent.owed_by_currency()
    if state == "holding":
        rows = [x for x in rows if x.held]
    elif state == "owed":
        rows = [x for x in rows if x.owed]
    elif state == "settled":
        rows = [x for x in rows if not x.held and not x.owed]

    held, owed = customs_positions()
    open_diff, open_loads = customs_open_position()
    page = Paginator(rows, 20).get_page(request.GET.get("page"))
    return render(request, "crm/customs_list.html", {
        "page": page, "q": q, "state": state,
        "held": held, "owed": owed,
        "open_diff": open_diff, "open_loads": open_loads,
        "has_filters": bool(state),
    })


def customs_reconciliation(state=""):
    """(rows, totals) for the per-load bojxona ledger: sent, spent, and the gap.

    Only loads that money actually touched — a yuk nobody sent clearing money for is
    not a settled load, it is simply not part of this ledger, and listing every
    shipment with three zeros would bury the handful that need chasing."""
    shipments = (Shipment.objects.select_related("contract__partner")
                 .prefetch_related("customs_payments__agent", "expenses")
                 .filter(Q(customs_payments__isnull=False)
                         | Q(expenses__customs_agent__isnull=False))
                 .distinct().order_by("-sent", "-created_at"))
    rows = []
    for shipment in shipments:
        diff = shipment.customs_diff_by_currency()
        # Whoever the money went through, for the column that names them. More than
        # one is possible and vanishingly rare, so they are simply listed.
        agents = {p.agent.name for p in shipment.customs_payments.all()}
        agents |= {e.customs_agent.name for e in shipment.expenses.all()
                   if e.customs_agent_id}
        rows.append({
            "shipment": shipment, "agents": ", ".join(sorted(agents)),
            "sent": shipment.customs_sent_by_currency(),
            "spent": shipment.customs_spent_by_currency(),
            "diff": diff,
            # Which way the gap points, for the filter and the badge. A load short
            # in one currency and over in another gets both flags rather than a
            # verdict picked by whichever heap happened to be bigger.
            "left": [(c, a) for c, a in diff if a > 0],
            "over": [(c, a) for c, a in diff if a < 0],
        })
    if state in ("left", "over"):
        rows = [r for r in rows if r[state]]
    elif state == "settled":
        rows = [r for r in rows if not r["diff"]]
    totals = {
        "sent": _by_currency(pair for r in rows for pair in r["sent"]),
        "spent": _by_currency(pair for r in rows for pair in r["spent"]),
        "diff": _by_currency(pair for r in rows for pair in r["diff"]),
    }
    return rows, totals


def customs_open_position():
    """([(currency, farq)] still unaccounted for, how many loads) — the headline the
    list page opens with, and the reason the reconciliation page exists.

    Both directions, not the positive ones only: a load we overfunded and one that
    ran short are both money nobody has squared. And the count travels with the
    figure because a net that nearly cancels across two open loads is not the same
    as nothing to do — it is two loads to chase."""
    rows, _totals = customs_reconciliation()
    open_rows = [r for r in rows if r["diff"]]
    return (_by_currency(pair for r in open_rows for pair in r["diff"]),
            len(open_rows))


@role_required(User.Role.ADMIN)
def customs_loads(request):
    """Yuklar bo'yicha hisob: what was sent for each load against what clearing it
    actually cost. THE screen this feature was asked for."""
    state = request.GET.get("state", "").strip()
    rows, totals = customs_reconciliation(state)
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    return render(request, "crm/customs_loads.html", {
        "page": page, "state": state, "totals": totals,
        "has_filters": bool(state),
    })


@role_required(User.Role.ADMIN)
def customs_detail(request, pk):
    """One bojxonachi's account: every top-up in, every clearing paid out, newest
    first — plus the per-load gaps that money is sitting behind."""
    agent = get_object_or_404(
        CustomsAgent.objects.prefetch_related(
            "payments__shipment__contract__partner",
            "payments__shipment__lines",
            "payments__shipment__status",
            "expenses__shipment__contract__partner",
            "expenses__shipment__lines",
            "expenses__shipment__status"), pk=pk)
    rows = []
    for payment in agent.payments.all():
        target = (f"Yuk #{payment.shipment_id} uchun" if payment.shipment_id
                  else "Umumiy to'ldirish")
        rows.append({"kind": "in", "date": payment.date, "obj": payment,
                     "title": f"{target}" + (f" · {payment.note}" if payment.note else ""),
                     "amount": payment.net_amount, "amount_uzs": payment.net_amount_uzs,
                     "currency": payment.currency, "method": payment.get_method_display(),
                     "method_code": payment.method})
    for expense in agent.expenses.all():
        rows.append({"kind": "out", "date": expense.date, "obj": expense,
                     "title": f"Yuk #{expense.shipment_id} · {expense.get_category_display()}"
                              + (f" · {expense.note}" if expense.note else ""),
                     "amount": expense.amount, "amount_uzs": expense.amount_uzs,
                     "currency": expense.currency, "method": expense.get_method_display(),
                     "method_code": expense.method})
    rows.sort(key=lambda r: (r["date"], r["obj"].pk), reverse=True)
    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    return render(request, "crm/customs_detail.html", {
        "agent": agent, "page": page,
        "loads": holder_loads(agent.expenses.all(), agent.payments.all()),
    })


@role_required(User.Role.ADMIN)
def customs_create(request):
    form = CustomsAgentForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            agent = form.save()
            AuditLog.record(request.user, AuditLog.Action.CREATE, "Bojxonachi",
                            agent.pk, f"Yangi bojxonachi: {agent.name}")
            messages.success(request, "Bojxonachi qo'shildi")
            return form_success(request, reverse("customs_list"))
        return form_response(request, form, "Yangi bojxonachi", invalid=True)
    return form_response(request, form, "Yangi bojxonachi")


@role_required(User.Role.ADMIN)
def customs_edit(request, pk):
    agent = get_object_or_404(CustomsAgent, pk=pk)
    form = CustomsAgentForm(request.POST or None, instance=agent)
    title = "Bojxonachini tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(request.user, AuditLog.Action.UPDATE, "Bojxonachi",
                            agent.pk, f"Bojxonachi tahrirlandi: {agent.name}")
            messages.success(request, "Bojxonachi yangilandi")
            return form_reload(request, reverse("customs_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def customs_delete(request, pk):
    agent = get_object_or_404(CustomsAgent, pk=pk)
    if request.method == "POST":
        try:
            agent.delete()
            AuditLog.record(request.user, AuditLog.Action.DELETE, "Bojxonachi", pk,
                            f"Bojxonachi o'chirildi: {agent.name}")
            messages.success(request, "Bojxonachi o'chirildi")
        except ProtectedError:
            # PROTECT on both sides, same as a logist: somebody with money behind
            # them is a piece of the ledger, not a contact card to tidy away.
            messages.error(request, "Bojxonachiga to'lov yoki xarajat biriktirilgan")
        return form_reload(request, reverse("customs_list"))
    return render_confirm(
        request, "Bojxonachini o'chirish", f"“{agent.name}” o'chiriladi.",
        "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="customs_list")


@role_required(User.Role.ADMIN)
def customs_payment_create(request):
    initial = {}
    # Prefilled from either side: from the bojxonachi's page (who), or from a yuk
    # (what for) — the two ways this form is actually reached.
    agent_id = request.GET.get("agent")
    if agent_id and agent_id.isdigit():
        initial["agent"] = int(agent_id)
    shipment_id = request.GET.get("shipment")
    if shipment_id and shipment_id.isdigit():
        initial["shipment"] = int(shipment_id)
    form = CustomsPaymentForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user
            payment.save()
            AuditLog.record(
                request.user, AuditLog.Action.PAYMENT, "Bojxonaga to'lov", payment.pk,
                f"Bojxonaga to'lov: {_payment_label(payment)}")
            messages.success(request, "Bojxonaga to'lov qo'shildi")
            return form_success(request, reverse("customs_list"))
        return form_response(request, form, "Bojxonaga pul yuborish", invalid=True)
    return form_response(request, form, "Bojxonaga pul yuborish")


@role_required(User.Role.ADMIN)
def customs_payment_edit(request, pk):
    payment = get_object_or_404(CustomsPayment, pk=pk)
    form = CustomsPaymentForm(request.POST or None, instance=payment)
    title = "To'lovni tahrirlash"
    if request.method == "POST":
        if form.is_valid():
            form.save()
            AuditLog.record(
                request.user, AuditLog.Action.UPDATE, "Bojxonaga to'lov", payment.pk,
                f"Bojxonaga to'lov tahrirlandi: {_payment_label(payment)}")
            messages.success(request, "To'lov yangilandi")
            return form_reload(request, reverse("customs_list"))
        return form_response(request, form, title, invalid=True)
    return form_response(request, form, title)


@role_required(User.Role.ADMIN)
def customs_payment_delete(request, pk):
    payment = get_object_or_404(CustomsPayment, pk=pk)
    if request.method == "POST":
        label = _payment_label(payment)
        payment.delete()
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Bojxonaga to'lov", pk,
                        f"Bojxonaga to'lov o'chirildi: {label}")
        messages.success(request, "To'lov o'chirildi")
        return form_reload(request, reverse("customs_list"))
    return render_confirm(
        request, "To'lovni o'chirish",
        f"“{_payment_label(payment)}” to'lovi o'chiriladi.",
        "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="customs_list")
