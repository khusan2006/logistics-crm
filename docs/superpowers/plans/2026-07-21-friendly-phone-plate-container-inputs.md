# Friendly Phone / Car / Container Inputs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the phone, car-plate (transport), and container inputs easy to use — an inline 🇺🇿+998 / 🇮🇷+98 country picker for phones, a lenient UZ/IR picker + light formatting for plates, and ISO-6346 spacing for containers.

**Architecture:** UI-only. Each field stays a single Django `CharField`. Server-side gains a small model-free helper module (`crm/formatting.py`) shared by both apps; client-side gains three vanilla-JS enhancers in `base.html` keyed off `data-*` attributes, matching the existing `data-money` / `data-som-*` house pattern. No models, migrations, or dependencies change.

**Tech Stack:** Django 5, pytest, vanilla JS (no libraries), plain CSS.

## Global Constraints

- UI language is **Uzbek**; keep all user-facing copy Uzbek.
- **No third-party JS libraries** — vanilla only, following the IIFE enhancer pattern already in `templates/base.html`.
- **No model/DB/migration changes.** Every field remains its existing `CharField`.
- Phone canonical stored form is `+998 90 123 45 67` / `+98 912 345 6789` (what `validate_intl_phone` already accepts).
- Enhancers must also run on dynamically-injected DOM via the existing `modal:loaded` event.
- Text files end with a trailing newline.
- Run tests with: `.venv/bin/pytest` (settings module is configured in `pytest.ini`).

---

### Task 1: Shared formatting module

Extract the phone validator + widget out of `crm/forms.py` into a new model-free module so `accounts/forms.py` can reuse them without importing `crm.models`, and add the container normalizer.

**Files:**
- Create: `crm/formatting.py`
- Modify: `crm/forms.py` (top: lines 13–36 region; call sites lines 43, 53)
- Test: `tests/test_formatting.py`

**Interfaces:**
- Produces:
  - `validate_intl_phone(value: str) -> str` — returns the stripped value if blank or a valid UZ/IR number; raises `django.forms.ValidationError` otherwise.
  - `phone_intl_widget() -> forms.TextInput` — a fresh `TextInput` carrying `data-phone-intl`.
  - `normalize_container(value: str) -> str` — uppercase + ISO grouping when it matches, else uppercase + single-spaced.

- [ ] **Step 1: Write the failing test**

Create `tests/test_formatting.py`:

```python
import pytest
from django import forms

from crm.formatting import normalize_container, validate_intl_phone


@pytest.mark.parametrize("value", [
    "", "+998 90 123 45 67", "+98 912 345 6789", "998901234567", "989123456789",
])
def test_validate_intl_phone_accepts(value):
    assert validate_intl_phone(value) == value


@pytest.mark.parametrize("value", ["+82343905395034355", "12345", "+1 202 555 0100"])
def test_validate_intl_phone_rejects(value):
    with pytest.raises(forms.ValidationError):
        validate_intl_phone(value)


@pytest.mark.parametrize("raw,expected", [
    ("msku1234567", "MSKU 123456 7"),
    ("MSKU 123456 7", "MSKU 123456 7"),
    ("MSKU1234567", "MSKU 123456 7"),
    ("  mscu-1 ", "MSCU-1"),
    ("", ""),
])
def test_normalize_container(raw, expected):
    assert normalize_container(raw) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_formatting.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'crm.formatting'`.

- [ ] **Step 3: Create the module**

Create `crm/formatting.py`:

```python
"""Model-free formatting + validation helpers for the contact-style inputs
(phone, container). Importable by any app without pulling in crm.models."""
import re

from django import forms

# Uzbek (+998 + 9 national digits) or Iranian (+98 + 10 national digits).
_PHONE_UZ = re.compile(r"998\d{9}")
_PHONE_IR = re.compile(r"98\d{10}")

# ISO 6346: 4 owner/category letters + 6 serial digits + 1 check digit.
_CONTAINER_ISO = re.compile(r"^([A-Z]{4})(\d{6})(\d)$")


def validate_intl_phone(value):
    """Blank, or a valid Uzbek/Iranian number. Formatting (spaces, +, -) is ignored."""
    v = (value or "").strip()
    if not v:
        return v
    digits = re.sub(r"\D", "", v)
    if _PHONE_UZ.fullmatch(digits) or _PHONE_IR.fullmatch(digits):
        return v
    raise forms.ValidationError(
        "Telefon O'zbekiston (+998 XX XXX XX XX) yoki Eron (+98 XXX XXX XXXX) "
        "formatida bo'lishi kerak")


def phone_intl_widget():
    """A fresh phone TextInput (so forms don't share a mutable attrs dict). The
    base.html data-phone-intl enhancer turns this into an inline country picker."""
    return forms.TextInput(attrs={
        "data-phone-intl": "", "inputmode": "tel", "autocomplete": "tel",
        "placeholder": "+998 90 123 45 67  yoki  +98 912 345 6789",
    })


def normalize_container(value):
    """Uppercase + strip; when the compacted value is ISO 6346 (4 letters + 7
    digits) render it grouped as 'ABCD 123456 7'. Otherwise return the uppercased,
    space-collapsed string unchanged. Lets 'msku1234567' and 'MSKU 123456 7'
    compare and store identically."""
    v = (value or "").strip().upper()
    if not v:
        return v
    compact = re.sub(r"\s+", "", v)
    m = _CONTAINER_ISO.match(compact)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return re.sub(r"\s+", " ", v)
```

- [ ] **Step 4: Rewire `crm/forms.py` to import from the module**

In `crm/forms.py`, delete the old block (the `_PHONE_UZ` / `_PHONE_IR` regexes, `validate_intl_phone`, and `_phone_widget` — lines 13–36) and replace with an import near the other imports (after the `from .models import (...)` block):

```python
from .formatting import normalize_container, phone_intl_widget, validate_intl_phone
```

Then update the two widget call sites (in `PartnerForm.Meta.widgets` and `CustomerForm.Meta.widgets`), replacing `_phone_widget()` with `phone_intl_widget()`:

```python
        widgets = {"note": forms.Textarea(attrs={"rows": 3}), "phone": phone_intl_widget()}
```

(The `clean_phone` methods already call `validate_intl_phone`, now imported — no change there.)

- [ ] **Step 5: Run the full suite to verify nothing regressed**

Run: `.venv/bin/pytest tests/test_formatting.py tests/test_partners.py tests/test_customers.py -q`
Expected: PASS (new formatting tests green; existing partner/customer phone tests still green via the re-exported `validate_intl_phone`).

- [ ] **Step 6: Commit**

```bash
git add crm/formatting.py crm/forms.py tests/test_formatting.py
git commit -m "refactor: extract model-free formatting helpers (phone, container)"
```

---

### Task 2: Container normalization + plate/container widget hooks

Wire `normalize_container` into the shipment/leg forms so stored + compared container values ignore spacing, and mark the transport and container widgets with the `data-*` attributes the JS enhancers key off.

**Files:**
- Modify: `crm/forms.py` (`ShipmentForm` — lines 167–214; `ShipmentLegForm` — lines 239–257)
- Test: `tests/test_shipments.py`

**Interfaces:**
- Consumes: `normalize_container` (Task 1).
- Produces: `ShipmentForm.transport`/`container` and `ShipmentLegForm.transport`/`container` widgets rendered with `data-plate-intl` / `data-container-iso`; `clean_container` returns the normalized string on both forms.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_shipments.py`:

```python
def test_container_unique_ignores_spacing(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="MSKU 123456 7")
    resp = _post_shipment(admin_client, c, kg="100", container="msku1234567")
    assert Shipment.objects.count() == 1 and resp.status_code == 200
    assert Shipment.objects.first().container == "MSKU 123456 7"


def test_container_stored_normalized(admin_client, db):
    c = _contract()
    _post_shipment(admin_client, c, kg="100", container="mscu1234567")
    assert Shipment.objects.get().container == "MSCU 123456 7"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_shipments.py::test_container_unique_ignores_spacing tests/test_shipments.py::test_container_stored_normalized -q`
Expected: FAIL — the second shipment saves (count == 2) and/or the stored value keeps its raw spacing, because `clean_container` doesn't normalize yet.

- [ ] **Step 3: Update `ShipmentForm`**

In `ShipmentForm.Meta.widgets`, change the `transport` widget and add a `container` widget:

```python
            "transport": forms.TextInput(attrs={
                "data-plate-intl": "", "autocomplete": "off", "placeholder": "01 777 AAA"}),
            "container": forms.TextInput(attrs={
                "data-container-iso": "", "autocomplete": "off", "placeholder": "MSKU 123456 7"}),
```

Replace `ShipmentForm.clean_container` with:

```python
    def clean_container(self):
        container = normalize_container(self.cleaned_data.get("container"))
        if container:
            clash = Shipment.objects.filter(container__iexact=container)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError("Bu konteyner raqami avval kiritilgan")
        return container
```

- [ ] **Step 4: Update `ShipmentLegForm`**

In `ShipmentLegForm.Meta.widgets`, change the `transport` widget and add a `container` widget:

```python
            "transport": forms.TextInput(attrs={
                "data-plate-intl": "", "autocomplete": "off",
                "placeholder": "Haydovchi ismi yoki 01 777 AAA"}),
            "container": forms.TextInput(attrs={
                "data-container-iso": "", "autocomplete": "off", "placeholder": "MSKU 123456 7"}),
```

Add a `clean_container` to `ShipmentLegForm` (normalize only — legs have no uniqueness constraint), placed above its existing `clean`:

```python
    def clean_container(self):
        return normalize_container(self.cleaned_data.get("container"))
```

- [ ] **Step 5: Run the shipment tests**

Run: `.venv/bin/pytest tests/test_shipments.py -q`
Expected: PASS (new normalization tests green; existing `test_container_unique`, transport, and modal tests still green — `MSCU-1`/`MSCU-2`/`MSCU-3` aren't ISO-shaped so they only uppercase, and `01A111AA`-style plates are untouched server-side).

- [ ] **Step 6: Commit**

```bash
git add crm/forms.py tests/test_shipments.py
git commit -m "feat: normalize container numbers (ISO spacing) + mark plate/container widgets"
```

---

### Task 3: Accounts user-form phone

Give the bare `UserForm.phone` field the same picker widget + validation as the CRM contact forms.

**Files:**
- Modify: `accounts/forms.py` (`UserForm`, lines 19–33 region)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `phone_intl_widget`, `validate_intl_phone` (Task 1).
- Produces: `UserForm` renders `phone` with `data-phone-intl` and rejects non-UZ/IR numbers.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_auth.py`:

```python
def test_userform_rejects_bad_phone(db):
    from accounts.forms import UserForm
    from accounts.models import User
    f = UserForm({"username": "u1", "first_name": "A", "last_name": "B",
                  "phone": "12345", "role": User.Role.TRANSLATOR, "password": "secret12"})
    assert not f.is_valid() and "phone" in f.errors


def test_userform_accepts_intl_phone(db):
    from accounts.forms import UserForm
    from accounts.models import User
    f = UserForm({"username": "u2", "first_name": "A", "last_name": "B",
                  "phone": "+998 90 123 45 67", "role": User.Role.TRANSLATOR, "password": "secret12"})
    assert f.is_valid(), f.errors
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_auth.py::test_userform_rejects_bad_phone -q`
Expected: FAIL — `f.is_valid()` is `True` because the plain `phone` field accepts `"12345"`.

- [ ] **Step 3: Update `UserForm`**

In `accounts/forms.py`, add the import at the top (with the other imports):

```python
from crm.formatting import phone_intl_widget, validate_intl_phone
```

Add a `widgets` dict to `UserForm.Meta` and a `clean_phone` method:

```python
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "phone", "role"]
        widgets = {"phone": phone_intl_widget()}

    def clean_phone(self):
        return validate_intl_phone(self.cleaned_data.get("phone"))
```

- [ ] **Step 4: Run the auth tests**

Run: `.venv/bin/pytest tests/test_auth.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add accounts/forms.py tests/test_auth.py
git commit -m "feat: intl phone picker + validation on the user admin form"
```

---

### Task 4: Phone country-picker enhancer + CSS + quick-add

Replace the two old phone IIFEs in `base.html` (the dead `data-phone` UZ-lock enhancer and the picker-less `data-phone-intl` blur formatter) with a single enhancer that injects an inline 🇺🇿+998 / 🇮🇷+98 picker; add the joined-field CSS; and upgrade the live customer quick-add to use it. Verified in the browser.

**Files:**
- Modify: `templates/base.html` (delete lines 837–960 — the two phone IIFEs; edit the quick-add builder at ~1044–1082)
- Modify: `static/css/app.css` (add `.intl-field` rules near the form-control block)

**Interfaces:**
- Produces globals: `window.enhancePhoneIntl(root)` (rescan + enhance `[data-phone-intl]`), `window.canonicalPhone(input)` (canonical `+998 …` string from an enhanced or raw input). The quick-add save handler consumes `window.canonicalPhone`.

- [ ] **Step 1: Replace the two phone IIFEs**

In `templates/base.html`, delete the **entire** `// Phone inputs (data-phone): …` IIFE (starts at line 837 `// Phone inputs (data-phone):`, ends at its closing `})();` on line 923) **and** the `// International phone (data-phone-intl): …` IIFE (lines 925–960). Replace both with this single IIFE:

```javascript
  // International phone (data-phone-intl): injects a country picker (🇺🇿 +998 /
  // 🇮🇷 +98) joined to the left of the field. The visible box holds only the
  // national digits, auto-grouped as you type. A hidden mirror carries the
  // canonical "+998 90 123 45 67" under the field's name, so whatever submit
  // path runs (normal POST or the modal's FormData) sends the canonical string
  // that validate_intl_phone accepts. Empty stays empty (phone is optional).
  (function () {
    var COUNTRIES = {
      '998': { label: '+998', flag: '🇺🇿', sizes: [2, 3, 2, 2], ph: '90 123 45 67' },
      '98':  { label: '+98',  flag: '🇮🇷', sizes: [3, 3, 4],    ph: '912 345 6789' }
    };
    var ORDER = ['998', '98'];
    function maxLen(cc) { return COUNTRIES[cc].sizes.reduce(function (a, b) { return a + b; }, 0); }
    function digitsOnly(s) { return String(s).replace(/\D/g, ''); }
    function group(nat, sizes) {
      var out = [], i = 0;
      for (var k = 0; k < sizes.length && i < nat.length; k++) { out.push(nat.slice(i, i + sizes[k])); i += sizes[k]; }
      if (i < nat.length) { out.push(nat.slice(i)); }
      return out.join(' ');
    }
    function parse(value) {
      var d = digitsOnly(value);
      if (d.indexOf('998') === 0) { return { cc: '998', nat: d.slice(3) }; }
      if (d.indexOf('98') === 0)  { return { cc: '98',  nat: d.slice(2) }; }
      return { cc: null, nat: d };
    }
    function display(cc, nat) { return group(nat.slice(0, maxLen(cc)), COUNTRIES[cc].sizes); }
    function canonicalFrom(cc, nat) {
      nat = digitsOnly(nat).slice(0, maxLen(cc));
      return nat ? COUNTRIES[cc].label + ' ' + group(nat, COUNTRIES[cc].sizes) : '';
    }
    function digitsBefore(s, caret) {
      var n = 0; for (var i = 0; i < caret && i < s.length; i++) { if (s[i] >= '0' && s[i] <= '9') { n++; } } return n;
    }
    function caretAfter(s, n) {
      if (n <= 0) { return 0; }
      var seen = 0; for (var i = 0; i < s.length; i++) { if (s[i] >= '0' && s[i] <= '9') { if (++seen === n) { return i + 1; } } } return s.length;
    }
    function selectOf(input) { var w = input.closest('.intl-field'); return w ? w.querySelector('select.intl-cc') : null; }
    function ccOf(input) { var s = selectOf(input); return s ? s.value : '998'; }
    function reformat(input) {
      var cc = ccOf(input), caret = input.selectionStart;
      var before = caret == null ? null : digitsBefore(input.value, caret);
      input.value = display(cc, digitsOnly(input.value));
      if (before != null) { var p = caretAfter(input.value, before); try { input.setSelectionRange(p, p); } catch (e) {} }
    }
    function mirror(input) { if (input.__intlHidden) { input.__intlHidden.value = canonicalFrom(ccOf(input), input.value); } }
    function buildSelect(cc) {
      var sel = document.createElement('select');
      sel.className = 'intl-cc';
      ORDER.forEach(function (k) { var o = document.createElement('option'); o.value = k; o.textContent = COUNTRIES[k].flag + ' ' + COUNTRIES[k].label; sel.appendChild(o); });
      sel.value = cc;
      return sel;
    }
    function enhance(root) {
      (root || document).querySelectorAll('input[data-phone-intl]').forEach(function (input) {
        if (input.dataset.phoneIntlReady) { return; }
        input.dataset.phoneIntlReady = '1';
        var p = parse(input.value), cc = p.cc || '998';
        var wrap = document.createElement('div');
        wrap.className = 'intl-field';
        input.parentNode.insertBefore(wrap, input);
        var sel = buildSelect(cc);
        wrap.appendChild(sel);
        wrap.appendChild(input);
        var hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = input.getAttribute('name') || '';
        input.__intlHidden = hidden;
        input.removeAttribute('name');
        wrap.appendChild(hidden);
        input.type = 'tel'; input.setAttribute('inputmode', 'numeric'); input.autocomplete = 'off';
        input.placeholder = COUNTRIES[cc].ph;
        input.value = display(cc, p.nat);
        mirror(input);
        input.addEventListener('input', function () { reformat(input); mirror(input); });
        sel.addEventListener('change', function () { reformat(input); input.placeholder = COUNTRIES[sel.value].ph; mirror(input); input.focus(); });
      });
    }
    window.enhancePhoneIntl = enhance;
    window.canonicalPhone = function (input) {
      if (!input) { return ''; }
      if (input.dataset && input.dataset.phoneIntlReady) { return canonicalFrom(ccOf(input), input.value); }
      var p = parse(input.value); return canonicalFrom(p.cc || '998', p.nat);
    };
    document.addEventListener('modal:loaded', function (e) { enhance(e.detail || document); });
    if (document.readyState !== 'loading') { enhance(); }
    else { document.addEventListener('DOMContentLoaded', function () { enhance(); }); }
  })();
```

- [ ] **Step 2: Add the joined-field CSS**

In `static/css/app.css`, after the form-control block (after line 953, the `input:focus…` rule), add:

```css
/* Country-code picker joined to a phone/plate field (base.html enhancers) */
.intl-field { display: flex; align-items: stretch; width: 100%; max-width: 480px; }
.intl-field > select.intl-cc {
  width: auto;
  flex: 0 0 auto;
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
  border-right-color: transparent;
  padding: 9px 8px;
}
.intl-field > input {
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
  max-width: none;
}
.intl-field > select.intl-cc:focus { position: relative; z-index: 1; }
.modal form.stacked .intl-field { max-width: none; }
```

- [ ] **Step 3: Upgrade the live customer quick-add to the picker**

In the `inject(sel)` builder (around line 1054), change the `.qa-phone` line to carry the attribute:

```javascript
        '<input type="text" class="qa-phone" data-phone-intl placeholder="Telefon (ixtiyoriy)">' +
```

Immediately after the panel is inserted (after the `sel.parentNode.insertBefore(panel, btn.nextSibling);` line ~1061), enhance it:

```javascript
      if (window.enhancePhoneIntl) { window.enhancePhoneIntl(panel); }
```

In the `qa-save` click handler (around line 1069), read the canonical value:

```javascript
        var name = panel.querySelector('.qa-name').value.trim();
        var phoneEl = panel.querySelector('.qa-phone');
        var phone = window.canonicalPhone ? window.canonicalPhone(phoneEl) : phoneEl.value.trim();
```

(the `fd.append('phone', phone);` line already uses `phone` — no further change).

- [ ] **Step 4: Verify in the browser**

Start the dev server (`preview_start`, config in `.claude/launch.json`). Then:
1. Open a form with a phone field (e.g. `/partners/new/`). Confirm the 🇺🇿+998 picker sits joined to the left; typing `901234567` shows `90 123 45 67`; switching to 🇮🇷 re-masks; submitting saves `+998 90 123 45 67` (check the saved Partner). Use `preview_snapshot` / `preview_inspect` to confirm structure + that no console errors appear (`preview_console_logs level=error`).
2. Open a Sale/Reservation form, click **+ Yangi mijoz**, confirm the quick-add phone shows the picker and the created customer's phone is canonical.
3. Edit an existing partner with an Iranian number — confirm the picker preselects 🇮🇷 and shows only the national part.

- [ ] **Step 5: Commit**

```bash
git add templates/base.html static/css/app.css
git commit -m "feat: inline country picker for phone inputs (+ quick-add), remove dead data-phone enhancer"
```

---

### Task 5: Plate + container enhancers

Add the `data-plate-intl` (UZ/IR picker + light formatting) and `data-container-iso` (uppercase + ISO grouping) enhancers to `base.html`. Verified in the browser.

**Files:**
- Modify: `templates/base.html` (add two IIFEs directly after the phone IIFE from Task 4)

**Interfaces:**
- Consumes: the `.intl-field` CSS from Task 4 (plate reuses it).
- Produces globals: `window.enhancePlateInputs(root)`, `window.enhanceContainerInputs(root)`.

- [ ] **Step 1: Add the plate enhancer**

In `templates/base.html`, directly after the phone-intl IIFE's closing `})();`, add:

```javascript
  // Car / truck plate (data-plate-intl): a UZ/IR picker that sets the example and
  // (for IR) allows Persian letters. Light, lenient formatting — uppercase live,
  // tidy spacing on blur — so unusual plates still save. The country is a
  // formatting hint; the plate string itself is what's stored (server keeps its
  // own lenient clean_transport). On edit the country is auto-detected.
  (function () {
    var PERSIAN = /[؀-ۿ]/;
    var EXAMPLE = { uz: '01 777 AAA', ir: '12 A 345 67' };
    function tidy(v) { return String(v).toUpperCase().replace(/\s+/g, ' ').replace(/^\s+/, ''); }
    function detect(v) { return PERSIAN.test(v) ? 'ir' : 'uz'; }
    function enhance(root) {
      (root || document).querySelectorAll('input[data-plate-intl]').forEach(function (input) {
        if (input.dataset.plateReady) { return; }
        input.dataset.plateReady = '1';
        var wrap = document.createElement('div');
        wrap.className = 'intl-field';
        input.parentNode.insertBefore(wrap, input);
        var sel = document.createElement('select');
        sel.className = 'intl-cc';
        [['uz', '🇺🇿 UZ'], ['ir', '🇮🇷 IR']].forEach(function (pair) {
          var o = document.createElement('option'); o.value = pair[0]; o.textContent = pair[1]; sel.appendChild(o);
        });
        wrap.appendChild(sel);
        wrap.appendChild(input);
        sel.value = input.value ? detect(input.value) : 'uz';
        input.autocomplete = 'off';
        input.placeholder = EXAMPLE[sel.value];
        if (input.value) { input.value = tidy(input.value); }
        input.addEventListener('input', function () {
          var caret = input.selectionStart;
          input.value = input.value.toUpperCase();
          try { input.setSelectionRange(caret, caret); } catch (e) {}
        });
        input.addEventListener('blur', function () { input.value = tidy(input.value); });
        sel.addEventListener('change', function () { input.placeholder = EXAMPLE[sel.value]; input.focus(); });
      });
    }
    window.enhancePlateInputs = enhance;
    document.addEventListener('modal:loaded', function (e) { enhance(e.detail || document); });
    if (document.readyState !== 'loading') { enhance(); }
    else { document.addEventListener('DOMContentLoaded', function () { enhance(); }); }
  })();
```

- [ ] **Step 2: Add the container enhancer**

Directly after the plate IIFE, add:

```javascript
  // Container (data-container-iso): uppercase live; on blur, when the value is
  // ISO 6346 (4 letters + 7 digits) group it as "MSKU 123456 7". No hard check —
  // the server's normalize_container produces the identical string for the
  // uniqueness test, so what you see is what's stored.
  (function () {
    var ISO = /^([A-Z]{4})(\d{6})(\d)$/;
    function fmt(v) {
      var up = String(v).toUpperCase(), compact = up.replace(/\s+/g, ''), m = ISO.exec(compact);
      return m ? (m[1] + ' ' + m[2] + ' ' + m[3]) : up.replace(/\s+/g, ' ').replace(/^\s+/, '');
    }
    function enhance(root) {
      (root || document).querySelectorAll('input[data-container-iso]').forEach(function (input) {
        if (input.dataset.containerReady) { return; }
        input.dataset.containerReady = '1';
        input.autocomplete = 'off';
        if (input.value) { input.value = fmt(input.value); }
        input.addEventListener('input', function () {
          var caret = input.selectionStart;
          input.value = input.value.toUpperCase();
          try { input.setSelectionRange(caret, caret); } catch (e) {}
        });
        input.addEventListener('blur', function () { input.value = fmt(input.value); });
      });
    }
    window.enhanceContainerInputs = enhance;
    document.addEventListener('modal:loaded', function (e) { enhance(e.detail || document); });
    if (document.readyState !== 'loading') { enhance(); }
    else { document.addEventListener('DOMContentLoaded', function () { enhance(); }); }
  })();
```

- [ ] **Step 3: Verify in the browser**

On a shipment create form (`/shipments/new/` or its modal):
1. Transport field shows the UZ/IR picker; typing `01777aaa` uppercases to `01777AAA`; blur tidies spacing; switching to 🇮🇷 changes the example. Confirm it still saves an odd value unchanged (leniency).
2. Container field: type `msku1234567`; blur shows `MSKU 123456 7`; save and confirm the stored value. Type a non-ISO value like `abc-99` and confirm it just uppercases.
3. Check `preview_console_logs level=error` is clean and the fields behave the same inside a modal (open the shipment modal, not just the full page).

- [ ] **Step 4: Run the whole test suite (guard against regressions)**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates/base.html
git commit -m "feat: UZ/IR plate picker + ISO container formatting enhancers"
```

---

## Self-Review

**Spec coverage:**
- Phone inline picker (UZ/IR), national-only entry, canonical storage, edit auto-detect, empty-stays-empty → Task 4 (+ widget from Task 1). ✔
- Accounts UserForm phone → Task 3. ✔
- Car/plate picker + light lenient formatting on both transport fields → Task 2 (widgets) + Task 5 (JS). ✔
- Container uppercase + ISO spacing, uniqueness ignores spacing, both forms → Task 1 (`normalize_container`) + Task 2 (forms) + Task 5 (JS). ✔
- Remove dead `data-phone` enhancer → Task 4. ✔ (Its only live consumer, `window.canonicalPhone`, is re-provided by the new module and the dead 329–367 quick-add block is left untouched.)
- Shared model-free module to avoid accounts→crm.models coupling → Task 1. ✔
- `.intl-field` CSS → Task 4. ✔
- Tests: phone canonical accepted, container spacing collision, accounts phone validation → Tasks 1–3. ✔

**Placeholder scan:** No TBD/TODO; every code + test step shows complete content; every command has an expected result. ✔

**Type consistency:** `validate_intl_phone` / `phone_intl_widget` / `normalize_container` defined in Task 1 are used with the same names/signatures in Tasks 2–3. JS globals `enhancePhoneIntl` / `canonicalPhone` defined in Task 4 are consumed by the quick-add edit in the same task; `enhancePlateInputs` / `enhanceContainerInputs` are self-contained in Task 5. ✔
