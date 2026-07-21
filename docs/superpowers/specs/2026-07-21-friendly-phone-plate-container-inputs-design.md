# Friendly Phone / Car / Container Inputs — Design Spec

**Date:** 2026-07-21
**Scope:** Make the phone, car-plate (transport), and container-number inputs easy to use — a
country-code picker for phones, a lenient country-aware formatter for plates, and ISO spacing for
containers. UI-only; no model or DB changes.

## 1. Problem

Three text fields are unfriendly today:

- **Phone** (`data-phone-intl`, Partner + Customer forms): a single plain box. The operator must
  type `+998`/`+98` themselves; spacing only appears on blur. No visible way to *choose* the country.
- **Phone** (accounts `UserForm.phone`): a bare `CharField`, no formatting or validation at all.
- **Car / transport** (`Shipment.transport`, `ShipmentLeg.transport`): plain text, no masking or
  spacing, only a lenient server check.
- **Container** (`Shipment.container`, `ShipmentLeg.container`): plain text, uniqueness check only,
  no formatting; `MSKU1234567` and `MSKU 123456 7` are treated as different values.

## 2. Decisions (agreed 2026-07-21)

| # | Topic | Decision |
|---|-------|----------|
| 1 | Phone UX | **Inline country picker.** A `<select>` (🇺🇿 +998 / 🇮🇷 +98) is injected, visually joined to the left edge of the input. Operator types only the national digits; they auto-group live. |
| 2 | Car plate UX | **Picker + light formatting.** Same UZ/IR picker; auto-uppercase + readable spacing when the value cleanly matches, otherwise leave as typed. Lenient — odd plates still save. |
| 3 | Container UX | **Uppercase + ISO 6346 spacing** (`4 letters / 6 digits / check digit`). No picker (containers are international). No hard validation. |
| 4 | Implementation | **`data-*` attribute + vanilla-JS enhancer in `base.html`**, matching the house pattern (`data-money`, `data-som-*`). Each field stays a single `CharField`; the picker is display-only and the value is canonicalized on submit. No `MultiWidget`, no field splitting, no schema change. |
| 5 | Accounts phone | The bare `UserForm.phone` field gets the same picker + validation. |
| 6 | Dead code | Remove the unused `data-phone` (non-intl, UZ-only lock) enhancer in `base.html` — no form references it. |

## 3. Behavior

### 3.1 Phone — `data-phone-intl`

- Enhancer injects a country `<select>` before the input, wrapped in an `.intl-field` flex container
  (joined borders, reusing the `.field-with-add` look).
- Countries: `+998` (UZ, national = 9 digits) and `+98` (IR, national = 10 digits).
- The input holds **only national digits**, auto-grouped live as typed:
  - UZ → `90 123 45 67` (2 · 3 · 2 · 2)
  - IR → `912 345 6789` (3 · 3 · 4)
- On submit, JS rewrites the input value to the **canonical stored string**:
  - `+998 90 123 45 67` / `+98 912 345 6789`
  - This is exactly what the existing `validate_intl_phone` accepts — server validation unchanged.
- **Empty stays empty** (phone is optional): a picker with no national digits submits as `""`.
- **Editing** an existing value: strip the value to digits, detect country (`998…` → UZ, `98…` → IR),
  select that country and show only the national part.
- Changing the country re-masks the current national digits under the new grouping rule.
- Works inside modals / quick-add (wired to the `modal:loaded` event + exposed rescan function).

### 3.2 Car / transport — `data-plate-intl`

- Same injected UZ/IR `<select>`; the country drives the placeholder/example and the permitted
  letter set (UZ = Latin, IR = Latin + Persian letters).
- **Light, lenient** formatting on input/blur: uppercase, collapse repeated whitespace to single
  spaces, and apply readable grouping **only when the value cleanly matches** a known shape
  (UZ `01 777 AAA`, IR `12 A 345 67`); otherwise leave the operator's spacing intact.
- The country is a formatting hint, **not stored separately** — the plate string is stored as shown.
  On edit, the country is auto-detected (Persian letters → IR, else UZ).
- Server side unchanged: the existing lenient `clean_transport` still guards the final string.

### 3.3 Container — `data-container-iso`

- No picker. On input/blur: uppercase and strip stray spaces.
- When the compacted value is `^[A-Z]{4}\d{7}$`, display as `ABCD 123456 7` (4 / 6 / 1).
  Otherwise show the uppercased value as typed. No hard validation.
- `clean_container` gains a **normalizer** (uppercase + canonical ISO spacing when it matches) applied
  **before** the uniqueness query and before storing, so `msku1234567` and `MSKU 123456 7` collide
  correctly and are stored consistently.

## 4. Components & files

- **`crm/formatting.py`** *(new, model-free)* — pure helpers importable by both apps without pulling
  in `crm.models`:
  - `validate_intl_phone(value)` (moved here from `crm/forms.py`)
  - `phone_intl_widget()` — the shared `TextInput` with `data-phone-intl` attrs
  - `normalize_container(value)` — uppercase + ISO spacing when it matches
- **`crm/forms.py`**
  - `_phone_widget()` → re-export / delegate to `formatting.phone_intl_widget`.
  - Add `_plate_widget()` (`data-plate-intl` + example placeholder); apply to `ShipmentForm.transport`
    and `ShipmentLegForm.transport`.
  - Add `data-container-iso` attr to the container widgets on both forms.
  - `clean_container` → normalize via `normalize_container` before the clash check + return normalized.
- **`accounts/forms.py`** — `UserForm`: set `phone`'s widget to `formatting.phone_intl_widget()` and add
  `clean_phone` → `validate_intl_phone`.
- **`templates/base.html`**
  - Upgrade the `data-phone-intl` IIFE: inject the country `<select>`, switch to national-digits-only
    input + submit-time canonicalization, keep `modal:loaded` wiring.
  - Add a `data-plate-intl` enhancer (picker + light formatting).
  - Add a `data-container-iso` enhancer (uppercase + ISO grouping).
  - Delete the dead `data-phone` (non-intl) enhancer.
- **`static/css/app.css`** — `.intl-field` styles: joined select + input (shared border radius, no
  double border between them), matching the existing input theme tokens.

## 5. Testing

- **Server (pytest, TDD):**
  - `validate_intl_phone` accepts canonical spaced UZ/IR strings and blank; rejects junk. (extend
    `test_partners` / `test_customers`)
  - Accounts `UserForm` rejects an invalid phone and accepts a valid one.
  - `normalize_container`: `msku1234567`, `MSKU 123456 7`, `MSKU1234567` all normalize equal; a
    second shipment with a spacing-variant of an existing container fails the uniqueness check.
    (extend `test_shipments`)
  - Non-ISO container strings pass through unchanged except uppercasing.
- **Client (preview browser, manual verify):** phone picker masks live and canonicalizes on submit;
  country switch re-masks; plate uppercases + spaces leniently; container groups as `MSKU 123456 7`;
  all behave inside a modal.

## 6. Out of scope

- No new stored country column on any model.
- No strict plate validation / rejection.
- No ISO 6346 check-digit verification (spacing only).
- No third-party libraries (intl-tel-input etc.) — vanilla JS only, per house style.
