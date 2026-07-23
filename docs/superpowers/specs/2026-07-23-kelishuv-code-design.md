# Kelishuv kodi: `hamkorNomi-raqam`

**Sana:** 2026-07-23
**Holat:** tasdiqlangan

## Muammo

Kelishuv (`Contract`) ekranlarda `#12` — ya'ni ichki raqamli `pk` — sifatida
ko'rinadi. Mijoz har bir hamkor uchun alohida, 1 dan boshlanadigan kod so'radi:
`sobir-1`, `sobir-2`, …, va `azim-1`, `azim-2`, ….

## Yechim

`Contract` ikkita yangi ustun oladi:

| Ustun | Turi | Izoh |
|---|---|---|
| `code_slug` | `CharField(max_length=120, db_index=True)` | Hamkor nomidan olingan prefiks |
| `code_number` | `PositiveIntegerField()` | Shu hamkor uchun tartib raqami |

`code` — bu saqlanmaydigan property: `f"{code_slug}-{code_number}"` → `sobir-3`.

Yaxlitlik: `UniqueConstraint(fields=["code_slug", "code_number"])`.

### Nomdan slug olish

`django.utils.text.slugify(partner.name, allow_unicode=True)`:

- `"Ali Valiyev"` → `ali-valiyev`
- `"G'ayrat"` → `gayrat` (apostrof tushadi)
- `"Шариф"` → `шариф` (kirillcha saqlanadi)

Slug bo'sh chiqsa (nom faqat tinish belgilaridan iborat bo'lsa) — `hamkor`
zaxira slugi ishlatiladi, shunda kod hech qachon `-3` ko'rinishida qolmaydi.

### Raqam berish qoidasi

```
raqam = 1 + max(shu hamkor ishlatgan eng katta raqam,
                shu slug ishlatgan eng katta raqam)
```

Ikkala manbani hisobga olish ikkita holatni bir vaqtda to'g'ri hal qiladi:

- **Hamkor nomi o'zgarsa.** `sobir` → `sobirjon`: eski kelishuvlar `sobir-1..6`
  bo'lib qoladi, keyingisi `sobirjon-7` bo'ladi (hamkor bo'yicha max = 6).
- **Ikki hamkor bir xil slugga tushsa.** `"G'ayrat"` va `"Gayrat"` — ikkalasi
  ham `gayrat`. Ikkinchisi birinchisining raqamlaridan yuqoridan boshlaydi,
  shuning uchun `gayrat-1` hech qachon takrorlanmaydi.

### Kod qachon beriladi

`Contract.save()` ichida, faqat ikki holatda:

1. **Yaratilganda** — `code_slug` hali bo'sh bo'lsa.
2. **Hamkor almashtirilganda** — `partner_id` bazadagidan farq qilsa. Kod yangi
   hamkor nomi bilan qayta beriladi (`sobir-3` → `javod-4`), eskisi esa butunlay
   iste'moldan chiqadi.

Boshqa hech qachon o'zgarmaydi ("frozen at creation").

### Muzlatilgan kodning oqibatlari

- `sobir-3` o'chirilsa, 3-raqam **qaytib berilmaydi** — o'rin bo'sh qoladi.
- Hamkor nomi o'zgarsa, mavjud kodlar **o'zgarmaydi**; bitta hamkorda
  `sobir-1..6` va `sobirjon-7` yonma-yon turishi normal holat.
- Kelishuv boshqa hamkorga ko'chirilsa, eski kodi **hech kimga berilmaydi**.

### Bir vaqtda saqlash (race)

Ikki admin bir paytda saqlasa, ikkalasi bir xil raqamni hisoblaydi. Unique
constraint yutqazganini rad etadi, `save()` esa raqamni qayta hisoblab, 5
martagacha urinib ko'radi — foydalanuvchiga xato ko'rsatilmaydi.

## Ko'rinishi

`Contract.__str__` → `f"{self.code} · {self.brand}"`. Hamkor nomi kod ichida
bor, shuning uchun uni takrorlash shart emas. Bu o'zgarish `ShipmentForm` va
`SupplierPaymentForm` dagi `<select>` larni ham avtomatik tuzatadi
(`ContractChoiceSelect` `__str__` ni chiqaradi).

`#` prefiksi olib tashlanadi — `sobir-3` yonida u ortiqcha.

| Shablon | Hozir | Bo'ladi |
|---|---|---|
| `contract_list.html:67` | `#{{ c.pk }}` | `{{ c.code }}` |
| `shipment_list.html:50` | `Kelishuv #{{ g.contract.pk }}` | `Kelishuv {{ g.contract.code }}` |
| `shipment_list.html:140` | `Kelishuv #{{ s.contract_id }}` | `Kelishuv {{ s.contract.code }}` |
| `shipment_detail.html:16` | `#{{ shipment.contract_id }}` | `{{ shipment.contract.code }}` |
| `shipment_done_list.html:39` | `#{{ s.contract_id }}` | `{{ s.contract.code }}` |
| `ombor.html:70` | `#{{ lot.contract_id }}` | `{{ lot.contract.code }}` |
| `supplier_payment_list.html:23` | `#{{ p.contract_id }}` | `{{ p.contract.code }}` |

Oxirgi to'rttasi hozir `contract_id` ni chiqaradi — join talab qilmaydi.
`contract.code` esa talab qiladi, shuning uchun tegishli view larda
`select_related("contract")` borligi tekshiriladi (aks holda 30 qatorli
ro'yxat 30 ta ortiqcha so'rov beradi).

### Audit log

`audit_list.html` barcha modellar uchun umumiy `{{ row.target_type }}
#{{ row.target_id }}` chiqaradi — uni kodga moslash har bir qator uchun turga
qarab qidiruv talab qiladi. Buning o'rniga kod izohga qo'shiladi:
`Yangi kelishuv: sobir-3 · Nurpak`. `target_id` raqamligicha qoladi.

### Qidiruv

Kelishuvlar ro'yxatidagi `q`:

- `sobir-3` → aynan shu kelishuv (slug + raqam bo'yicha aniq moslik)
- `sobir` → hamkor nomi bo'yicha allaqachon topiladi; qo'shimcha ravishda
  `code_slug` bo'yicha ham qidiriladi, shunda nomi o'zgargan hamkorning eski
  kodlari ham topiladi
- `3` → **`code_number` = 3** (ilgari `pk` = 3 edi). Raqam yozib, ekranda boshqa
  raqamli kod chiqishi chalkashtiruvchi edi.

## Migratsiya

`0019_contract_code`:

1. Ikkala ustun ham `null=True` bilan qo'shiladi.
2. Data migration: har bir kelishuv `(created, id)` bo'yicha tartiblanib,
   slug ichida 1 dan boshlab raqamlanadi (mavjud 13 ta qator).
3. Ustunlar `null=False` ga o'tkaziladi va unique constraint qo'shiladi.

`bulk_create` hech qayerda ishlatilmaydi — `import_prototype` ham,
`load_starting_data` ham `objects.create()` ishlatadi, ya'ni ular avtomatik
ravishda kod oladi, o'zgartirish shart emas.

## URL lar

O'zgarmaydi: `/contracts/12/edit/` raqamli `pk` da qoladi. Bu ichki
mexanizm — hech kim o'qimaydi, va mavjud havolalar buzilmaydi.

## Testlar

`tests/test_contracts.py` ga qo'shiladi:

- birinchi kelishuv `sobir-1` oladi; ikkinchisi `sobir-2`
- har bir hamkor 1 dan boshlaydi (`sobir-1` va `azim-1` yonma-yon)
- hamkor nomi o'zgarsa, mavjud kodlar o'zgarmaydi
- nom o'zgargach, keyingi kod hamkorning eng katta raqamidan davom etadi
- o'rtadagi kelishuv o'chirilsa, raqam qayta berilmaydi
- hamkor almashtirilsa, kod yangi hamkor nomi bilan qayta beriladi
- bir xil slugga tushadigan ikki hamkor bir-birining raqamini olmaydi
- ko'p so'zli va apostrofli nomlar to'g'ri sluglanadi
- `sobir-3`, `sobir` va `3` bo'yicha qidiruv
- migratsiyadan keyin mavjud qatorlarda kod bor (data migration testi)
