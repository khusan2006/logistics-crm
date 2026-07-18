# GranulaLog Phase 1 (Import Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the GranulaLog logistics CRM with auth/roles, partners, contracts, supplier payments (overpay-blocked), editable shipment statuses, shipments with permission-aware status flow, ETA delay tracking with overdue badges, shipment expenses with landed cost, and an admin dashboard.

**Architecture:** Standalone Django project copied structurally from `/Users/khusan/Desktop/client-crm` (same config layout, accounts app, base.html shell, app.css, audit-log pattern, function-based views, `data-modal` form flow). One `crm` app holds all domain models. All money is canonical USD; so'm entries are converted at entry with a stored rate.

**Tech Stack:** Django 6.0.6, PostgreSQL (psycopg 3), pytest + pytest-django (SQLite test settings), whitenoise, django-axes, openpyxl (later phases), vanilla JS + one `app.css`.

## Global Constraints

- Source of patterns: `/Users/khusan/Desktop/client-crm` — copy, then adapt; do not invent new UI systems. Target project root: `/Users/khusan/Desktop/logistic-crm`.
- UI language: Uzbek. Brand: **GranulaLog**. App name: `crm`, project package: `config`.
- Money: USD canonical. `MONEY` = `DecimalField(max_digits=14, decimal_places=2)`; unit prices `DecimalField(14, 4)`; kg `DecimalField(12, 3)`; original-currency amounts `DecimalField(18, 2)`.
- Roles: `admin` (everything), `translator` (shipments only, no money, cannot set the arrival status).
- Every mutating view writes an `AuditLog` row.
- Tests: `pytest` from the project root; settings `config.settings_test` (SQLite). No Playwright in Phase 1.
- Text files end with a newline. Follow client-crm code style (docstring tone, Uzbek verbose_names).
- Commit after every task (git repo created in Task 1).

---

### Task 1: Project skeleton copied from client-crm + roles + auth smoke test

**Files:**
- Create: `manage.py`, `config/{__init__,settings,settings_test,urls,wsgi,asgi}.py`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `conftest.py`, `.env`, `.env.example`, `.gitignore`, `accounts/` (whole app), `crm/` (empty app skeleton), `templates/base.html`, `templates/_modal.html`, `templates/_confirm_modal.html`, `templates/accounts/` (login templates), `templates/crm/form.html`, `templates/crm/confirm_delete.html`, `static/css/app.css`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `accounts.models.User` with `Role.ADMIN = "admin"`, `Role.TRANSLATOR = "translator"`, property `is_admin_role`; `accounts.decorators.role_required(*roles)`; conftest fixtures `admin_user`, `translator_user`, `admin_client`, `translator_client` (logged-in Django test clients), constant `PASSWORD`.
- URL names produced: `login`, `logout`, `dashboard` (placeholder view for now).

- [ ] **Step 1: Copy the skeleton from client-crm**

```bash
cd /Users/khusan/Desktop/logistic-crm
SRC=/Users/khusan/Desktop/client-crm
mkdir -p config accounts crm/migrations templates/crm templates/accounts static/css tests
cp $SRC/manage.py .
cp $SRC/config/__init__.py $SRC/config/settings.py $SRC/config/settings_test.py $SRC/config/urls.py $SRC/config/wsgi.py $SRC/config/asgi.py config/
cp $SRC/requirements.txt $SRC/requirements-dev.txt $SRC/.gitignore $SRC/.env.example .
cp -R $SRC/accounts/. accounts/
rm -rf accounts/__pycache__ accounts/migrations/__pycache__
cp $SRC/templates/base.html $SRC/templates/_modal.html $SRC/templates/_confirm_modal.html templates/
cp -R $SRC/templates/accounts/. templates/accounts/
cp $SRC/templates/crm/form.html $SRC/templates/crm/confirm_delete.html templates/crm/
cp $SRC/static/css/app.css static/css/
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

If `requirements-dev.txt` contains Playwright extras, keep them installed but unused. Ensure `pytest` and `pytest-django` are present in `requirements-dev.txt`; add them if missing.

- [ ] **Step 2: Adapt the copied files**

1. `accounts/models.py` — replace the Role choices and helpers with:

```python
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        TRANSLATOR = "translator", "Tarjimon"

    role = models.CharField("Rol", max_length=12, choices=Role.choices, default=Role.TRANSLATOR)
    phone = models.CharField("Telefon", max_length=30, blank=True)

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN

    def __str__(self):
        return self.get_full_name() or self.username
```

2. Delete `accounts/migrations/*.py` (keep `__init__.py`) — we regenerate fresh migrations.
3. `accounts/views.py` / `accounts/forms.py`: keep LoginView; delete or comment out user-management views that reference removed roles (`user_list`, `user_create`, `user_edit` come back in Phase 3) — remove their imports/URLs for now.
4. `config/settings.py`: change DB defaults `POSTGRES_DB/USER` to `granulalog`; delete the `OMBOR_START_DATE` block; keep everything else (axes, whitenoise, `LANGUAGE_CODE = "uz"`, `TIME_ZONE = "Asia/Tashkent"`, security block).
5. `config/urls.py`: strip to admin, login/logout, and a `dashboard` placeholder:

```python
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views as accounts_views
from crm import views as crm_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", accounts_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", crm_views.dashboard, name="dashboard"),
]
```

6. Create `crm/__init__.py`, `crm/apps.py` (`name = "crm"`, `verbose_name = "GranulaLog"`), `crm/migrations/__init__.py`, `crm/models.py` (empty for now), and `crm/views.py`:

```python
from django.shortcuts import render


def dashboard(request):
    return render(request, "crm/dashboard.html")
```

7. Create `templates/crm/dashboard.html`:

```html
{% extends "base.html" %}
{% block title %}Dashboard · GranulaLog{% endblock %}
{% block topbar_title %}Dashboard{% endblock %}
{% block content %}<p class="muted">GranulaLog ishga tushdi.</p>{% endblock %}
```

8. `templates/base.html`: replace brand text `Paket CRM` → `GranulaLog`; replace the whole `<nav class="sidebar-nav">` body with just the Dashboard link for now (Tasks 3–11 add items); delete nav links whose URL names no longer exist (otherwise every page 500s on `{% url %}`). Keep topbar, theme toggle, logout form, modal/JS blocks untouched.
9. `pytest.ini`:

```ini
[pytest]
DJANGO_SETTINGS_MODULE = config.settings_test
python_files = tests.py test_*.py *_tests.py
```

10. `conftest.py` (replaces the copied one entirely):

```python
"""Shared fixtures: an admin and a translator, plus logged-in test clients."""
import pytest

from accounts.models import User

PASSWORD = "test-pass-123"


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="boss", password=PASSWORD, role=User.Role.ADMIN,
        first_name="Bosh", last_name="Admin",
    )


@pytest.fixture
def translator_user(db):
    return User.objects.create_user(
        username="tarjimon", password=PASSWORD, role=User.Role.TRANSLATOR,
        first_name="Tar", last_name="Jimon",
    )


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def translator_client(client, translator_user):
    client.force_login(translator_user)
    return client
```

11. `.env` (dev): copy `.env.example`, set `SECRET_KEY=dev-only-secret`, `DEBUG=True`, `POSTGRES_DB=granulalog`, `POSTGRES_USER=granulalog`.
12. `settings.py` INSTALLED_APPS: ensure `"accounts", "crm", "axes"` present (crm replaces any old app label).

- [ ] **Step 3: Write the failing smoke test**

`tests/test_auth.py`:

```python
from accounts.models import User


def test_roles_exist(db):
    u = User.objects.create_user(username="x", password="p", role=User.Role.TRANSLATOR)
    assert not u.is_admin_role
    assert User.objects.create_user(username="y", password="p", role=User.Role.ADMIN).is_admin_role


def test_login_required_redirects(client, db):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.url.startswith("/login/")


def test_admin_sees_dashboard(admin_client):
    resp = admin_client.get("/")
    assert resp.status_code == 200
    assert "GranulaLog" in resp.content.decode()
```

- [ ] **Step 4: Generate migrations, run tests until green**

```bash
.venv/bin/python manage.py makemigrations accounts crm
.venv/bin/pytest tests/test_auth.py -v
```
Expected: 3 passed. Fix template/url leftovers from the copy until green (typical failures: `{% url %}` to removed names, imports of deleted account views).

- [ ] **Step 5: Init repo and commit**

```bash
git init && git add -A && git commit -m "feat: GranulaLog skeleton copied from client-crm with admin/translator roles"
```

---

### Task 2: Audit log

**Files:**
- Create: `crm/models.py` (AuditLog + shared constants), `templates/crm/audit_list.html`
- Modify: `crm/views.py`, `config/urls.py`, `templates/base.html`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces: `AuditLog.record(user, action, target_type, target_id, summary)`; `AuditLog.Action` choices `CREATE/UPDATE/DELETE/STATUS/PAYMENT`; module constants `MONEY`, `QTY`, `PayMethod`, `Currency` used by all later tasks; URL `audit_list`.

- [ ] **Step 1: Write the failing test**

`tests/test_audit.py`:

```python
from crm.models import AuditLog


def test_record_writes_row(admin_user):
    AuditLog.record(admin_user, AuditLog.Action.CREATE, "Hamkor", 7, "Yangi hamkor: Pars")
    row = AuditLog.objects.get()
    assert row.user == admin_user and row.target_id == 7 and "Pars" in row.summary


def test_audit_page_admin_only(admin_client, translator_client):
    assert admin_client.get("/audit/").status_code == 200
    assert translator_client.get("/audit/").status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_audit.py -v` — Expected: FAIL (ImportError: AuditLog).

- [ ] **Step 3: Implement**

Top of `crm/models.py`:

```python
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import DecimalField, Sum
from django.utils import timezone

MONEY = DecimalField(max_digits=14, decimal_places=2)   # USD
QTY = DecimalField(max_digits=12, decimal_places=3)     # kg


class PayMethod(models.TextChoices):
    CASH = "cash", "Naqd"
    CARD = "card", "Karta"
    TRANSFER = "transfer", "Bank o'tkazmasi"


class Currency(models.TextChoices):
    USD = "usd", "Dollar"
    UZS = "uzs", "So'm"


class AuditLog(models.Model):
    """Append-only trail of money- and status-relevant actions (client-crm pattern)."""

    class Action(models.TextChoices):
        CREATE = "create", "Qo'shildi"
        UPDATE = "update", "O'zgartirildi"
        DELETE = "delete", "O'chirildi"
        STATUS = "status", "Holat o'zgardi"
        PAYMENT = "payment", "To'lov"

    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="audit_logs", verbose_name="Kim",
    )
    action = models.CharField("Amal", max_length=10, choices=Action.choices)
    target_type = models.CharField("Obyekt", max_length=40)
    target_id = models.IntegerField("ID", null=True, blank=True)
    summary = models.CharField("Tafsilot", max_length=255)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit yozuvi"
        verbose_name_plural = "Audit jurnali"

    @classmethod
    def record(cls, user, action, target_type, target_id, summary):
        return cls.objects.create(user=user, action=action, target_type=target_type,
                                  target_id=target_id, summary=summary)

    def __str__(self):
        return f"{self.get_action_display()} · {self.target_type} · {self.summary}"
```

`crm/views.py` additions:

```python
from django.core.paginator import Paginator

from accounts.decorators import role_required
from accounts.models import User

from .models import AuditLog


@role_required(User.Role.ADMIN)
def audit_list(request):
    page = Paginator(AuditLog.objects.select_related("user"), 50).get_page(request.GET.get("page"))
    return render(request, "crm/audit_list.html", {"page": page})
```

`templates/crm/audit_list.html`:

```html
{% extends "base.html" %}
{% block title %}Audit · GranulaLog{% endblock %}
{% block topbar_title %}Audit jurnali{% endblock %}
{% block content %}
<div class="table-wrap"><table>
  <tr><th>Vaqt</th><th>Kim</th><th>Amal</th><th>Obyekt</th><th>Tafsilot</th></tr>
  {% for row in page %}
  <tr><td>{{ row.created_at|date:"d.m.Y H:i" }}</td><td>{{ row.user|default:"Tizim" }}</td>
      <td>{{ row.get_action_display }}</td><td>{{ row.target_type }} {% if row.target_id %}#{{ row.target_id }}{% endif %}</td>
      <td>{{ row.summary }}</td></tr>
  {% empty %}<tr><td colspan="5" class="muted">Yozuvlar yo'q</td></tr>{% endfor %}
</table></div>
{% endblock %}
```

`config/urls.py`: add `path("audit/", crm_views.audit_list, name="audit_list"),`.
`templates/base.html`: add a "Nazorat" nav group with the Audit link wrapped in `{% if user.is_admin_role %}`.
Run `.venv/bin/python manage.py makemigrations crm`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_audit.py -v` — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: audit log model, page and shared money constants"`

---

### Task 3: Partners (Hamkorlar) CRUD

**Files:**
- Modify: `crm/models.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`
- Create: `crm/forms.py`, `templates/crm/partner_list.html`
- Test: `tests/test_partners.py`

**Interfaces:**
- Produces: `Partner(name, phone, city, note, created_at)`; URLs `partner_list`, `partner_create`, `partner_edit`, `partner_delete`; the **modal CRUD convention used by ALL later CRUD tasks**, via helpers in `crm/utils.py` (adopted from client-crm during Task 3):
  - `create` view: GET → `form_response(request, form, title)`; invalid POST → `form_response(request, form, title, invalid=True)`; valid POST → save + `AuditLog.record(...)` + `messages.success(...)` + `form_success(request, reverse("<list>"))`.
  - `edit` view: same, but valid POST returns `form_reload(request, reverse("<list>"))` (in-place action; the opener page reloads).
  - `delete` view: GET → `render_confirm(request, title, message, "Ha, o'chirish", confirm_class="btn-danger", cancel_url_name="<list>")`; POST → try delete (catch `ProtectedError` → error message) + `form_reload(request, reverse("<list>"))`.
  - Helpers: `is_ajax`, `form_response(...modal_template="_modal.html")` (AJAX → `_modal.html` partial 200/422, else full `crm/form.html`), `form_success` (AJAX → 204 + `X-Redirect`, else redirect), `form_reload` (AJAX → 204, else redirect), `render_confirm` (AJAX → `_confirm_modal.html`, else `crm/confirm.html`; takes `cancel_url_name`).
  - Templates: `_modal.html`, `_confirm_modal.html` (top-level), `crm/form.html`, `crm/confirm.html`. The base template's `data-modal` JS fetches with `X-Requested-With: XMLHttpRequest` and injects the partial. Every CRUD list uses `data-modal` links for create/edit/delete.
  - Each CRUD test set must include modal-path tests: AJAX GET returns the partial (has `modal-head`, no `<html`); AJAX valid POST returns 204 + `X-Redirect`; AJAX invalid POST returns 422 + partial.

- [ ] **Step 1: Write the failing test**

`tests/test_partners.py`:

```python
import pytest

from crm.models import AuditLog, Partner


def test_create_partner(admin_client):
    resp = admin_client.post("/partners/new/", {
        "name": "Pars Polymer", "phone": "+98 912 440 1122", "city": "Tehron", "note": "",
    })
    assert resp.status_code == 302
    assert Partner.objects.filter(name="Pars Polymer").exists()
    assert AuditLog.objects.filter(target_type="Hamkor").exists()


def test_list_and_search(admin_client):
    Partner.objects.create(name="Arya Petrochem", phone="1", city="Shiroz")
    Partner.objects.create(name="Toshkent Polimer", phone="2", city="Toshkent")
    html = admin_client.get("/partners/?q=arya").content.decode()
    assert "Arya" in html and "Toshkent Polimer" not in html


def test_translator_forbidden(translator_client):
    assert translator_client.get("/partners/").status_code == 403
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_partners.py -v` → FAIL (404 / ImportError).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class Partner(models.Model):
    """Yetkazib beruvchi (supplier) in Iran or elsewhere."""

    name = models.CharField("Nomi", max_length=200)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    city = models.CharField("Shahar", max_length=100, blank=True)
    note = models.TextField("Izoh", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Hamkor"
        verbose_name_plural = "Hamkorlar"

    def __str__(self):
        return self.name
```

`crm/forms.py`:

```python
from django import forms

from .models import Partner


class PartnerForm(forms.ModelForm):
    class Meta:
        model = Partner
        fields = ["name", "phone", "city", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}
```

`crm/views.py`:

```python
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect

from .forms import PartnerForm
from .models import Partner


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
    if request.method == "POST" and form.is_valid():
        partner = form.save()
        AuditLog.record(request.user, AuditLog.Action.CREATE, "Hamkor", partner.pk, f"Yangi hamkor: {partner.name}")
        messages.success(request, "Hamkor qo'shildi")
        return redirect("partner_list")
    return render(request, "crm/form.html", {"form": form, "title": "Yangi hamkor"})


@role_required(User.Role.ADMIN)
def partner_edit(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    form = PartnerForm(request.POST or None, instance=partner)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.record(request.user, AuditLog.Action.UPDATE, "Hamkor", partner.pk, f"Hamkor tahrirlandi: {partner.name}")
        messages.success(request, "Hamkor yangilandi")
        return redirect("partner_list")
    return render(request, "crm/form.html", {"form": form, "title": "Hamkorni tahrirlash"})


@role_required(User.Role.ADMIN)
def partner_delete(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    if request.method == "POST":
        name = partner.name
        try:
            partner.delete()
        except models.ProtectedError:
            messages.error(request, "Hamkorga kelishuv biriktirilgan — o'chirib bo'lmaydi")
            return redirect("partner_list")
        AuditLog.record(request.user, AuditLog.Action.DELETE, "Hamkor", pk, f"Hamkor o'chirildi: {name}")
        messages.success(request, "Hamkor o'chirildi")
        return redirect("partner_list")
    return render(request, "crm/confirm_delete.html", {"obj": partner, "title": "Hamkorni o'chirish"})
```

(`from django.db import models` is already imported in views via models module — import `ProtectedError` from `django.db.models` explicitly.)

`config/urls.py`: add

```python
    path("partners/", crm_views.partner_list, name="partner_list"),
    path("partners/new/", crm_views.partner_create, name="partner_create"),
    path("partners/<int:pk>/edit/", crm_views.partner_edit, name="partner_edit"),
    path("partners/<int:pk>/delete/", crm_views.partner_delete, name="partner_delete"),
```

`templates/crm/partner_list.html` — follow client-crm's `client_list.html` structure exactly (searchbar form with `q`, `table-wrap` table, `data-modal` icon actions, fab button "Yangi hamkor" → `partner_create`). Columns: Nomi, Telefon, Shahar, Izoh, actions (edit `data-modal`, delete `data-modal`).
`templates/base.html`: add nav item Hamkorlar (inside the `{% if user.is_admin_role %}` group).

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_partners.py -v` → PASS. Also `makemigrations` before running.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: partners CRUD"`

---

### Task 4: Contracts (Kelishuvlar) with progress numbers

**Files:**
- Modify: `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`
- Create: `templates/crm/contract_list.html`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Consumes: `Partner`, CRUD/view conventions from Task 3.
- Produces: `Contract(partner, brand, kg, price, created, deadline, note, created_by)` with properties `total_value`, `shipped_kg`, `remaining_kg`, `paid_total`, `debt`; URLs `contract_list/create/edit/delete`. `shipped_kg`/`paid_total` read the `shipments` / `supplier_payments` reverse relations that Tasks 5 and 7 create — until then they return 0 via `getattr` guard (see code).

- [ ] **Step 1: Write the failing test**

`tests/test_contracts.py`:

```python
from decimal import Decimal

from crm.models import Contract, Partner


def _contract(**kw):
    partner = kw.pop("partner", None) or Partner.objects.create(name="Pars", phone="1", city="Tehron")
    defaults = dict(partner=partner, brand="LLDPE 209AA", kg=Decimal("50000"),
                    price=Decimal("0.96"), created="2026-07-01", deadline="2026-07-28")
    defaults.update(kw)
    return Contract.objects.create(**defaults)


def test_total_value(db):
    c = _contract()
    assert c.total_value == Decimal("48000.00")
    assert c.debt == Decimal("48000.00")
    assert c.remaining_kg == Decimal("50000")


def test_create_via_view(admin_client, admin_user):
    p = Partner.objects.create(name="Arya", phone="1", city="Shiroz")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "brand": "HDPE 7000F", "kg": "30000", "price": "1.05",
        "created": "2026-07-04", "deadline": "2026-08-05", "note": "",
    })
    assert resp.status_code == 302
    c = Contract.objects.get(brand="HDPE 7000F")
    assert c.created_by == admin_user


def test_deadline_before_created_rejected(admin_client):
    p = Partner.objects.create(name="X", phone="1", city="Y")
    resp = admin_client.post("/contracts/new/", {
        "partner": p.pk, "brand": "B", "kg": "10", "price": "1",
        "created": "2026-07-10", "deadline": "2026-07-01", "note": "",
    })
    assert resp.status_code == 200 and not Contract.objects.exists()
```

- [ ] **Step 2: Run to verify failure** — FAIL (ImportError: Contract).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class Contract(models.Model):
    """Kelishuv: one brand of granula from one partner at one USD/kg price."""

    partner = models.ForeignKey(Partner, on_delete=models.PROTECT,
                                related_name="contracts", verbose_name="Hamkor")
    brand = models.CharField("Granula markasi", max_length=100)
    kg = models.DecimalField("Kelishilgan kg", max_digits=12, decimal_places=3)
    price = models.DecimalField("1 kg narxi (USD)", max_digits=14, decimal_places=4)
    created = models.DateField("Kelishuv sanasi", default=timezone.localdate)
    deadline = models.DateField("Yetkazish muddati")
    note = models.TextField("Izoh", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="contracts",
                                   verbose_name="Kim ochdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]
        verbose_name = "Kelishuv"
        verbose_name_plural = "Kelishuvlar"

    @property
    def total_value(self):
        return (self.kg * self.price).quantize(Decimal("0.01"))

    @property
    def shipped_kg(self):
        if not hasattr(self, "shipments"):  # relation lands in Task 7
            return Decimal("0")
        return self.shipments.aggregate(s=Sum("kg"))["s"] or Decimal("0")

    @property
    def remaining_kg(self):
        return self.kg - self.shipped_kg

    @property
    def paid_total(self):
        if not hasattr(self, "supplier_payments"):  # relation lands in Task 5
            return Decimal("0")
        return self.supplier_payments.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def debt(self):
        return self.total_value - self.paid_total

    def __str__(self):
        return f"#{self.pk} · {self.brand} · {self.partner}"
```

`crm/forms.py`:

```python
from .models import Contract


class ContractForm(forms.ModelForm):
    class Meta:
        model = Contract
        fields = ["partner", "brand", "kg", "price", "created", "deadline", "note"]
        widgets = {
            "created": forms.DateInput(attrs={"type": "date"}),
            "deadline": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned = super().clean()
        created, deadline = cleaned.get("created"), cleaned.get("deadline")
        if created and deadline and deadline < created:
            self.add_error("deadline", "Muddat kelishuv sanasidan oldin bo'la olmaydi")
        kg = cleaned.get("kg")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        # Shrinking below already-shipped kg is invalid (matters from Task 7 on).
        if self.instance.pk and kg is not None and kg < self.instance.shipped_kg:
            self.add_error("kg", "Kelishilgan kg yuborilgan kg dan kam bo'la olmaydi")
        return cleaned
```

`crm/views.py` — `contract_list` (search over brand/partner name/id, paginate 30, pass page), `contract_create/edit/delete` following the Task 3 convention verbatim with: `target_type="Kelishuv"`, on create set `contract = form.save(commit=False); contract.created_by = request.user; contract.save()`. Delete protected by `ProtectedError` (payments/shipments FK) with message "Kelishuvga to'lov yoki yuk biriktirilgan".

`templates/crm/contract_list.html` columns: `#ID`, Sana, Hamkor, Marka, Kelishilgan kg, Narx, Jami (`total_value`), Yuborilgan/Qolgan (`shipped_kg` / `remaining_kg`), To'langan/Qarz (`paid_total` / `debt`), Muddat, actions. Same searchbar/fab/data-modal pattern as Task 3.

URLs: `contracts/`, `contracts/new/`, `contracts/<int:pk>/edit/`, `contracts/<int:pk>/delete/` named `contract_list/create/edit/delete`. Nav item "Kelishuvlar".

- [ ] **Step 4: Run** — `makemigrations` + `.venv/bin/pytest tests/test_contracts.py -v` → PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat: contracts CRUD with derived totals"` (use `git add -A` first for new files).

---

### Task 5: Supplier payments, overpay blocked, so'm→USD entry

**Files:**
- Modify: `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`
- Create: `templates/crm/supplier_payment_list.html`
- Test: `tests/test_supplier_payments.py`

**Interfaces:**
- Consumes: `Contract` (Task 4), `PayMethod`, `Currency`, `MONEY` (Task 2).
- Produces: `SupplierPayment` (related_name `supplier_payments`); `MoneyEntryFormMixin` reused by Task 10's expense form — contract: fields `currency`, `amount`, `exchange_rate` on the form; after `clean()`, `cleaned_data["amount"]` is canonical USD, `amount_original`/`exchange_rate` populated; URLs `supplier_payment_list/create/edit/delete`.

- [ ] **Step 1: Write the failing test**

`tests/test_supplier_payments.py`:

```python
from decimal import Decimal

from crm.models import Contract, Partner, SupplierPayment


def _contract(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="Tehron")
    return Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                   price=Decimal("1.00"), created="2026-07-01",
                                   deadline="2026-07-28")


def test_payment_reduces_debt(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "400",
        "exchange_rate": "", "method": "transfer", "note": "",
    })
    assert resp.status_code == 302
    assert c.debt == Decimal("600.00")


def test_overpay_blocked(admin_client, db):
    c = _contract(db)
    resp = admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "1500",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 200 and not SupplierPayment.objects.exists()


def test_uzs_converted_to_usd(admin_client, db):
    c = _contract(db)
    admin_client.post("/supplier-payments/new/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "uzs", "amount": "1265000",
        "exchange_rate": "12650", "method": "cash", "note": "",
    })
    p = SupplierPayment.objects.get()
    assert p.amount == Decimal("100.00")
    assert p.amount_original == Decimal("1265000")
    assert p.exchange_rate == Decimal("12650")


def test_edit_excludes_own_amount_from_debt_check(admin_client, db):
    c = _contract(db)
    p = SupplierPayment.objects.create(contract=c, date="2026-07-02", amount=Decimal("1000"),
                                       amount_original=Decimal("1000"), method="cash")
    resp = admin_client.post(f"/supplier-payments/{p.pk}/edit/", {
        "contract": c.pk, "date": "2026-07-02", "currency": "usd", "amount": "900",
        "exchange_rate": "", "method": "cash", "note": "",
    })
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.amount == Decimal("900.00")
```

- [ ] **Step 2: Run to verify failure** — FAIL (ImportError: SupplierPayment).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class SupplierPayment(models.Model):
    """To'lov to one supplier contract. `amount` is always USD; a so'm payment is
    converted at entry and keeps its original figure + rate. Overpaying a contract
    is blocked at the form layer (per-contract model, no supplier prepayments)."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="supplier_payments", verbose_name="Kelishuv")
    date = models.DateField("Sana", default=timezone.localdate)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=0)
    amount_original = models.DecimalField("Asl summa (valyutada)", max_digits=18,
                                          decimal_places=2, default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.TRANSFER)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="supplier_payments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Hamkor to'lovi"
        verbose_name_plural = "Hamkor to'lovlari"

    def __str__(self):
        return f"{self.contract_id} · {self.amount}$ ({self.date})"
```

`crm/forms.py`:

```python
from decimal import ROUND_HALF_UP, Decimal

from .models import Currency, SupplierPayment


class MoneyEntryFormMixin:
    """Shared so'm→USD conversion. The user types `amount` in `currency`; after
    clean(), cleaned_data["amount"] is canonical USD and amount_original/exchange_rate
    carry the entry-time facts. Reused by the expense form."""

    def clean(self):
        cleaned = super().clean()
        currency, amount = cleaned.get("currency"), cleaned.get("amount")
        rate = cleaned.get("exchange_rate") or Decimal("0")
        if amount is None:
            return cleaned
        if amount <= 0:
            self.add_error("amount", "Summa musbat bo'lishi kerak")
            return cleaned
        if currency == Currency.UZS:
            if rate <= 0:
                self.add_error("exchange_rate", "So'mdagi summa uchun dollar kursini kiriting")
                return cleaned
            cleaned["amount_original"] = amount
            cleaned["amount"] = (amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            cleaned["amount_original"] = amount
            cleaned["exchange_rate"] = Decimal("0")
        return cleaned


class SupplierPaymentForm(MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = SupplierPayment
        fields = ["contract", "date", "currency", "amount", "exchange_rate", "method", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def clean(self):
        cleaned = super().clean()
        contract, amount = cleaned.get("contract"), cleaned.get("amount")
        if contract and amount is not None and not self.errors:
            debt = contract.debt
            if self.instance.pk and self.instance.contract_id == contract.pk:
                debt += self.instance.amount
            if amount > debt:
                self.add_error("amount", f"Ortiqcha to'lovga ruxsat berilmaydi (qarz: {debt} $)")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.amount = self.cleaned_data["amount"]
        obj.amount_original = self.cleaned_data["amount_original"]
        obj.exchange_rate = self.cleaned_data["exchange_rate"]
        if commit:
            obj.save()
        return obj
```

`crm/views.py` — `supplier_payment_list` (paginated, `select_related("contract__partner")`, optional `?contract=` filter), `supplier_payment_create/edit/delete` per the Task 3 convention (`target_type="Hamkor to'lovi"`, action `PAYMENT` on create; set `created_by` on create). The create view accepts `?contract=<pk>` to preselect via `initial`.

URLs: `supplier-payments/` + `new/ / <pk>/edit/ / <pk>/delete/`, names `supplier_payment_list/create/edit/delete`. Nav item "To'lovlar (hamkor)". Template columns: Sana, Kelishuv (#id · marka), Hamkor, Summa ($, plus original so'm small print when `currency == "uzs"`), Usul, Izoh, actions. Contract list rows get a "To'lov" quick action linking `{% url 'supplier_payment_create' %}?contract={{ c.pk }}`.

- [ ] **Step 4: Run** — `makemigrations` + `.venv/bin/pytest tests/test_supplier_payments.py tests/test_contracts.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: supplier payments with overpay guard and som->usd entry"`

---

### Task 6: Editable shipment statuses

**Files:**
- Modify: `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`
- Create: `templates/crm/status_list.html`, data migration `crm/migrations/000X_seed_statuses.py`
- Test: `tests/test_statuses.py`

**Interfaces:**
- Consumes: view conventions (Task 3).
- Produces: `ShipmentStatus(name, order, is_arrival)` ordered by `order`; save() keeps exactly one `is_arrival`; seeded six mockup statuses; helper `ShipmentStatus.arrival()` classmethod; URLs `status_list/create/edit/delete/move`.

- [ ] **Step 1: Write the failing test**

`tests/test_statuses.py`:

```python
import pytest

from crm.models import ShipmentStatus


def test_seed_exists(db):
    names = list(ShipmentStatus.objects.values_list("name", flat=True))
    assert names == ["Tayyorlanmoqda", "Yuklanmoqda", "Yo'lda", "Chegarada", "Bojxona",
                     "Omborga yetib keldi"]
    assert ShipmentStatus.arrival().name == "Omborga yetib keldi"


def test_only_one_arrival(db):
    s = ShipmentStatus.objects.get(name="Bojxona")
    s.is_arrival = True
    s.save()
    assert ShipmentStatus.objects.filter(is_arrival=True).count() == 1
    assert ShipmentStatus.arrival() == s


def test_arrival_delete_blocked(admin_client, db):
    arrival = ShipmentStatus.arrival()
    admin_client.post(f"/statuses/{arrival.pk}/delete/")
    assert ShipmentStatus.objects.filter(pk=arrival.pk).exists()


def test_reorder(admin_client, db):
    first = ShipmentStatus.objects.first()
    admin_client.post(f"/statuses/{first.pk}/move/", {"dir": "down"})
    assert ShipmentStatus.objects.first() != first
```

- [ ] **Step 2: Run to verify failure** — FAIL (ImportError).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class ShipmentStatus(models.Model):
    """Admin-editable ordered status chain. Exactly one row is the arrival status —
    reaching it is what turns a shipment into a warehouse lot, so it is protected:
    saving another row as arrival demotes the rest, and the arrival row can't be
    deleted (guarded in the view)."""

    name = models.CharField("Nomi", max_length=100, unique=True)
    order = models.PositiveSmallIntegerField("Tartib", default=0)
    is_arrival = models.BooleanField("Omborga kelish holati", default=False)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Yuk holati"
        verbose_name_plural = "Yuk holatlari"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_arrival:
            ShipmentStatus.objects.exclude(pk=self.pk).update(is_arrival=False)

    @classmethod
    def arrival(cls):
        return cls.objects.filter(is_arrival=True).first()

    def __str__(self):
        return self.name
```

Data migration (after `makemigrations crm` for the model, run `python manage.py makemigrations crm --empty -n seed_statuses` and fill):

```python
from django.db import migrations

NAMES = ["Tayyorlanmoqda", "Yuklanmoqda", "Yo'lda", "Chegarada", "Bojxona",
         "Omborga yetib keldi"]


def seed(apps, schema_editor):
    ShipmentStatus = apps.get_model("crm", "ShipmentStatus")
    for i, name in enumerate(NAMES, start=1):
        ShipmentStatus.objects.get_or_create(
            name=name, defaults={"order": i, "is_arrival": name == NAMES[-1]})


class Migration(migrations.Migration):
    dependencies = [("crm", "000X_shipmentstatus")]  # fill real previous migration name
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
```

`crm/forms.py`: `ShipmentStatusForm` (fields `name`, `is_arrival`).
`crm/views.py`: `status_list` (all statuses + create/edit forms via `crm/form.html` modals), `status_create` (sets `order = max+1`), `status_edit`, `status_delete` (block when `is_arrival` — message "Omborga kelish holatini o'chirib bo'lmaydi" — or when `status.shipments.exists()` after Task 7; wrap in try/ProtectedError), `status_move` (POST `dir=up|down`: swap `order` with neighbor). All `@role_required(User.Role.ADMIN)`; audit every change (`target_type="Holat"`).
`templates/crm/status_list.html`: ordered table Nomi / Tartib (↑↓ POST mini-forms) / Omborga kelish badge / actions. Nav item "Holatlar" in a "Boshqaruv" admin group.
URLs: `statuses/`, `statuses/new/`, `statuses/<int:pk>/edit/`, `statuses/<int:pk>/delete/`, `statuses/<int:pk>/move/`.

- [ ] **Step 4: Run** — migrations + `.venv/bin/pytest tests/test_statuses.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: editable shipment statuses with seeded chain and single arrival flag"`

---

### Task 7: Shipments (Yuklar) CRUD

**Files:**
- Modify: `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/base.html`
- Create: `templates/crm/shipment_list.html`
- Test: `tests/test_shipments.py`

**Interfaces:**
- Consumes: `Contract` (Task 4), `ShipmentStatus` (Task 6).
- Produces: `Shipment(contract, kg, status, sent, eta, arrived, transport, container, note, created_by)` with `is_overdue`, `days_late`; list URL `shipment_list` reachable by BOTH roles; admin-only `shipment_create/edit/delete`. Task 8 adds `shipment_set_status`; Task 9 `shipment_extend`; Task 10 expenses + `landed_cost_per_kg`.

- [ ] **Step 1: Write the failing test**

`tests/test_shipments.py`:

```python
from datetime import date, timedelta
from decimal import Decimal

from crm.models import Contract, Partner, Shipment, ShipmentStatus


def _contract(kg="1000"):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    return Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal(kg),
                                   price=Decimal("1.00"), created="2026-07-01",
                                   deadline="2026-08-01")


def _post_shipment(client, contract, **extra):
    data = {"contract": contract.pk, "kg": "400",
            "status": ShipmentStatus.objects.first().pk, "sent": "2026-07-05",
            "eta": "2026-07-20", "transport": "01A111AA", "container": "MSCU-1",
            "note": ""}
    data.update(extra)
    return client.post("/shipments/new/", data)


def test_create_and_contract_progress(admin_client, db):
    c = _contract()
    assert _post_shipment(admin_client, c).status_code == 302
    assert c.shipped_kg == Decimal("400.000")
    assert c.remaining_kg == Decimal("600.000")


def test_kg_over_contract_blocked(admin_client, db):
    c = _contract(kg="300")
    resp = _post_shipment(admin_client, c)
    assert resp.status_code == 200 and not Shipment.objects.exists()


def test_container_unique(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c)
    resp = _post_shipment(admin_client, c, kg="100", container="mscu-1")
    assert Shipment.objects.count() == 1 and resp.status_code == 200


def test_overdue(db, admin_user):
    c = _contract()
    s = Shipment.objects.create(contract=c, kg=Decimal("100"),
                                status=ShipmentStatus.objects.first(),
                                eta=date.today() - timedelta(days=3))
    assert s.is_overdue and s.days_late == 3


def test_translator_sees_list_but_cannot_create(translator_client, db):
    assert translator_client.get("/shipments/").status_code == 200
    c = _contract()
    assert _post_shipment(translator_client, c).status_code == 403
```

- [ ] **Step 2: Run to verify failure** — FAIL (ImportError: Shipment).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class Shipment(models.Model):
    """Yuk: one load moving under a contract. Once it reaches the arrival status
    (arrived date set) it doubles as a warehouse lot in Phase 2."""

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT,
                                 related_name="shipments", verbose_name="Kelishuv")
    kg = models.DecimalField("Yuborilgan kg", max_digits=12, decimal_places=3)
    status = models.ForeignKey(ShipmentStatus, on_delete=models.PROTECT,
                               related_name="shipments", verbose_name="Holat")
    sent = models.DateField("Jo'natilgan sana", null=True, blank=True)
    eta = models.DateField("Taxminiy kelish", null=True, blank=True)
    arrived = models.DateField("Yetib kelgan sana", null=True, blank=True)
    transport = models.CharField("Transport raqami", max_length=50, blank=True)
    container = models.CharField("Konteyner raqami", max_length=50, blank=True)
    note = models.TextField("Izoh", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipments",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Yuk"
        verbose_name_plural = "Yuklar"

    @property
    def is_overdue(self):
        return self.arrived is None and self.eta is not None and self.eta < timezone.localdate()

    @property
    def days_late(self):
        return (timezone.localdate() - self.eta).days if self.is_overdue else 0

    @property
    def days_left(self):
        if self.arrived or not self.eta:
            return None
        return (self.eta - timezone.localdate()).days

    def __str__(self):
        return f"Yuk #{self.pk} · {self.contract.brand} · {self.kg} kg"
```

`crm/forms.py`:

```python
from .models import Shipment


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["contract", "kg", "status", "sent", "eta", "transport", "container", "note"]
        widgets = {
            "sent": forms.DateInput(attrs={"type": "date"}),
            "eta": forms.DateInput(attrs={"type": "date"}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_container(self):
        container = (self.cleaned_data.get("container") or "").strip()
        if container:
            clash = Shipment.objects.filter(container__iexact=container)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError("Bu konteyner raqami avval kiritilgan")
        return container

    def clean(self):
        cleaned = super().clean()
        contract, kg = cleaned.get("contract"), cleaned.get("kg")
        sent, eta = cleaned.get("sent"), cleaned.get("eta")
        if kg is not None and kg <= 0:
            self.add_error("kg", "Kg musbat bo'lishi kerak")
        if contract and kg is not None and kg > 0:
            left = contract.remaining_kg
            if self.instance.pk and self.instance.contract_id == contract.pk:
                left += self.instance.kg
            if kg > left:
                self.add_error("kg", f"Yuk miqdori qolgan kg dan oshmasligi kerak ({left} kg)")
        if sent and eta and eta < sent:
            self.add_error("eta", "Kelish sanasi jo'natish sanasidan oldin bo'la olmaydi")
        return cleaned
```

`crm/views.py`:

```python
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
```

`shipment_create/edit/delete`: admin-only, Task 3 convention, `target_type="Yuk"`; on create, if the chosen status `is_arrival`, stamp `arrived = timezone.localdate()` before saving.

`templates/crm/shipment_list.html`: searchbar + a status `<select name="status">` filter (submit on change); columns: `#ID / Kelishuv`, Hamkor, Marka, Kg, Holat (a per-row `<form method="post" action="{% url 'shipment_set_status' s.pk %}">` select — wired for Task 8; render as plain badge until then), Transport/Konteyner, Jo'natilgan, Kelish (eta + badge: `days_late` red "N kun kechikdi" / `days_left` grey "N kun qoldi" / green "Yetib kelgan" when arrived), actions `{% if user.is_admin_role %}`edit/delete`{% endif %}`. **No money columns** — translators see this page.
Nav: "Yuklar" visible to both roles (`{% if user.is_authenticated %}` group separate from admin group).
URLs: `shipments/` + `new/ / <pk>/edit/ / <pk>/delete/`, names `shipment_list/create/edit/delete`.

- [ ] **Step 4: Run** — migrations + `.venv/bin/pytest tests/test_shipments.py tests/test_contracts.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: shipments CRUD with kg/container guards and overdue flags"`

---

### Task 8: Status transitions with translator rules

**Files:**
- Modify: `crm/views.py`, `config/urls.py`, `templates/crm/shipment_list.html`
- Test: `tests/test_status_flow.py`

**Interfaces:**
- Consumes: `Shipment`, `ShipmentStatus`, `AuditLog`.
- Produces: POST endpoint `shipment_set_status` (name used by list template): field `status` = status pk. Rules: translator may set any non-arrival status; only admin sets the arrival status; entering arrival stamps `arrived`, leaving clears it.

- [ ] **Step 1: Write the failing test**

`tests/test_status_flow.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from crm.models import AuditLog, Contract, Partner, Shipment, ShipmentStatus


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                       price=Decimal("1"), created="2026-07-01",
                                       deadline="2026-08-01")
    return Shipment.objects.create(contract=contract, kg=Decimal("500"),
                                   status=ShipmentStatus.objects.first())


def _set(client, shipment, status):
    return client.post(f"/shipments/{shipment.pk}/status/", {"status": status.pk})


def test_translator_moves_nonfinal(translator_client, shipment):
    target = ShipmentStatus.objects.get(name="Bojxona")
    assert _set(translator_client, shipment, target).status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == target
    assert AuditLog.objects.filter(action="status", target_id=shipment.pk).exists()


def test_translator_cannot_finish(translator_client, shipment):
    resp = _set(translator_client, shipment, ShipmentStatus.arrival())
    assert resp.status_code == 403
    shipment.refresh_from_db()
    assert not shipment.status.is_arrival


def test_admin_finish_stamps_arrival(admin_client, shipment):
    _set(admin_client, shipment, ShipmentStatus.arrival())
    shipment.refresh_from_db()
    assert shipment.status.is_arrival and shipment.arrived == date.today()


def test_leaving_arrival_clears_date(admin_client, shipment):
    _set(admin_client, shipment, ShipmentStatus.arrival())
    _set(admin_client, shipment, ShipmentStatus.objects.get(name="Bojxona"))
    shipment.refresh_from_db()
    assert shipment.arrived is None
```

- [ ] **Step 2: Run to verify failure** — FAIL (404).

- [ ] **Step 3: Implement**

`crm/views.py`:

```python
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_POST


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
```

URL: `path("shipments/<int:pk>/status/", crm_views.shipment_set_status, name="shipment_set_status"),`.
`templates/crm/shipment_list.html`: the Holat cell becomes a per-row POST form: `<select name="status" onchange="this.form.submit()">` over `statuses`, with the arrival option wrapped in `{% if user.is_admin_role %}` so translators don't even see it; include `{% csrf_token %}` and `<input type="hidden" name="next" value="{{ request.get_full_path }}">`.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_status_flow.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: permission-aware status transitions with arrival stamping"`

---

### Task 9: ETA extension with delay history

**Files:**
- Modify: `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/crm/shipment_list.html`
- Create: `templates/crm/shipment_detail.html`
- Test: `tests/test_delays.py`

**Interfaces:**
- Consumes: `Shipment` (Task 7).
- Produces: `ShipmentDelay(shipment, old_eta, new_eta, reason, created_by, created_at)` related_name `delays`; URLs `shipment_extend` (GET modal form / POST), `shipment_detail`. Both roles may extend; reason is mandatory.

- [ ] **Step 1: Write the failing test**

`tests/test_delays.py`:

```python
from datetime import date, timedelta
from decimal import Decimal

import pytest

from crm.models import Contract, Partner, Shipment, ShipmentDelay, ShipmentStatus


@pytest.fixture
def late_shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                       price=Decimal("1"), created="2026-07-01",
                                       deadline="2026-08-01")
    return Shipment.objects.create(contract=contract, kg=Decimal("500"),
                                   status=ShipmentStatus.objects.first(),
                                   eta=date.today() - timedelta(days=2))


def test_extend_requires_reason(translator_client, late_shipment):
    new_eta = (date.today() + timedelta(days=5)).isoformat()
    resp = translator_client.post(f"/shipments/{late_shipment.pk}/extend/",
                                  {"new_eta": new_eta, "reason": ""})
    assert resp.status_code == 200 and not ShipmentDelay.objects.exists()


def test_extend_saves_history_and_updates_eta(translator_client, late_shipment):
    old_eta = late_shipment.eta
    new_eta = date.today() + timedelta(days=5)
    resp = translator_client.post(f"/shipments/{late_shipment.pk}/extend/",
                                  {"new_eta": new_eta.isoformat(), "reason": "Chegarada navbat"})
    assert resp.status_code == 302
    late_shipment.refresh_from_db()
    assert late_shipment.eta == new_eta and not late_shipment.is_overdue
    delay = late_shipment.delays.get()
    assert delay.old_eta == old_eta and delay.reason == "Chegarada navbat"


def test_detail_shows_history(admin_client, late_shipment):
    admin_client.post(f"/shipments/{late_shipment.pk}/extend/",
                      {"new_eta": (date.today() + timedelta(days=3)).isoformat(),
                       "reason": "Bojxona tekshiruvi"})
    html = admin_client.get(f"/shipments/{late_shipment.pk}/").content.decode()
    assert "Bojxona tekshiruvi" in html
```

- [ ] **Step 2: Run to verify failure** — FAIL (ImportError: ShipmentDelay).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class ShipmentDelay(models.Model):
    """One ETA extension: the audit trail requirement — every push of the arrival
    date keeps its reason and author."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="delays", verbose_name="Yuk")
    old_eta = models.DateField("Avvalgi sana", null=True)
    new_eta = models.DateField("Yangi sana")
    reason = models.CharField("Kechikish sababi", max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipment_delays",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Yuk kechikishi"
        verbose_name_plural = "Yuk kechikishlari"

    def __str__(self):
        return f"{self.shipment_id}: {self.old_eta} → {self.new_eta}"
```

`crm/forms.py`:

```python
class ShipmentExtendForm(forms.Form):
    new_eta = forms.DateField(label="Yangi kelish sanasi",
                              widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Kechikish sababi", max_length=255)
```

`crm/views.py`:

```python
@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_extend(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    form = ShipmentExtendForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ShipmentDelay.objects.create(
            shipment=shipment, old_eta=shipment.eta,
            new_eta=form.cleaned_data["new_eta"], reason=form.cleaned_data["reason"],
            created_by=request.user)
        shipment.eta = form.cleaned_data["new_eta"]
        shipment.save(update_fields=["eta"])
        AuditLog.record(request.user, AuditLog.Action.UPDATE, "Yuk", shipment.pk,
                        f"Muddat uzaytirildi: {form.cleaned_data['new_eta']} ({form.cleaned_data['reason']})")
        messages.success(request, "Kelish sanasi uzaytirildi")
        return redirect("shipment_list")
    return render(request, "crm/form.html",
                  {"form": form, "title": f"Yuk #{shipment.pk} — muddatni uzaytirish"})


@role_required(User.Role.ADMIN, User.Role.TRANSLATOR)
def shipment_detail(request, pk):
    shipment = get_object_or_404(
        Shipment.objects.select_related("contract__partner", "status"), pk=pk)
    return render(request, "crm/shipment_detail.html", {"shipment": shipment})
```

URLs: `shipments/<int:pk>/` name `shipment_detail`; `shipments/<int:pk>/extend/` name `shipment_extend`.
`templates/crm/shipment_detail.html`: card with shipment facts (no money for translator; landed-cost/expenses section added in Task 10 inside `{% if user.is_admin_role %}`), delay-history table (Sana, Eski → Yangi, Sabab, Kim), buttons: "Muddatni uzaytirish" (`data-modal`), back link.
`templates/crm/shipment_list.html`: shipment id cell links to detail; add an "uzaytirish" icon-action (`data-modal`) next to the eta badge; show `⏱ {{ s.delays.count }}` marker when delays exist.

- [ ] **Step 4: Run** — migrations + `.venv/bin/pytest tests/test_delays.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: eta extension with mandatory reason and delay history"`

---

### Task 10: Shipment expenses and landed cost

**Files:**
- Modify: `crm/models.py`, `crm/forms.py`, `crm/views.py`, `config/urls.py`, `templates/crm/shipment_detail.html`
- Test: `tests/test_expenses.py`

**Interfaces:**
- Consumes: `Shipment`, `MoneyEntryFormMixin` (Task 5), `Currency`, `PayMethod`.
- Produces: `ShipmentExpense(shipment, date, category, amount, currency, exchange_rate, amount_original, method, note, created_by)` related_name `expenses`; `Shipment.expenses_total` and `Shipment.landed_cost_per_kg` (used by Phase 2 sales as the lot cost snapshot); URLs `expense_create` (`?shipment=<pk>`), `expense_edit`, `expense_delete`. Admin only.

- [ ] **Step 1: Write the failing test**

`tests/test_expenses.py`:

```python
from decimal import Decimal

import pytest

from crm.models import Contract, Partner, Shipment, ShipmentExpense, ShipmentStatus


@pytest.fixture
def shipment(db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    contract = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("20000"),
                                       price=Decimal("1.00"), created="2026-07-01",
                                       deadline="2026-08-01")
    return Shipment.objects.create(contract=contract, kg=Decimal("10000"),
                                   status=ShipmentStatus.objects.first())


def test_landed_cost(admin_client, shipment):
    for amount in ("1200", "800"):
        admin_client.post("/expenses/new/?shipment=%d" % shipment.pk, {
            "shipment": shipment.pk, "date": "2026-07-10", "category": "customs",
            "currency": "usd", "amount": amount, "exchange_rate": "",
            "method": "cash", "note": "",
        })
    assert shipment.expenses_total == Decimal("2000.00")
    # 1.00 + 2000/10000 = 1.20 per kg
    assert shipment.landed_cost_per_kg == Decimal("1.2000")


def test_no_expenses_landed_cost_is_contract_price(shipment):
    assert shipment.landed_cost_per_kg == Decimal("1.0000")


def test_translator_forbidden(translator_client, shipment):
    resp = translator_client.get("/expenses/new/?shipment=%d" % shipment.pk)
    assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify failure** — FAIL (ImportError: ShipmentExpense).

- [ ] **Step 3: Implement**

`crm/models.py`:

```python
class ShipmentExpense(models.Model):
    """Road/customs money spent on one load. Rolls into that load's landed cost:
    landed cost per kg = contract price + expenses ÷ kg (decision #3)."""

    class Category(models.TextChoices):
        CUSTOMS = "customs", "Bojxona"
        TRANSPORT = "transport", "Transport"
        ROAD = "road", "Yo'l xarajati"
        CERT = "cert", "Sertifikat"
        OTHER = "other", "Boshqa"

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE,
                                 related_name="expenses", verbose_name="Yuk")
    date = models.DateField("Sana", default=timezone.localdate)
    category = models.CharField("Turkum", max_length=10, choices=Category.choices,
                                default=Category.OTHER)
    amount = models.DecimalField("Summa (USD)", max_digits=14, decimal_places=2)
    currency = models.CharField("Valyuta", max_length=3, choices=Currency.choices,
                                default=Currency.USD)
    exchange_rate = models.DecimalField("Dollar kursi (1$ = so'm)", max_digits=12,
                                        decimal_places=2, default=0)
    amount_original = models.DecimalField("Asl summa (valyutada)", max_digits=18,
                                          decimal_places=2, default=0)
    method = models.CharField("To'lov usuli", max_length=8, choices=PayMethod.choices,
                              default=PayMethod.CASH)
    note = models.CharField("Izoh", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, related_name="shipment_expenses",
                                   verbose_name="Kim kiritdi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Yuk xarajati"
        verbose_name_plural = "Yuk xarajatlari"

    def __str__(self):
        return f"{self.get_category_display()}: {self.amount}$ (yuk #{self.shipment_id})"
```

Add to `Shipment`:

```python
    @property
    def expenses_total(self):
        return self.expenses.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def landed_cost_per_kg(self):
        """True cost of one kg in this load: contract price plus this load's own
        road/customs spend spread over its kg. Phase 2 snapshots this into sales."""
        extra = self.expenses_total / self.kg if self.kg else Decimal("0")
        return (self.contract.price + extra).quantize(Decimal("0.0001"))
```

`crm/forms.py`:

```python
from .models import ShipmentExpense


class ShipmentExpenseForm(MoneyEntryFormMixin, forms.ModelForm):
    class Meta:
        model = ShipmentExpense
        fields = ["shipment", "date", "category", "currency", "amount",
                  "exchange_rate", "method", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}),
                   "shipment": forms.HiddenInput()}

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.amount = self.cleaned_data["amount"]
        obj.amount_original = self.cleaned_data["amount_original"]
        obj.exchange_rate = self.cleaned_data["exchange_rate"]
        if commit:
            obj.save()
        return obj
```

`crm/views.py`: `expense_create` (admin-only; `initial={"shipment": request.GET.get("shipment")}`; on save set `created_by`, audit `target_type="Yuk xarajati"`, redirect to `shipment_detail` of the expense's shipment), `expense_edit`, `expense_delete` (same convention).
URLs: `expenses/new/`, `expenses/<int:pk>/edit/`, `expenses/<int:pk>/delete/`, names `expense_create/edit/delete`.
`templates/crm/shipment_detail.html`: inside `{% if user.is_admin_role %}` add an expenses card: table (Sana, Turkum, Summa $ + so'm small print, Usul, Izoh, actions), footer row "Jami: {{ shipment.expenses_total }} $ · Tan narx: {{ shipment.landed_cost_per_kg }} $/kg", fab-style "+ Xarajat" `data-modal` link to `{% url 'expense_create' %}?shipment={{ shipment.pk }}`.

- [ ] **Step 4: Run** — migrations + `.venv/bin/pytest tests/test_expenses.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: shipment expenses rolling into landed cost"`

---

### Task 11: Dashboard, nav finalization, translator lockdown sweep

**Files:**
- Modify: `crm/views.py`, `templates/crm/dashboard.html`, `templates/base.html`
- Test: `tests/test_dashboard.py`, `tests/test_permissions.py`

**Interfaces:**
- Consumes: everything above.
- Produces: dashboard KPI context keys `total_kg`, `shipped_kg`, `arrived_kg`, `paid_total`, `debt_total`, `overdue` (queryset), `contracts` (open, with progress), `status_counts`; translator hitting `/` is redirected to `shipment_list`.

- [ ] **Step 1: Write the failing tests**

`tests/test_dashboard.py`:

```python
from datetime import date, timedelta
from decimal import Decimal

from crm.models import Contract, Partner, Shipment, ShipmentStatus


def test_dashboard_kpis(admin_client, db):
    partner = Partner.objects.create(name="Pars", phone="1", city="T")
    c = Contract.objects.create(partner=partner, brand="LLDPE", kg=Decimal("1000"),
                                price=Decimal("1"), created="2026-07-01", deadline="2026-08-01")
    Shipment.objects.create(contract=c, kg=Decimal("400"),
                            status=ShipmentStatus.objects.first(),
                            eta=date.today() - timedelta(days=2))
    html = admin_client.get("/").content.decode()
    assert "kechikdi" in html.lower()
    assert "LLDPE" in html


def test_translator_redirected(translator_client):
    resp = translator_client.get("/")
    assert resp.status_code == 302 and resp.url == "/shipments/"
```

`tests/test_permissions.py` — the lockdown sweep:

```python
import pytest

ADMIN_ONLY_URLS = [
    "/partners/", "/partners/new/", "/contracts/", "/contracts/new/",
    "/supplier-payments/", "/supplier-payments/new/", "/statuses/",
    "/expenses/new/", "/audit/",
]


@pytest.mark.parametrize("url", ADMIN_ONLY_URLS)
def test_translator_gets_403(translator_client, url):
    assert translator_client.get(url).status_code == 403


@pytest.mark.parametrize("url", ["/shipments/"])
def test_translator_allowed(translator_client, url):
    assert translator_client.get(url).status_code == 200


def test_anonymous_redirected(client, db):
    assert client.get("/shipments/").status_code == 302
```

- [ ] **Step 2: Run to verify failure** — dashboard test FAILS (placeholder page has no KPIs, no redirect).

- [ ] **Step 3: Implement**

`crm/views.py` dashboard:

```python
def dashboard(request):
    if not request.user.is_admin_role:
        return redirect("shipment_list")
    shipments = Shipment.objects.select_related("contract__partner", "status")
    contracts = Contract.objects.select_related("partner")
    total_kg = contracts.aggregate(s=Sum("kg"))["s"] or 0
    shipped_kg = shipments.aggregate(s=Sum("kg"))["s"] or 0
    arrived_kg = shipments.filter(arrived__isnull=False).aggregate(s=Sum("kg"))["s"] or 0
    paid_total = SupplierPayment.objects.aggregate(s=Sum("amount"))["s"] or 0
    debt_total = sum((c.debt for c in contracts), Decimal("0"))
    overdue = [s for s in shipments.filter(arrived__isnull=True, eta__isnull=False)
               if s.is_overdue]
    status_counts = (ShipmentStatus.objects
                     .annotate(n=models.Count("shipments"))
                     .filter(n__gt=0))
    return render(request, "crm/dashboard.html", {
        "total_kg": total_kg, "shipped_kg": shipped_kg, "arrived_kg": arrived_kg,
        "paid_total": paid_total, "debt_total": debt_total, "overdue": overdue,
        "contracts": contracts[:8], "status_counts": status_counts,
    })
```

(Import `Decimal`, `SupplierPayment`, and `from django.db import models` as needed at the top of views.)

`templates/crm/dashboard.html`: KPI card grid (client-crm dashboard markup style — `card` blocks): Kelishilgan kg, Yuborilgan kg, Omborga kelgan kg, Jami to'langan $, Hamkor qarzi $, Kechikkan yuklar (count, red when > 0). Below: two cards — "Kelishuvlar bajarilishi" (progress bars: `shipped_kg`/`kg` per contract, width via inline style) and "Yuk holatlari" (status name + count pills). Then "Kechikayotgan yuklar" table (Yuk, Hamkor, Marka, Kg, Holat, Reja sana, N kun kechikdi red, link to detail) shown only when `overdue`.
`templates/base.html` final nav: admin group (Dashboard, Hamkorlar, Kelishuvlar, To'lovlar, Holatlar, Audit) wrapped in `{% if user.is_admin_role %}`; shared group (Yuklar) for all authenticated users.

- [ ] **Step 4: Run the whole suite** — `.venv/bin/pytest -v` → ALL PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: admin dashboard with overdue block and translator lockdown"`

---

## Phase 1 exit criteria

- `pytest` green; `python manage.py check` clean; `makemigrations --check` no pending.
- Manual smoke (admin): create partner → contract → payment (overpay rejected) → shipment (kg guard) → translator moves status but can't finish → extend ETA with reason → admin finishes load → expense → landed cost shows on detail → dashboard shows overdue.
- Phase 2 plan (`warehouse & selling`) is written next, consuming: arrived `Shipment` as lot, `landed_cost_per_kg`, `Customer`-side models per spec §4.
