# package-crm → logistics-crm: ko'chirishga arziydigan funksiyalar

Tahlil sanasi: 2026-08-13. Taqqoslangan: `Desktop/package-crm` (crm ~14 000 satr,
73 shablon) va `Desktop/logistics-crm` (crm ~9 700 satr, 38 shablon).

Maqsad: logistika ishini yengillashtiradigan, allaqachon boshqa loyihada
ishlab turgan va kichik xatolarning oldini oladigan narsalarni topish.

> **Holat (2026-08-13):** 1-band (sana filtri bir xillashtirildi) va 2-band (Excel:
> `exports.py` kuchaytirildi + har ro'yxatga tugma) bajarildi — pastda ✅ bilan
> belgilangan. Keyingi navbatda: kelajak sanasi guardi va filtr paneli.

---

## 0. Hozirgi holat — nima bor, nima yo'q

| Sahifa | Qidiruv | Filtr paneli | Sana filtri | Excel |
|---|---|---|---|---|
| kelishuvlar (contract_list) | ✅ | ❌ | ~~❌~~ → ✅ ‹davr› bar | ~~❌~~ → ✅ |
| yuklar (shipment_list) | ✅ | ❌ | ~~❌~~ → ✅ ‹davr› bar | ~~❌~~ → ✅ |
| sotuvlar (sale_list) | ✅ | ❌ | ~~❌~~ → ✅ ‹davr› bar | ~~❌~~ → ✅ |
| mijozlar (customer_list) | ✅ | ❌ | — (holat sahifasi) | ❌ |
| hamkorlar (partner_list) | ✅ | ❌ | — (holat sahifasi) | ❌ |
| bojxona (customs_list) | ✅ | ❌ | — (holat sahifasi) | ❌ |
| logistlar (logist_list) | ✅ | ❌ | — (holat sahifasi) | ❌ |
| bronlar (reservation_list) | ✅ | ❌ | — (holat sahifasi) | ❌ |
| ombor | ✅ | ❌ | — (holat sahifasi) | ~~❌~~ → ✅ |
| mijoz to'lovlari | ✅ | ❌ | ~~2 ta input~~ → ✅ ‹davr› bar | ~~❌~~ → ✅ |
| hamkor to'lovlari | ✅ | ❌ | ~~2 ta input~~ → ✅ ‹davr› bar | ~~❌~~ → ✅ |
| qarzlar (debt_list) | ❌ | ❌ | — (holat sahifasi) | ~~❌~~ → ✅ |
| audit | ❌ | ❌ | ~~❌~~ → ✅ ‹davr› bar | ~~❌~~ → ✅ |
| kassa | ❌ | ❌ | ✅ ‹davr› bar | ~~❌~~ → ✅ (2 varaq) |
| hisobotlar (reports) | ❌ | ❌ | ~~2 ta input~~ → ✅ ‹davr› bar | ✅ 5 ta |

Ya'ni hozir: sana filtri 8 ta sahifada bir xil, Excel 9 ta ro'yxatda va har biri
sahifaning o'z filtridan o'tadi. Qolgani — **filtr paneli hech qayerda yo'q**.

"Holat sahifasi" deb belgilanganlarga sana oynasi qo'yilmadi — ular qoldiq, qarz va
balans ko'rsatadi, ya'ni **bugungi holat**. Ularga davr qo'yish "iyulda qancha qarz
bor edi" degan savolga bugungi javobni ko'rsatgan bo'lardi, bu esa yangi bag.

---

## 1. Sana filtri — bir xil bo'lsin (eng ko'p bag oldini oladi) — ✅ BAJARILDI

### Muammo (bajarilishidan oldingi holat)

Querystring nomlari sahifadan sahifaga har xil:

| View | Parametr |
|---|---|
| `supplier_payment_list` ([views.py:730](../crm/views.py:730)) | `?date_from=`, `?date_to=` |
| `customer_payment_list` ([views.py:930](../crm/views.py:930)) | `?date_from=`, `?date_to=` |
| `kassa` ([views.py:2586](../crm/views.py:2586)) | `?from=`, `?to=` |
| `reports` ([views.py:3143](../crm/views.py:3143)) | `?from=`, `?to=` |

Oqibati: kassadan "Iyul" ni tanlab to'lovlar ro'yxatiga o'tsangiz — davr yo'qoladi;
bir sahifadan olingan havolani ikkinchisiga qo'ysangiz — jimgina hammasini
ko'rsatadi. Bu foydalanuvchiga xato raqam ko'rsatadigan turdagi kichik bag.

### Nima qilindi

1. **Bitta nom — `?from=` / `?to=`.** Yangi `_date_window(request)` shu ikkitasini
   o'qiydi; eski `?date_from`/`?date_to` ham **hali ham o'qiladi**, shuning uchun
   saqlangan havola va zakladkalar buzilmadi. Ilova endi faqat yangi nomni yozadi.
2. **`_window_url(request, dan, gacha)`** — bar havolalarini Python tomonda quradi:
   sahifaning boshqa filtrlarini saqlaydi, eski sana nomlarini va **uchala sahifa
   raqamini** (`page`, `ipage`, `opage`) tashlab yuboradi. Ilgari bu bilim
   shablonlarda `{% querystring %}` ichida yozilgan edi va kassaga xos bo'lib qolgan
   edi — natijada boshqa ro'yxatda davr o'zgarganda `page=3` joyida qolardi.
3. **Bitta umumiy shablon — `templates/crm/_daterange.html`**, `{% include %}` bilan
   qo'yiladi. Kassadagi nusxa ham shunga almashtirildi.
4. **8 ta sahifada** bar bor: kassa, sotuvlar, yuklar, kelishuvlar, audit, mijoz
   to'lovlari, hamkor to'lovlari, hisobotlar. Har birida ustidagi qidiruv/filtr
   formasi davrni yashirin `from`/`to` bo'lib olib yuradi, shuning uchun select
   o'zgartirish davrni tushirib yubormaydi.
5. **Har sahifa qaysi sana bo'yicha filtrlanadi** (aniq va o'qib bo'ladigan qilib):
   sotuvlar — sotuv sanasi; kelishuvlar — kelishuv sanasi; yuklar — **yuk o'z sanasi**
   (kelgan bo'lsa `arrived`, yo'lda bo'lsa `eta` — jadval Sana ustunida nima
   yozilsa o'sha); audit — yozuv yozilgan kalendar kuni; to'lovlar — to'lov sanasi.
6. **Holat sahifalariga qo'yilmadi** (ombor, qarzlar, mijozlar, hamkorlar, logistlar,
   bojxona, bronlar) — ular bugungi qoldiq/balansni ko'rsatadi, davr ularga yolg'on
   javob berardi.
7. `tests/test_daterange_window.py` — 23 ta test: bar hamma sahifada bormi, filtr
   haqiqatan toraytiryaptimi, eski `?date_from` havolasi ishlayaptimi, strelka
   boshqa filtrlarni saqlab, sahifa raqamini tashlayaptimi, noto'g'ri sana 500
   bermayaptimi.

**Qolgan taklif:** package-crm dagi `default_window` ("today" / "month") — sahifa
qaysi davr bilan ochilishi. Hozir hamma sahifa "Hammasi" bilan ochiladi, bu to'g'ri
xatti-harakat; oylik ochilish kerak bo'lsa keyin qo'shiladi.

---

## 2. Excel — har bir ro'yxatdan, ko'rinib turgan filtr bilan — ✅ BAJARILDI

### Bajarilishidan oldingi holat

`crm/exports.py` — 28 satr, bitta funksiya ([exports.py:11](../crm/exports.py:11)):
sarlavha qalin, qolgani xom. Number format yo'q, ustun kengligi yo'q, ko'p varaq yo'q.
5 ta eksport ham faqat `/reports/export/*.xlsx` da, ro'yxat filtrlaridan mustaqil.

### package-crm da nima bor

- `_xlsx_response(filename, sheet_title, headers, rows, number_formats)` — ustunga
  format beriladi: pul `#,##0.00`, og'irlik `0.000`. Excelda raqam raqamdek turadi,
  qo'lda formatlash shart emas.
- `_xlsx_book_response(filename, sheets)` — bir kitobda bir nechta varaq
  (Kirimlar + Chiqimlar alohida tab).
- **Eksport ro'yxat filtridan o'tadi**: `sale_export` aynan ro'yxatning
  `_filter_sales(request, base)` funksiyasini chaqiradi — ekranda ko'ringan narsa
  yuklanadi. Bu "eksport boshqa raqam chiqardi" degan shikoyatning oldini oladi.
- 12 ta eksport nuqtasi: mijozlar, mijoz tarixi, qarzlar, bitta mijoz qarzi, kassa
  (kirim/chiqim/ikkalasi), ombor, mahsulot bo'yicha ombor, sotuvlar.
- **Eksport modali** (`_kassa_export_modal.html` + `kassa_export` view): davr presetlari
  (Bugun / Kecha / 7 kun / Shu oy / Hammasi) va qaysi daftar kerakligi so'raladi.
  Sahifadagi ko'rinishni buzmaydi — modal o'z davrini olib yuradi.

### Nima qilindi

1. **`crm/exports.py` qayta yozildi** (28 → 120 satr):
   - format **qiymatning o'zidan** aniqlanadi — Decimal pul (`#,##0.00`), sana esa
     haqiqiy sana (`DD.MM.YYYY`). package-crm dagi "ustun indeksi → format" lug'ati
     olinmadi: ustun qo'shilganda u jimgina sarlavhalarga mos kelmay qoladi. Kerak
     bo'lganda **sarlavha nomi bilan** ustiga yoziladi: `formats={"Kg": KG}`;
   - ustun kengligi ichidagi eng uzun qiymat bo'yicha (9–42 belgi oralig'ida);
   - sarlavha qalin va **muzlatilgan** (`freeze_panes`) — bir yillik ro'yxatni
     aylantirganda ustun nomlari ko'rinib turadi;
   - `xlsx_book_response(filename, sheets)` — ko'p varaqli kitob.
2. **9 ta ro'yxatda Excel tugmasi** — har biri **o'sha sahifaning filtr funksiyasidan**
   o'tadi: kelishuvlar, yuklar, sotuvlar, mijoz to'lovlari, hamkor to'lovlari, ombor,
   audit, kassa, qarzdorlar. Tugma URL ga joriy querystringni ulaydi, sahifa
   raqamlarini (`page`/`ipage`/`opage`) tashlaydi — fayl sahifalanmaydi.
3. **Filtrlar ajratib olindi** (`_filter_contracts`, `_filter_shipments`, `_filter_sales`,
   `_filter_customer_payments`, `_filter_supplier_payments`, `_filter_audit`,
   `_ombor_groups`, `_kassa_window` + `_kassa_ledger_rows`) — sahifa ham, eksport ham
   shu bitta funksiyani chaqiradi. "Eksport boshqa raqam chiqardi" degan bag shu bilan
   yopiladi. Ustun ta'riflari ham bitta joyda (`_contracts_table`, `_sales_table`, …),
   hisobotlar sahifasidagi 5 ta eski eksport ham shulardan foydalanadi.
4. **Kassa — ikki varaqli fayl**: Kirim va Chiqim alohida tab. Ular bir-biriga qarab
   o'qiladi, shuning uchun bitta faylda.
5. `tests/test_list_exports.py` — 17 ta test: fayl ochiladimi, davr/qidiruv/Hammasi
   toggle faylga o'tadimi, pul **raqam** bo'lib tushyaptimi (satr emas — aks holda
   Excelda yig'ib bo'lmaydi), kg 3 xona bilanmi, rol cheklovlari.

**Qolgan taklif:** kassa uchun eksport modali (Bugun / Kecha / 7 kun / Shu oy /
Hammasi presetlari bilan). Hozir tugma sahifadagi davrni oladi — bu yetarli; modal
sahifadagi ko'rinishni buzmasdan boshqa davrni yuklab olish uchun kerak bo'ladi.

---

## 3. Excel yuklash (import) — ikkala loyihada ham UI yo'q

package-crm da 6 ta import buyrug'i bor, lekin hammasi CLI:
`import_excel`, `import_hozmag`, `import_debts_from_sverka`, `import_opening_debts`,
`golive_load`, `redate_opening_debts`. logistics-crm da: `import_opening`,
`import_prototype`, `load_starting_data`.

package-crm ning `import_excel.py` dan olinadigan naqsh (yaxshi yozilgan):
- formulalar keshi eskirgan bo'lishi mumkin → **jami ustunlar qayta hisoblanadi**,
  fayldagi ЖАМИ ga ishonilmaydi;
- butun import bitta `transaction.atomic` ichida;
- fayldagi to'lovlar mijoz bo'yicha → sotuvlarga FIFO taqsimlanadi;
- ortiqcha pul "opening balance" sotuviga yoziladi, shunda import fayl bilan tiyin-tiyin mos keladi.

**Taklif (yangi ish, ikkala loyihada ham yo'q):** ilova ichida "Excel yuklash" oynasi —
fayl tanlash → **oldindan ko'rish (dry-run): nechta qator tushadi, nechtasi xato, qaysi
mijoz topilmadi** → tasdiqlash. Logistikada eng ko'p qo'l ishi ketadigan joy shu:
yuk qatorlari va to'lovlar. Dry-run bosqichi bo'lmasa import o'zi bag manbasiga aylanadi.

**Hajmi:** katta. **Foydasi:** yuqori, lekin avval 1 va 2 qilinsin.

---

## 4. Filtr paneli va chiplar — kod bor, ishlatilmayapti

`templates/base.html` da drawer JS **bor** ([base.html:1602](../templates/base.html:1602),
[:1616](../templates/base.html:1616)), CSS da `.filter-chips`, `.chip-x`, `.filter-badge`
klasslari **bor** ([app.css:1574](../static/css/app.css:1574)) — lekin hech bir shablon
`#filter-drawer` yoki `[data-filter-open]` chiqarmaydi. Ya'ni o'lik kod.

package-crm da tayyor turgan ikki parcha:
- `_filter_toolbar.html` — qidiruv + faol filtr chiplari (har birida ✕ olib tashlash) +
  ‹davr› bar + Excel tugmasi + "Filtrlash (2)" tugmasi.
- `_filter_drawer.html` — o'ng tomondan chiqadigan panel, ichida `data-combobox` li
  selectlar; sana va `q` yashirin inputlar bilan saqlanadi (filtr qo'yganda davr yo'qolmaydi).

Logistikaga mos filtrlar: hamkor, valyuta, status, logist, bojxona agenti, konteyner turi,
mas'ul xodim.

**Muhim detal (bag oldini oladi):** drawer `q` va `dan/gacha` ni yashirin input qilib
olib yuradi — hozirgi logistics ro'yxatlarida filtrni almashtirganda qidiruv so'zi
yo'qolib ketadi.

**Hajmi:** o'rtacha. **Foydasi:** yuqori (allaqachon yozilgan JS/CSS jonlanadi).

---

## 5. Kichik guardlar — arzon, lekin xato yo'lini yopadi

### 5.1 Kelajak sanasi taqiqlansin

package-crm da umumiy validator bor (`_reject_future`, forms.py:45): pul harakati
kelajak sanasi bilan yozilmaydi. Sababi aniq yozilgan: kassa sahifasi bugungi kunga
qadar sanaydi, "kassada yetarli pulmi" tekshiruvi esa hamma qatorni sanaydi — bitta
kelajak sanali qator ikkovini bir-biriga zid qilib qo'yadi.

logistics-crm da bunday guard **faqat bitta joyda**: `arrived`
([forms.py:962](../crm/forms.py:962)). Ochiq qolgan: mijoz to'lovi, hamkor to'lovi,
logist to'lovi, bojxona to'lovi, kapital, xarajatlar, sotuv sanasi.

**Taklif:** `crm/forms.py` ga bitta `reject_future(value)` validatori va uni barcha
pul/sana maydonlariga ulash. Orqaga sana yozish ochiq qolsin (eski daftar kiritiladi).

### 5.2 Muddat chipi (deadline badge)

package-crm: `{% deadline_badge sale.debt_deadline %}` → "3 kun qoldi" / "5 kun o'tgan" /
"Bugun" chipi. logistics-crm da `debt_deadline` ikkita shablonda oddiy sana bo'lib turadi
([debt_customer.html:37](../templates/crm/debt_customer.html:37),
[sale_detail.html:55](../templates/crm/sale_detail.html:55)) — holbuki modelda
`is_overdue` mantiqi allaqachon bor ([models.py:2290](../crm/models.py:2290)).
Ko'chirish ~20 satr: 1 ta inclusion_tag + 4 satrli shablon + CSS.

### 5.3 `timeago_uz` va "oxirgi harakat" sanalari

package-crm mijoz kartasida "oxirgi yuk olgan" va "oxirgi to'lov" sanalari
("12 kun oldin") turadi. Logistikada mijoz/hamkor kartasi uchun aynan mos:
kim uzoq vaqtdan beri yo'q — bir qarashda ko'rinadi.

---

## 6. Ko'chirmaslik kerak bo'lgan narsalar

- `page_url` / `qs_replace` templatetaglari — logistics-crm Django 6 ning
  o'rnatilgan `{% querystring %}` tegini ishlatadi ([_pagination.html](../templates/crm/_pagination.html)),
  bu yaxshiroq. Ko'chirilmasin.
- package-crm ning xodimlar/oylik, ishlab chiqarish, mahsulot katalogi bo'limlari —
  boshqa biznes, logistikaga tegishli emas.
- `CLAUDE.md` dagi "serverni ishga tushirma" qoidasi — u loyihaga xos, bu yerga kerak emas.

---

## 7. Tavsiya etilgan tartib

| # | Ish | Hajmi | Foydasi |
|---|---|---|---|
| ~~1~~ | ~~Sana parametrini bir xillashtirish + ‹davr› barni umumiy partial qilish~~ ✅ | O'rtacha | ★★★ |
| ~~2~~ | ~~‹davr› barni barcha ro'yxatlarga qo'yish~~ ✅ | O'rtacha | ★★★ |
| ~~3~~ | ~~`exports.py` ni kuchaytirish (format, ustun kengligi, ko'p varaq)~~ ✅ | Kichik | ★★ |
| ~~4~~ | ~~Har bir ro'yxatga Excel tugmasi (sahifa filtri bilan)~~ ✅ | O'rtacha | ★★★ |
| 5 | Kelajak sanasi guardi — barcha pul formalariga | Kichik | ★★ |
| 6 | Filtr paneli + chiplar (o'lik JS/CSS jonlansin) | O'rtacha | ★★ |
| 7 | Muddat chipi + `timeago_uz` + "oxirgi harakat" | Kichik | ★ |
| 8 | Kassa eksport modali (davr presetlari) | Kichik | ★ |
| 9 | Excel yuklash (dry-run bilan) | Katta | ★★★ |
