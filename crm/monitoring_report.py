"""GranulaLog's sections of the daily monitoring report (see monitoring/report.py).

Each takes (start, end), yesterday's bounds in local time, and returns lines whose
first line is the section title. Business figures read each row's own `date`, the
day the operator says it happened. Activity and suspicious actions read the audit log
and the login log, which record when things were actually done."""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from crm.models import (AuditLog, Contract, ContractExpense, Currency, Customer,
                        CustomerPayment, CustomsPayment, LogistPayment, OtherExpense,
                        Reservation, Return, Sale, Scale, Shipment, ShipmentExpense,
                        ShipmentLine, SupplierPayment, arrived_lots, brand_stock_costed,
                        customer_receivable_by_currency, kassa_cash_by_currency,
                        kassa_cash_by_method, kassa_row_sets, own_side)

COMPARE_DAYS = 7               # "vs 7-day average"
OLD_RECORD_AFTER = timedelta(days=3)   # editing a row older than this is flagged
BACKDATED_AFTER = timedelta(days=3)    # a row dated this far before its entry is flagged
NIGHT_FROM, NIGHT_TO = 22, 7           # local hours counted as off-hours
LARGE_FACTOR = 3               # an amount this many times the 30-day average is flagged
LARGE_MIN_SAMPLES = 5
MAX_LISTED = 12                # longest list per suspicious rule
SCALE_QUIET_AFTER = timedelta(hours=24)
LOW_STOCK_DAYS = 7             # flag a marka with less than this many days of sales left
STOCK_SALES_WINDOW = 30        # days of sales the daily rate is averaged over
TOP_OVERDUE = 10

# Audit target types whose rows carry money, mapped to their model: an edit of an
# old one, or a backdated one, is what the suspicious section looks for.
MONEY_TYPES = {
    "Sotuv": Sale,
    "Mijoz to'lovi": CustomerPayment,
    "Hamkor to'lovi": SupplierPayment,
    "Yuk xarajati": ShipmentExpense,
    "Kelishuv xarajati": ContractExpense,
    "Boshqa chiqim": OtherExpense,
    "Logistga to'lov": LogistPayment,
    "Bojxonaga to'lov": CustomsPayment,
    "Vazvrat": Return,
}
ACTION_LABELS = dict(AuditLog.Action.choices)


# --- formatting --------------------------------------------------------------

def _num(value, places=0):
    return f"{value:,.{places}f}".replace(",", " ")


def _money(pairs):
    """[(currency, amount)] → "$12 340 · 45 600 000 so'm"."""
    parts = []
    for currency, amount in pairs:
        if not amount:
            continue
        sign = "−" if amount < 0 else ""
        value = _num(abs(amount))
        parts.append(f"{sign}${value}" if currency == Currency.USD else f"{sign}{value} so'm")
    return " · ".join(parts) or "0"


def _ordered(totals):
    """{currency: amount} → [(currency, amount)], dollars first."""
    return [(c, totals[c]) for c in (Currency.USD, Currency.UZS) if totals.get(c)]


def _by_currency(rows, usd_attr="amount", uzs_attr="amount_uzs"):
    totals = defaultdict(Decimal)
    for row in rows:
        totals[row.currency] += own_side(row, getattr(row, usd_attr), getattr(row, uzs_attr)) or 0
    return [(c, totals[c]) for c in (Currency.USD, Currency.UZS) if totals.get(c)]


def _vs_average(today_value, history):
    """' (↑ 40% vs 7-day avg)' or '' when there is no history to compare to."""
    if not history:
        return ""
    avg = sum(history) / len(history)
    if not avg:
        return ""
    change = (Decimal(today_value) - Decimal(avg)) / Decimal(avg) * 100
    arrow = "↑" if change > 0 else "↓" if change < 0 else "="
    return f" ({arrow} {abs(change):.0f}% vs {COMPARE_DAYS}-day avg)"


def _local(dt):
    return timezone.localtime(dt)


def _who(user):
    if user is None:
        return "system"
    return user.get_full_name() or user.get_username()


# --- 📈 business -------------------------------------------------------------

def _sale_groups(sales):
    return {s.group or f"pk{s.pk}" for s in sales}


def _sales_lines(day):
    sales = list(Sale.objects.filter(date=day).select_related("customer"))
    if not sales:
        return ["🛒 Sales: none"]
    kg = sum((s.kg for s in sales), Decimal("0"))
    groups = _sale_groups(sales)

    past = Sale.objects.filter(date__gte=day - timedelta(days=COMPARE_DAYS), date__lt=day)
    kg_by_day = defaultdict(Decimal)
    for d, k in past.values_list("date", "kg"):
        kg_by_day[d] += k
    history = [kg_by_day.get(day - timedelta(days=i), Decimal("0"))
               for i in range(1, COMPARE_DAYS + 1)]

    turnover = _by_currency(sales, "total", "total_uzs")
    lines = [f"🛒 Sales: {len(groups)} sales, {_num(kg)} kg{_vs_average(kg, history)}",
             f"   turnover {_money(turnover)}"]
    by_customer = Counter()
    for s in sales:
        by_customer[s.customer.name] += s.kg
    top = ", ".join(f"{name} {_num(k)} kg" for name, k in by_customer.most_common(3))
    lines.append(f"   top: {top}")
    return lines


def _count_and_money(label, rows):
    rows = list(rows)
    if not rows:
        return [f"{label}: none"]
    return [f"{label}: {len(rows)} · {_money(_by_currency(rows))}"]


def business(start, end):
    day = start.date()
    lines = ["📈 BUSINESS"]
    lines += _sales_lines(day)
    lines += _count_and_money("💵 Customer payments", CustomerPayment.objects.filter(date=day))
    lines += _count_and_money("🏭 Supplier payments", SupplierPayment.objects.filter(date=day))
    expenses = [*ShipmentExpense.objects.filter(date=day),
                *ContractExpense.objects.filter(date=day),
                *OtherExpense.objects.filter(date=day),
                *LogistPayment.objects.filter(date=day),
                *CustomsPayment.objects.filter(date=day)]
    lines += _count_and_money("💸 Expenses (yuk, kelishuv, boshqa, logist, bojxona)", expenses)

    sent = Shipment.objects.filter(sent=day).count()
    arrived = Shipment.objects.filter(arrived=day).count()
    lines.append(f"🚚 Trucks: {sent} sent, {arrived} arrived")
    new_contracts = Contract.objects.filter(created=day).count()
    new_customers = Customer.objects.filter(created_at__gte=start, created_at__lt=end).count()
    brons = Reservation.objects.filter(date=day).count()
    returns = Return.objects.filter(date=day).count()
    lines.append(f"🆕 {new_contracts} contracts, {new_customers} customers, "
                 f"{brons} brons, {returns} returns")

    receivable, debtors = customer_receivable_by_currency()
    lines.append(f"📒 Customers owe now: {_money(receivable)} ({debtors} customers)")
    overdue = [s for s in Shipment.objects.filter(arrived__isnull=True, eta__isnull=False)
               if s.is_overdue]
    if overdue:
        lines.append(f"⏰ Overdue trucks: {len(overdue)}")
    return lines


# --- 👥 activity -------------------------------------------------------------

def _hour_chart(hours):
    """Text bar chart of actions per local hour, first to last active hour."""
    if not hours:
        return []
    peak = max(hours.values())
    first, last = min(hours), max(hours)
    rows = []
    for h in range(first, last + 1):
        n = hours.get(h, 0)
        bar = "█" * max(1, round(n / peak * 12)) if n else "·"
        rows.append(f"{h:02d}  {bar} {n or ''}".rstrip())
    return rows


def activity(start, end):
    from axes.models import AccessLog

    entries = list(AuditLog.objects.filter(created_at__gte=start, created_at__lt=end)
                   .select_related("user"))
    logins = list(AccessLog.objects.filter(attempt_time__gte=start, attempt_time__lt=end))
    lines = ["👥 ACTIVITY"]
    login_users = {l.username for l in logins}
    lines.append(f"🔑 Logins: {len(logins)} by {len(login_users)} users")
    if not entries:
        lines.append("📝 No actions recorded")
        return lines

    by_user = defaultdict(Counter)
    for e in entries:
        by_user[_who(e.user)][e.target_type] += 1
    lines.append(f"📝 {len(entries)} actions by {len(by_user)} users:")
    for who, kinds in sorted(by_user.items(), key=lambda kv: -sum(kv[1].values())):
        top = ", ".join(f"{t} {n}" for t, n in kinds.most_common(3))
        lines.append(f"• {who}: {sum(kinds.values())} ({top})")

    by_action = Counter(ACTION_LABELS.get(e.action, e.action) for e in entries)
    lines.append("   " + ", ".join(f"{a} {n}" for a, n in by_action.most_common()))

    hours = Counter(_local(e.created_at).hour for e in entries)
    peak_hour, peak_n = hours.most_common(1)[0]
    first, last = min(_local(e.created_at) for e in entries), max(_local(e.created_at) for e in entries)
    lines.append(f"🕐 Busiest hour {peak_hour:02d}:00 ({peak_n} actions); "
                 f"first {first:%H:%M}, last {last:%H:%M}")
    lines += _hour_chart(hours)
    return lines


# --- 🕵️ suspicious -----------------------------------------------------------

def _limited(title, items):
    if not items:
        return []
    shown = items[:MAX_LISTED]
    more = len(items) - len(shown)
    return [title, *shown, *([f"   … and {more} more"] if more else [])]


def _entry_line(e):
    return f"• {_local(e.created_at):%H:%M} {_who(e.user)} — {e.target_type}: {e.summary[:110]}"


def _deletions(entries):
    deleted = [e for e in entries if e.action == AuditLog.Action.DELETE]
    lines = _limited(f"🗑 Deleted ({len(deleted)}):", [_entry_line(e) for e in deleted])
    heavy = [who for who, n in Counter(_who(e.user) for e in deleted).items() if n >= 5]
    if heavy:
        lines.append(f"   ‼️ many deletions by: {', '.join(heavy)}")
    return lines


def _grouped(title, items, unit):
    """items: (who, label, pk, days). One line per (who, label): how many records,
    how far back, and a few ids to look up."""
    if not items:
        return []
    groups = defaultdict(list)
    for who, label, pk, days in items:
        groups[(who, label)].append((pk, days))
    lines = [title]
    for (who, label), rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        pks = sorted({pk for pk, _ in rows})
        days = [d for _, d in rows]
        span = f"{min(days)}" if min(days) == max(days) else f"{min(days)}–{max(days)}"
        ids = ", ".join(f"#{pk}" for pk in pks[:4]) + (" …" if len(pks) > 4 else "")
        lines.append(f"• {who} — {label} ×{len(rows)}, {span} {unit} ({ids})")
    return lines


def _old_edits(entries, start):
    edits = [e for e in entries
             if e.action == AuditLog.Action.UPDATE and e.target_type in MONEY_TYPES and e.target_id]
    ids = defaultdict(set)
    for e in edits:
        ids[e.target_type].add(e.target_id)
    created = {}
    for kind, pks in ids.items():
        for pk, at in MONEY_TYPES[kind].objects.filter(pk__in=pks).values_list("pk", "created_at"):
            created[(kind, pk)] = at
    flagged = []
    for e in edits:
        at = created.get((e.target_type, e.target_id))
        if at and at < start - OLD_RECORD_AFTER:
            flagged.append((_who(e.user), e.target_type, e.target_id, (e.created_at - at).days))
    return _grouped(f"✏️ Old money records edited ({len(flagged)} edits):", flagged, "days old")


def _backdated(start, end):
    flagged = []
    for label, model in MONEY_TYPES.items():
        rows = model.objects.filter(created_at__gte=start, created_at__lt=end).select_related("created_by")
        for row in rows:
            entered = _local(row.created_at).date()
            if row.date < entered - BACKDATED_AFTER:
                flagged.append((_who(row.created_by), label, row.pk, (entered - row.date).days))
    return _grouped(f"📅 Backdated entries ({len(flagged)}):", flagged, "days back")


def _night(entries):
    night = [e for e in entries
             if _local(e.created_at).hour >= NIGHT_FROM or _local(e.created_at).hour < NIGHT_TO]
    if not night:
        return []
    per_user = Counter(_who(e.user) for e in night)
    who = ", ".join(f"{u} {n}" for u, n in per_user.most_common())
    return [f"🌙 Off-hours actions ({NIGHT_FROM}:00–0{NIGHT_TO}:00): {len(night)} — {who}"]


def _failed_logins(start, end):
    from axes.models import AccessAttempt

    attempts = list(AccessAttempt.objects.filter(attempt_time__gte=start, attempt_time__lt=end))
    if not attempts:
        return []
    limit = getattr(settings, "AXES_FAILURE_LIMIT", 5)
    lines = [f"🚫 Failed logins: {sum(a.failures_since_start for a in attempts)}"]
    for a in attempts[:MAX_LISTED]:
        locked = " — LOCKED OUT" if a.failures_since_start >= limit else ""
        lines.append(f"• {a.username or '?'} from {a.ip_address}: "
                     f"{a.failures_since_start}x{locked}")
    return lines


def _user_changes(entries):
    changed = [_entry_line(e) for e in entries if e.target_type == "Foydalanuvchi"]
    return _limited(f"👤 User accounts changed ({len(changed)}):", changed)


def _large_amounts(start, end):
    flagged = []
    checks = [("Sotuv", Sale, "total", "total_uzs"),
              ("Mijoz to'lovi", CustomerPayment, "amount", "amount_uzs"),
              ("Hamkor to'lovi", SupplierPayment, "amount", "amount_uzs")]
    for label, model, usd, uzs in checks:
        recent = list(model.objects.filter(created_at__gte=start - timedelta(days=30),
                                           created_at__lt=start))
        averages = {}
        for currency in (Currency.USD, Currency.UZS):
            values = [own_side(r, getattr(r, usd), getattr(r, uzs)) or 0
                      for r in recent if r.currency == currency]
            if len(values) >= LARGE_MIN_SAMPLES:
                averages[currency] = sum(values) / len(values)
        for row in model.objects.filter(created_at__gte=start, created_at__lt=end).select_related("created_by"):
            avg = averages.get(row.currency)
            value = own_side(row, getattr(row, usd), getattr(row, uzs)) or 0
            if avg and value > avg * LARGE_FACTOR:
                flagged.append(f"• {_local(row.created_at):%H:%M} {_who(row.created_by)} — "
                               f"{label} #{row.pk}: {_money([(row.currency, value)])} "
                               f"({value / avg:.1f}× the 30-day average)")
    return _limited(f"💰 Unusually large amounts ({len(flagged)}):", flagged)


def suspicious(start, end):
    entries = list(AuditLog.objects.filter(created_at__gte=start, created_at__lt=end)
                   .select_related("user"))
    found = [*_deletions(entries), *_old_edits(entries, start), *_backdated(start, end),
             *_large_amounts(start, end), *_night(entries), *_failed_logins(start, end),
             *_user_changes(entries)]
    return ["🕵️ WORTH A LOOK", *(found or ["✅ Nothing unusual"])]


# --- ⚖️ scales ---------------------------------------------------------------

def scales(start, end):
    active = list(Scale.objects.filter(is_active=True))
    if not active:
        return []
    now = timezone.now()
    lines = ["⚖️ SCALES"]
    for scale in active:
        if scale.online:
            state = "online"
        elif scale.last_seen and now - scale.last_seen < SCALE_QUIET_AFTER:
            state = f"offline, last data {_local(scale.last_seen):%H:%M}"
        else:
            state = "⚠️ no data for 24h+"
        lines.append(f"• {scale.name}: {state}")
    return lines



# --- ⏰ overdue debts ---------------------------------------------------------

def overdue_debts(start, end):
    """Who is past their to'lov muddati, in each sotuv's own currency."""
    today = timezone.localdate()
    sales = (Sale.objects.filter(debt_deadline__lt=today)
             .select_related("customer").prefetch_related("allocations", "returns"))
    per_customer = defaultdict(lambda: {"owed": [], "oldest": None})
    for sale in sales:
        if not sale.is_overdue:
            continue
        entry = per_customer[sale.customer]
        entry["owed"].append((sale.currency, sale.remaining_own))
        if entry["oldest"] is None or sale.debt_deadline < entry["oldest"]:
            entry["oldest"] = sale.debt_deadline
    if not per_customer:
        return ["⏰ OVERDUE DEBTS", "✅ Nobody is past their muddat"]

    totals = defaultdict(Decimal)
    rows = []
    for customer, entry in per_customer.items():
        owed = defaultdict(Decimal)
        for currency, amount in entry["owed"]:
            owed[currency] += amount
            totals[currency] += amount
        days = (today - entry["oldest"]).days
        rows.append((days, customer.name, _ordered(owed)))
    rows.sort(key=lambda r: -r[0])
    newly = [name for days, name, _ in rows if days == 1]
    lines = ["⏰ OVERDUE DEBTS",
             f"{len(rows)} customers · {_money(_ordered(totals))}"]
    for days, name, owed in rows[:TOP_OVERDUE]:
        lines.append(f"• {name}: {_money(owed)} — {days} days late")
    if len(rows) > TOP_OVERDUE:
        lines.append(f"   … and {len(rows) - TOP_OVERDUE} more")
    if newly:
        lines.append(f"🆕 became overdue yesterday: {', '.join(newly)}")
    return lines


# --- 📦 stock ----------------------------------------------------------------

def low_stock(start, end):
    """Markas that will run out soon at the recent sales rate, or have already."""
    day = start.date()
    since = day - timedelta(days=STOCK_SALES_WINDOW - 1)
    sold = defaultdict(Decimal)
    for brand, kg in (Sale.objects.filter(date__gte=since, date__lte=day)
                      .values_list("line__contract_line__brand", "kg")):
        sold[brand] += kg
    stock = {row["brand"]: row for row in brand_stock_costed()}
    transit = defaultdict(Decimal)
    for brand, kg in (ShipmentLine.objects.filter(shipment__arrived__isnull=True)
                      .values_list("contract_line__brand", "kg")):
        transit[brand] += kg

    flagged = []
    for brand, kg_sold in sold.items():
        rate = kg_sold / STOCK_SALES_WINDOW
        row = stock.get(brand)
        free = (row["on_hand"] - row["reserved"]) if row else Decimal("0")
        days_left = free / rate if rate else None
        if days_left is not None and days_left >= LOW_STOCK_DAYS:
            continue
        on_way = f", {_num(transit[brand])} kg on the way" if transit.get(brand) else ""
        if free <= 0:
            state = "❌ out" + (f" ({_num(row['reserved'])} kg all bronned)" if row else "")
        else:
            left = "under 1 day" if days_left < 1 else f"{days_left:.0f} days"
            state = f"{_num(free)} kg free ≈ {left}"
        flagged.append((days_left or 0,
                        f"• {brand}: {state} (sells ~{_num(rate)} kg/day){on_way}"))
    total = sum((row["on_hand"] for row in stock.values()), Decimal("0"))
    lines = ["📦 STOCK", f"On hand: {_num(total)} kg in {len(stock)} markas"]
    if not flagged:
        return lines + [f"✅ Every selling marka has {LOW_STOCK_DAYS}+ days of stock"]
    flagged.sort(key=lambda f: f[0])
    return lines + [f"⚠️ Running low (under {LOW_STOCK_DAYS} days at the "
                    f"{STOCK_SALES_WINDOW}-day sales rate):", *[text for _, text in flagged]]


# --- 🏦 kassa ----------------------------------------------------------------

def _rows_on(rows, day):
    """The kassa row sets narrowed to the rows that moved money on `day`."""
    def on(row):
        return (getattr(row, "paid_date", None) or row.date) == day
    return {key: [r for r in value if on(r)] for key, value in rows.items()}


def kassa(start, end):
    """The till now, yesterday's movement, and a flag for any heap (method ×
    currency) that turned negative YESTERDAY. A heap that has long been negative is
    how these books are kept (purchases funded from outside the till); one that just
    crossed zero usually means a to'lov went in under the wrong usul or currency."""
    rows = kassa_row_sets()
    balance = kassa_cash_by_currency(rows)
    by_method = kassa_cash_by_method(rows)
    day_rows = _rows_on(rows, start.date())
    day_by_method = {m["code"]: dict(m["split_full"]) for m in kassa_cash_by_method(day_rows)}
    moved_in = kassa_cash_by_currency({**day_rows, "outgoing": [], "exchange": []})
    moved_out = kassa_cash_by_currency({**day_rows, "incoming": [], "kapital": [], "exchange": []})

    lines = ["🏦 KASSA", f"Balance now: {_money(balance)}"]
    if moved_in or moved_out:
        lines.append(f"Yesterday: in {_money(moved_in)} · out {_money([(c, -a) for c, a in moved_out])}")
    flags = []
    for method in by_method:
        if method["split"]:
            lines.append(f"• {method['label']}: {_money(method['split'])}")
        for currency, now in method["split_full"]:
            before = now - day_by_method.get(method["code"], {}).get(currency, Decimal("0"))
            if now < 0 <= before:
                flags.append(f"‼️ {method['label']} went negative yesterday: "
                             f"{_money([(currency, before)])} → {_money([(currency, now)])} "
                             "— check yesterday's entries for a wrong usul or valyuta")
    return lines + flags


# --- 🧮 data checks ----------------------------------------------------------

def data_checks(start, end):
    """The repo's own read-only consistency checks, run every night."""
    from crm.management.commands.check_fifo import check as fifo_check
    from crm.management.commands.reconcile_allocations import (
        _payments, _sales, planned_moves, unlinked_payment_money)

    problems = []
    negative = [lot for lot in arrived_lots() if lot.available_kg < 0]
    if negative:
        brands = sorted({lot.brand for lot in negative})
        problems.append(f"‼️ {len(negative)} lots below zero kg ({', '.join(brands[:5])}) "
                        "→ manage.py fix_negative_lots")

    drift = short = 0
    for _marka, moved, _undated, missing, _count in fifo_check():
        drift += sum(1 for row in moved if row["fifo"])
        short += len(missing)
    if drift:
        problems.append(f"⚠️ {drift} FIFO sales sit on the wrong lot (tannarx/foyda off) "
                        "→ manage.py check_fifo")
    if short:
        problems.append(f"‼️ {short} sales need kg that never arrived → manage.py check_fifo")

    customers = 0
    for customer in Customer.objects.all():
        sales = _sales(customer)
        if not sales:
            continue
        if planned_moves(sales) or unlinked_payment_money(customer, sales, _payments(customer)):
            customers += 1
    if customers:
        problems.append(f"⚠️ {customers} customers have payments on the wrong sotuv "
                        "→ manage.py reconcile_allocations")
    return ["🧮 DATA CHECKS", *(problems or ["✅ Stock, FIFO and payment links all add up"])]


# --- 📅 weekly (Mondays) -----------------------------------------------------

def _week_figures(first, last):
    sales = list(Sale.objects.filter(date__gte=first, date__lte=last).select_related("customer"))
    payments = list(CustomerPayment.objects.filter(date__gte=first, date__lte=last))
    kg_by_day = defaultdict(Decimal)
    by_customer = Counter()
    for sale in sales:
        kg_by_day[sale.date] += sale.kg
        by_customer[sale.customer.name] += sale.kg
    return {
        "count": len(_sale_groups(sales)),
        "kg": sum((s.kg for s in sales), Decimal("0")),
        "turnover": _by_currency(sales, "total", "total_uzs"),
        "paid": _by_currency(payments),
        "customers": by_customer,
        "best_day": max(kg_by_day.items(), key=lambda kv: kv[1]) if kg_by_day else None,
        "new_customers": Customer.objects.filter(
            created_at__date__gte=first, created_at__date__lte=last).count(),
    }


def _change(now, before):
    if not before:
        return ""
    pct = (Decimal(now) - Decimal(before)) / Decimal(before) * 100
    return f" ({'↑' if pct > 0 else '↓' if pct < 0 else '='} {abs(pct):.0f}%)"


def weekly(start, end):
    """Only in the Monday report (the day being reported is a Sunday)."""
    last = start.date()
    if last.weekday() != 6:
        return []
    first = last - timedelta(days=6)
    this, prev = _week_figures(first, last), _week_figures(first - timedelta(days=7),
                                                           first - timedelta(days=1))
    lines = [f"📅 WEEK {first:%d.%m}–{last:%d.%m} vs the week before",
             f"🛒 {this['count']} sales{_change(this['count'], prev['count'])}, "
             f"{_num(this['kg'])} kg{_change(this['kg'], prev['kg'])}",
             f"   turnover {_money(this['turnover'])} (before: {_money(prev['turnover'])})",
             f"💵 Payments in {_money(this['paid'])} (before: {_money(prev['paid'])})",
             f"🆕 {this['new_customers']} new customers (before: {prev['new_customers']})"]
    if this["best_day"]:
        day, kg = this["best_day"]
        lines.append(f"📈 Best day: {day:%A} with {_num(kg)} kg")
    if this["customers"]:
        lines.append("🏆 Top customers:")
        for name, kg in this["customers"].most_common(5):
            before = prev["customers"].get(name, 0)
            lines.append(f"• {name}: {_num(kg)} kg{_change(kg, before) if before else ' (new this week)'}")
    return lines
