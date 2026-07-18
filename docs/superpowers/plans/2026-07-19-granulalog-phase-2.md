# GranulaLog Phase 2 (Warehouse & Selling) Implementation Plan

> **For agentic workers:** execute task-by-task with the subagent-driven-development loop. Checkbox (`- [ ]`) steps.

**Goal:** Turn arrived shipments into sellable warehouse lots, and build the full customer selling side: single-line sales priced against each lot's landed cost, returns, a customer payment ledger with FIFO+manual allocation and advances, reservations (bron) that block lot kg and convert to sales, and a debts page.

**Architecture:** Extends the Phase 1 `crm` app. An arrived `Shipment` (status `is_arrival`, `arrived` set) IS a warehouse lot; its `landed_cost_per_kg` (Phase 1) is snapshotted onto each sale. Money stays canonical USD; the `MoneyEntryFormMixin` (so'm→USD) and modal CRUD helpers (`crm/utils.py`) are reused throughout. Customer balances are derived: Σ sale net_total − Σ payments; per-sale paid comes from `PaymentAllocation` rows; unallocated payment money is the customer's advance.

**Tech Stack:** Django 6, pytest/SQLite, existing modal system, `usd` money filter, `crm_extras`.

## Global Constraints

- Root: `/Users/khusan/Desktop/logistic-crm`. Admin-only for ALL Phase 2 features (translators still see only Yuklar — never add Phase 2 nav items outside the admin block, never expose money to translators).
- Money canonical USD: `MONEY`=Decimal(14,2); unit price Decimal(14,4); kg Decimal(12,3). so'm entries via `MoneyEntryFormMixin`; money models put `blank=True` on `exchange_rate` (+`default=0`) and `default=0` on `amount_original` (Task-5 lesson).
- Modal CRUD convention (Phase 1): `form_response`/`form_success` (create) / `form_reload` (edit,delete) / `render_confirm` (delete GET). Lists use `data-modal`. Every CRUD test set includes AJAX GET-partial / valid-POST-204+X-Redirect / invalid-POST-422.
- `AuditLog.record(user, action, target_type, target_id, summary)` on every mutation.
- Render money with `{{ value|usd }}` (load `crm_extras`). kg is not money.
- Tests: `pytest` from root, settings `config.settings_test`. Text files end with newline.
- A "lot" = a `Shipment` with `arrived` set (status.is_arrival). Only arrived lots can be sold from or (once arrived) fully reserved; in-transit shipments can be reserved but not sold.
- Commit after each task; end commit bodies with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Customers (Mijozlar) CRUD

**Files:** modify `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`; create `templates/crm/customer_list.html`; test `tests/test_customers.py`.

**Produces:** `Customer(name, phone, address, note, created_at)` ordering `["name"]` verbose "Mijoz"; URLs `customer_list/create/edit/delete`; balance helpers used by later tasks (added here as stubs returning 0 via hasattr guards for `sales`/`customer_payments`, mirroring Phase-1 Contract guards).

- [ ] Step 1: failing test — create via modal, list+search, translator 403, plus AJAX modal-path tests. (Mirror `tests/test_partners.py` exactly, s/partner/customer/, fields name/phone/address/note.)
- [ ] Step 2: run → FAIL.
- [ ] Step 3: implement. Model:
```python
class Customer(models.Model):
    name = models.CharField("Ismi", max_length=200)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    address = models.CharField("Manzil", max_length=300, blank=True)
    note = models.TextField("Izoh", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]; verbose_name = "Mijoz"; verbose_name_plural = "Mijozlar"

    @property
    def sales_total(self):
        if not hasattr(self, "sales"): return Decimal("0")
        return sum((s.net_total for s in self.sales.all()), Decimal("0"))

    @property
    def paid_total(self):
        if not hasattr(self, "customer_payments"): return Decimal("0")
        return self.customer_payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def balance(self):
        """Positive = customer owes us (qarz); negative = advance (avans)."""
        return self.sales_total - self.paid_total

    def __str__(self): return self.name
```
`CustomerForm` (fields name/phone/address/note; textarea note). Views mirror partner CRUD (admin-only, target_type "Mijoz", modal helpers). `customer_list` search over name/phone/address. Template mirrors `partner_list.html`; columns Ismi/Telefon/Manzil/Balans (`{{ c.balance|usd }}` — red qarz when >0, green avans when <0, via a small inline conditional) /actions. Nav "Mijozlar" in admin block (a new "Savdo" nav group is fine).
- [ ] Step 4: makemigrations, tests green, full suite.
- [ ] Step 5: commit `feat: customers CRUD`.

---

### Task 2: Warehouse lots (Ombor) — lot availability + page

**Files:** modify `crm/models.py` (Shipment lot props + a `lots` queryset helper), `crm/views.py`, `config/urls.py`, `templates/base.html`; create `templates/crm/ombor.html`; test `tests/test_ombor.py`.

**Produces:** on `Shipment` — `sold_kg`, `reserved_kg`, `returned_kg`, `available_kg` (all hasattr-guarded so they return 0 until Tasks 3/5/6 add the `sale_set`/`returns`/`reservations` relations); `Shipment.lots()` classmethod → arrived shipments annotated/iterable for the ombor page; URL `ombor`.

- [ ] Step 1: failing test — an arrived shipment appears as a lot with `available_kg == kg` when nothing sold/reserved; a non-arrived shipment is NOT a lot; translator 403 on `/ombor/`.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement. Add to `Shipment`:
```python
    @property
    def is_lot(self):
        return self.arrived is not None

    @property
    def sold_kg(self):
        if not hasattr(self, "sales"): return Decimal("0")
        return sum((s.kg for s in self.sales.all()), Decimal("0"))

    @property
    def returned_kg(self):
        # kg flowed back into this lot by restocked returns on its sales
        if not hasattr(self, "sales"): return Decimal("0")
        total = Decimal("0")
        for s in self.sales.all():
            total += sum((r.kg for r in s.returns.all() if r.restock), Decimal("0"))
        return total

    @property
    def reserved_kg(self):
        if not hasattr(self, "reservations"): return Decimal("0")
        return sum((r.kg for r in self.reservations.all() if r.status == "active"), Decimal("0"))

    @property
    def available_kg(self):
        return self.kg - self.sold_kg - self.reserved_kg + self.returned_kg
```
(`sales` = the reverse relation from `Sale.shipment` added in Task 3, `related_name="sales"`.) `ombor` view (admin-only): `lots = Shipment.objects.filter(arrived__isnull=False).select_related("contract__partner")`, search over brand/partner/contract id, paginate. Template columns: Kelishuv #, Hamkor, Marka, Kelgan sana, Kirim kg, Sotilgan kg, Bron kg, Qoldiq kg (`available_kg`), Tan narx (`{{ lot.landed_cost_per_kg }} $/kg`), and actions "Sotish" (data-modal → `sale_create?lot=<pk>`, Task 3) + "Bron" (data-modal → `reservation_create?lot=<pk>`, Task 6) — render those action buttons but they can 404 until their tasks land; guard by only linking if the url exists is overkill — instead, in THIS task, render them as disabled placeholders or omit and add in Tasks 3/6. Simplest: omit the action buttons here; Task 3 adds "Sotish", Task 6 adds "Bron". Nav "Ombor" admin block.
- [ ] Step 4: makemigrations (none expected — all properties), tests green, full suite.
- [ ] Step 5: commit `feat: warehouse lots (ombor) with availability`.

---

### Task 3: Sales (single-line) from a lot, with cost snapshot and debt

**Files:** modify `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`, `templates/crm/ombor.html` (add "Sotish"); create `templates/crm/sale_list.html`, `templates/crm/sale_detail.html`; test `tests/test_sales.py`.

**Produces:** `Sale(customer FK PROTECT related_name="sales", shipment FK PROTECT related_name="sales", kg, price, cost_price, date, debt_deadline null, reservation FK SET_NULL null related_name="+", note, created_by)`; derived `total`, `returned_amount`, `net_total`, `paid` (Σ allocations — 0 until Task 4), `remaining`, `is_overdue`, `profit`. URLs `sale_list/create/edit/delete/detail`.

- [ ] Step 1: failing tests — creating a sale for 4,000 kg from a 10,000 kg lot @ landed cost $1.20, sale price $1.60: sale.total == $6,400, cost_price snapshot == 1.20 (frozen even if lot expenses change later), profit == (1.60−1.20)*4000 == $1,600, lot.available_kg drops to 6,000, customer.balance rises by 6,400; selling more than available_kg is rejected; selling from a non-arrived shipment is rejected; translator 403. Plus modal-path tests.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement. Model:
```python
class Sale(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales", verbose_name="Mijoz")
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="sales", verbose_name="Lot (yuk)")
    kg = models.DecimalField("Sotilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg sotuv narxi (USD)", max_digits=14, decimal_places=4)
    cost_price = models.DecimalField("1 kg tan narxi (USD)", max_digits=14, decimal_places=4)
    date = models.DateField("Sana", default=timezone.localdate)
    debt_deadline = models.DateField("To'lov muddati", null=True, blank=True)
    reservation = models.ForeignKey("Reservation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Bron")
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="sales")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]; verbose_name = "Sotuv"; verbose_name_plural = "Sotuvlar"

    @property
    def total(self): return (self.kg * self.price).quantize(Decimal("0.01"))
    @property
    def returned_amount(self):
        if not hasattr(self, "returns"): return Decimal("0")
        return sum((r.amount for r in self.returns.all()), Decimal("0"))
    @property
    def net_total(self): return self.total - self.returned_amount
    @property
    def paid(self):
        if not hasattr(self, "allocations"): return Decimal("0")
        return self.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    @property
    def remaining(self): return self.net_total - self.paid
    @property
    def is_paid(self): return self.remaining <= 0
    @property
    def is_overdue(self):
        return self.remaining > 0 and self.debt_deadline is not None and self.debt_deadline < timezone.localdate()
    @property
    def profit(self): return ((self.price - self.cost_price) * self.kg).quantize(Decimal("0.01")) - self._returned_profit
    @property
    def _returned_profit(self):
        if not hasattr(self, "returns"): return Decimal("0")
        return sum(((r.price - self.cost_price) * r.kg for r in self.returns.all() if r.restock), Decimal("0"))
```
Note the `reservation` FK forward-references the `Reservation` model (Task 6) by string — fine; migration for Task 3 will include a nullable FK to a not-yet-existing model, so DEFER the `reservation` field to Task 6 (add it in Task 6's migration) OR create a minimal `Reservation` stub now. Cleanest: **omit the `reservation` field in Task 3**; Task 6 adds it via migration. Update `profit`/`net_total` accordingly (they don't depend on reservation).
`SaleForm(forms.ModelForm)`: fields customer, shipment, kg, price, date, debt_deadline, note. Limit `shipment` queryset to arrived lots with available_kg>0. `clean()`: kg>0; kg ≤ shipment.available_kg (+own old kg on edit if same shipment); shipment must be arrived. On save (view), snapshot `cost_price = shipment.landed_cost_per_kg`. `sale_create` accepts `?lot=<pk>` and `?customer=<pk>` initial. Views admin-only, modal convention, target_type "Sotuv". `sale_list` (search customer/brand/lot), `sale_detail` (facts + profit + returns section placeholder for Task 5 + payments/allocations placeholder for Task 4). Ombor "Sotish" button → `sale_create?lot=<pk>` data-modal, disabled when available_kg<=0. Nav "Sotuvlar".
- [ ] Step 4: makemigrations, tests green, full suite.
- [ ] Step 5: commit `feat: single-line sales from lots with cost snapshot`.

---

### Task 4: Customer payments + PaymentAllocation (FIFO + manual pick) + advances

**Files:** modify `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`, `templates/crm/sale_detail.html`, `templates/crm/customer_list.html` (a "To'lov" action); create `templates/crm/customer_payment_list.html`, `templates/crm/_payment_alloc_fields.html`; test `tests/test_customer_payments.py`, `tests/test_allocation.py`.

**Produces:** `CustomerPayment(customer FK PROTECT related_name="customer_payments", date, amount USD, currency, exchange_rate, amount_original, method, reservation FK SET_NULL null [earmark; Task 6 uses it — omit until Task 6 OR include nullable now with string ref to Reservation → same deferral choice as Task 3; DEFER earmark to Task 6], note, created_by)`; `PaymentAllocation(payment FK CASCADE related_name="allocations", sale FK CASCADE related_name="allocations", amount)`; functions `allocate_customer_payment(payment, picks=None)` and `apply_customer_advance(sale)`.

- [ ] Step 1: failing tests (`tests/test_allocation.py`):
  - Customer has 2 unpaid sales S1 $3,000 (older) and S2 $2,000. A $4,000 payment with no picks → FIFO: S1 fully allocated $3,000, S2 $1,000; S1.remaining 0, S2.remaining 1,000; customer.balance 1,000.
  - Overpay: sales total $5,000, pay $6,000 → $5,000 allocated across both, $1,000 stays unallocated (advance); customer.balance == −1,000 (avans).
  - Manual pick: pay $2,000, pick only S2 → S2 gets 2,000, S1 untouched.
  - Advance auto-applies: customer has an unallocated $1,000 payment (advance); create a new sale S3 $800 → `apply_customer_advance(S3)` allocates $800 from the advance; S3.remaining 0; remaining advance $200.
  - Per-sale allocation Σ never exceeds sale.net_total; per-payment Σ never exceeds payment.amount.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement models + `PaymentAllocation` + the two functions:
```python
def allocate_customer_payment(payment, picks=None):
    """Allocate a payment across the customer's outstanding sales. `picks` is an
    optional list of (sale_id, amount) chosen in the form; the rest (or all, if no
    picks) auto-fills oldest-first. Leftover stays unallocated = advance."""
    from django.db import transaction
    remaining = payment.amount - (payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0"))
    with transaction.atomic():
        if picks:
            for sale_id, amt in picks:
                sale = Sale.objects.get(pk=sale_id, customer=payment.customer)
                amt = min(Decimal(amt), sale.remaining, remaining)
                if amt > 0:
                    PaymentAllocation.objects.create(payment=payment, sale=sale, amount=amt)
                    remaining -= amt
        # FIFO the leftover across still-outstanding sales
        for sale in payment.customer.sales.order_by("date", "id"):
            if remaining <= 0: break
            take = min(sale.remaining, remaining)
            if take > 0:
                PaymentAllocation.objects.create(payment=payment, sale=sale, amount=take)
                remaining -= take
    return remaining  # the advance left over

def apply_customer_advance(sale):
    """Pull this customer's unallocated payment money (advance) onto a new sale,
    oldest payment first, until the sale is covered or the advance runs out."""
    from django.db import transaction
    with transaction.atomic():
        for payment in sale.customer.customer_payments.order_by("date", "id"):
            if sale.remaining <= 0: break
            unallocated = payment.amount - (payment.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0"))
            take = min(unallocated, sale.remaining)
            if take > 0:
                PaymentAllocation.objects.create(payment=payment, sale=sale, amount=take)
```
`CustomerPaymentForm(MoneyEntryFormMixin, ModelForm)` (fields customer, date, currency, amount, exchange_rate, method, note; save writes converted amount). The create view: after saving the payment, read optional manual picks from POST (a small allocation table listing the customer's outstanding sales with amount inputs) and call `allocate_customer_payment(payment, picks)`. `customer_payment_create` accepts `?customer=<pk>`. In Task 3's `sale_create`, after creating the sale call `apply_customer_advance(sale)` so pre-existing advances auto-apply. Audit target_type "Mijoz to'lovi", action PAYMENT. `customer_payment_list` (dated feed). Customer list "To'lov" action → `customer_payment_create?customer=<pk>`. sale_detail shows allocations (which payments paid it) + remaining. Nav "Mijoz to'lovlari".
- [ ] Step 4: makemigrations, tests green (allocation + modal-path), full suite.
- [ ] Step 5: commit `feat: customer payment ledger with FIFO+manual allocation and advances`.

---

### Task 5: Returns (Qaytarish) — credit debt, restock lot

**Files:** modify `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/crm/sale_detail.html`; create `templates/crm/_return_modal.html` (or reuse form.html); test `tests/test_returns.py`.

**Produces:** `Return(sale FK CASCADE related_name="returns", kg, price, date, restock bool default True, note, created_by)`; `Return.amount = kg*price`. URLs `return_create` (`?sale=<pk>`), `return_delete`.

- [ ] Step 1: failing tests — returning 1,000 kg of a 4,000 kg sale @ $1.60 credits $1,600: sale.net_total drops by 1,600, sale.remaining drops by 1,600, customer.balance drops by 1,600; with restock=True the lot.available_kg rises by 1,000 (and returned_kg reflects it); return kg cannot exceed sold kg; translator 403.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement. `Return` model (price defaults to the sale's price via form initial). `clean()`: kg>0, kg ≤ sale.kg − already-returned kg. View admin-only, modal, `?sale=<pk>` initial, target_type "Qaytarish", action RETURN; redirect/reload to `sale_detail`. sale_detail gets a returns table + "+ Qaytarish" data-modal action. Restock flows into `Shipment.returned_kg` (already handles `restock`). 
- [ ] Step 4: makemigrations, tests green, full suite.
- [ ] Step 5: commit `feat: sale returns crediting debt and restocking lots`.

---

### Task 6: Reservations (Bron) — block kg, earmark payments, convert to sale

**Files:** modify `crm/models.py` (Reservation model; add `Sale.reservation` FK + `CustomerPayment.reservation` FK via this migration), `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`, `templates/crm/ombor.html` (add "Bron"), `templates/crm/customer_list.html` (a "Bron" action), `templates/crm/sale_detail.html`; create `templates/crm/reservation_list.html`; test `tests/test_reservations.py`.

**Produces:** `Reservation(customer FK PROTECT related_name="reservations", shipment FK PROTECT related_name="reservations", kg, price null, status [active|converted|cancelled] default active, note, created_by, created_at)`; adds `Sale.reservation` and `CustomerPayment.reservation` (earmark) FKs. URLs `reservation_list/create/cancel/convert`.

- [ ] Step 1: failing tests:
  - A reservation for 5,000 kg on a lot (arrived OR in-transit) with status active reduces that lot's `available_kg` by 5,000 (for arrived) / reservable amount; over-reserving beyond kg−sold−otherReserved is rejected.
  - Earmarked payment: a CustomerPayment with `reservation=R` is the customer's money set aside for R.
  - Convert-to-sale: converting R (arrived lot) creates a Sale for R.kg at R.price (or a passed price) with cost snapshot, marks R.status="converted", links `Sale.reservation=R`, and applies earmarked payments first then FIFO advance (via `apply_customer_advance` + earmark handling); the lot's reserved_kg drops (R no longer active) and sold_kg rises — net available unchanged.
  - Cancel: R.status="cancelled" frees the kg.
  - translator 403.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement `Reservation`, add the two FKs (migration), `ReservationForm` (customer, shipment [arrived or in-transit], kg, price optional, note), clean() blocks over-reservation. `reservation_convert` view: builds a Sale (kg, price from reservation.price or POST, cost_price = shipment.landed_cost_per_kg — requires arrived; block converting an un-arrived lot with a clear message), sets reservation converted + Sale.reservation, then `apply_customer_advance(sale)` — and prioritise earmarked payments: in `apply_customer_advance`, if the sale has a reservation, first pull unallocated money from payments whose `reservation==sale.reservation`, then general advances. Update `apply_customer_advance` accordingly (earmark-first ordering). Ombor "Bron" + customer "Bron" actions. Nav "Bronlar". Audit target_type "Bron".
- [ ] Step 4: makemigrations, tests green, full suite.
- [ ] Step 5: commit `feat: reservations blocking lot kg, earmarked payments, convert-to-sale`.

---

### Task 7: Debts page (Qarzlar)

**Files:** modify `crm/views.py`, `config/urls.py`, `templates/base.html`; create `templates/crm/debt_list.html`, `templates/crm/debt_customer.html`; test `tests/test_debts.py`.

**Produces:** URLs `debt_list` (customers with outstanding balance), `debt_customer` (`<pk>` — that customer's outstanding sales + pay action). Reuses Task 4 payment.

- [ ] Step 1: failing tests — a customer with remaining>0 appears in debt_list with the right total and an overdue flag when any sale is_overdue; a fully-paid customer does not; `debt_customer` lists their outstanding sales; translator 403.
- [ ] Step 2: FAIL.
- [ ] Step 3: implement. `debt_list`: customers whose `balance > 0`, showing total qarz, count of overdue sales (red). `debt_customer`: outstanding sales (remaining>0) with deadlines/overdue badges + a "To'lov" action → `customer_payment_create?customer=<pk>`. Nav "Qarzlar". Admin-only.
- [ ] Step 4: tests green, full suite.
- [ ] Step 5: commit `feat: debts page with overdue receivables`.

---

## Phase 2 exit criteria
- `pytest` green; `manage.py check` clean; `makemigrations --check` none pending.
- Manual: arrive a load → it appears in Ombor with landed cost → sell part to a customer (debt created, lot qoldiq drops) → take a customer payment (FIFO allocates, overpay → avans) → return some kg (debt credited, lot restocked) → reserve kg on an incoming load, earmark a payment, convert on arrival → debts page shows the receivable.
- Phase 3 plan written next.
