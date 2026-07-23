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

`Partner` ham ikkita ustun oladi:

| Ustun | Turi | Izoh |
|---|---|---|
| `code_slug` | `CharField(max_length=120, db_index=True)` | Joriy nomdan, har `save()` da qayta hisoblanadi |
| `code_counter` | `PositiveIntegerField(default=0)` | Berilgan eng katta raqam; faqat o'sadi |

Hisoblagich nega `Partner` da? Kodlar muzlatilgan bo'lishi kerak, ya'ni
`sobir-3` o'chirilsa, 3-raqam qaytib berilmasligi shart. Agar raqam mavjud
kelishuvlar ustidan `Max()` bilan hisoblansa, oxirgi qator o'chirilgach raqam
"unutiladi" va qayta beriladi. Hisoblagich qatorlardan uzoqroqda — hamkorda —
turgani uchun o'chirish unga ta'sir qilmaydi.

### Nomdan slug olish

`django.utils.text.slugify(partner.name, allow_unicode=True)`:

- `"Ali Valiyev"` → `ali-valiyev`
- `"G'ayrat"` → `gayrat` (apostrof tushadi)
- `"Шариф"` → `шариф` (kirillcha saqlanadi)

Slug bo'sh chiqsa (nom faqat tinish belgilaridan iborat bo'lsa) — `hamkor`
zaxira slugi ishlatiladi, shunda kod hech qachon `-3` ko'rinishida qolmaydi.

### Raqam berish qoidasi

```
raqam = 1 + max(shu hamkorning code_counter i,
                shu slugga ega hamkorlarning code_counter i)
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

### Eskirgan `Partner` nusxasidan himoya

`Contract.save()` hisoblagichni nuqtali `UPDATE` bilan oshiradi, shuning uchun
undan oldin o'qilgan `Partner` nusxasida eski qiymat qoladi. Oddiy
`partner.save()` (masalan, telefon raqamini tahrirlash) o'sha eski qiymatni
qaytarib yozsa, hamkorning raqamlashi nolga tushadi va keyingi kelishuv
allaqachon band kodni olishga urinadi — bu holatda qayta urinish ham yordam
bermaydi, chunki hisob har safar bir xil noto'g'ri raqamni beradi.

Shuning uchun `Partner.save()` yozishdan oldin bazadagi `code_counter` ni
o'qiydi va undan pastga tushmaydi.

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
`contract.code` esa talab qiladi; tekshirildi — barcha tegishli view larda
`select_related("contract__partner")` allaqachon bor, ortiqcha so'rov yo'q.

Bundan tashqari: ro'yxat sarlavhasi `#ID` → `KOD`, qidiruv maydonining
placeholder i "…yoki kod bo'yicha qidirish", o'chirish tasdig'i va Kassa
chiqim qatori ham kodni ko'rsatadi.

### Excel eksportlari

Mijoz Excel ni ham o'qiydi, shuning uchun u yerda ham kod chiqadi. Uch
eksportda ustun `Kelishuv ID` (raqam) → `Kelishuv` (kod) bo'ldi:
`kelishuvlar.xlsx`, `hamkor-tolovlari.xlsx`, `yuklar.xlsx`.

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
  raqamli kod chiqishi chalkashtiruvchi edi. Marka bo'yicha moslik saqlanadi —
  `209` yozilganda `LLDPE 209AA` ham topilaveradi, chunki shartlar OR bilan
  birlashadi.

## Migratsiya

`0019_contract_code`:

1. To'rtta ustun qo'shiladi (`Partner.code_slug`, `Partner.code_counter`,
   `Contract.code_slug`, `Contract.code_number`).
2. Data migration: hamkorlarga slug beriladi; kelishuvlar `(created, id)`
   bo'yicha tartiblanib, slug ichida 1 dan raqamlanadi; hamkor hisoblagichi
   o'z kelishuvlarining eng katta raqamidan boshlanadi.
3. Unique constraint qo'shiladi.

Mavjud bazada tekshirildi — 13 ta kelishuv, 4 hamkor, har biri 1 dan
boshlanadi, kodsiz qator yo'q, 13 tasi ham yagona:

```
abulqosim  counter=1  abulqosim-1
javod      counter=2  javod-1, javod-2
sobir      counter=6  sobir-1 … sobir-6
vazifadon  counter=4  vazifadon-1 … vazifadon-4
```

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
- hamkorni tahrirlash raqamlashni nolga qaytarmaydi
- band raqamga to'qnashganda `save()` qayta uriniladi
- `sobir-3`, `sobir` va `3` bo'yicha qidiruv; `209` marka bo'yicha topiladi
- uch Excel eksporti kodni chiqaradi
- audit izohi va o'chirish tasdig'i kodni nomlaydi

`tests/test_contract_codes.py` — 32 ta test. Migratsiyaning o'zi mavjud
bazada qo'lda tekshirildi (yuqoridagi natija), chunki test bazasi bo'sh
holatdan quriladi.
