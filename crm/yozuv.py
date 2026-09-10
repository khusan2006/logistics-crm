"""Uzbek Latin → Uzbek Cyrillic, on the server — the same transliteration
`static/js/yozuv.js` performs in the browser.

The browser is where the app actually converts its text, and that is right: most of
the Uzbek on screen is built in JS or arrives by fetch, so a DOM walk catches what a
response filter would miss. What the server needs it for is a different question —
**would these two strings read the same to the operator?** — and only the app's own
transliteration can answer it.

That question is not academic. A marka is a free-typed string, and `i 1561` (latin i)
renders as `и 1561`, which is byte-for-byte what somebody else typed as a cyrillic и.
Two products, one appearance, joined by nothing: see `crm/management/commands/
merge_brand.py` for what that cost.

The two implementations MUST agree, so `tests/test_yozuv_parity.py` reads the tables
straight out of yozuv.js and fails if either side is edited alone.
"""
import re

#: The same character to a reader, four different ones to a keyboard.
APOSTROPHE = "'‘’ʻʼ`"

#: o' → ў and g' → ғ. Taken before their own letters, or the apostrophe reads as a
#: hard sign of its own.
DIGRAPHS = {"o": ("ў", "Ў"), "g": ("ғ", "Ғ")}

#: Ordered pairs, taken before single letters for the same reason.
PAIRS = {"sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю",
         "ya": "я", "ye": "е"}

LETTERS = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф",
    "g": "г", "h": "ҳ", "i": "и", "j": "ж", "k": "к",
    "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "x": "х", "y": "й", "z": "з",
}

#: Spelled the way it is spelled, whatever the rules say.
KEEP = {"granulalog"}

_WORD = re.compile("[A-Za-z" + re.escape(APOSTROPHE) + "]+")


def _is_foreign(word):
    """Words the transliterator refuses — the same three rules yozuv.js applies.

    Two capitals in a row is an acronym or a code (LLDPE, HDPE, MSCU), and `c` outside
    "ch" or a `w` puts the word outside the Uzbek Latin alphabet altogether. Half a
    word in each script is worse than none."""
    if word.lower() in KEEP:
        return True
    if re.search("[A-Z]{2}", word):
        return True
    if re.search("w", word, re.I):
        return True
    return bool(re.search("c(?!h)", re.sub("C(?=H)", "Ch", word), re.I))


def _cased(cyr, latin):
    """Give `cyr` the case `latin` was written in — a digraph typed in full capitals
    (SHU) stays in full capitals."""
    if latin[0].islower():
        return cyr
    if len(latin) > 1 and latin[1].isupper():
        return cyr.upper()
    return cyr[0].upper() + cyr[1:]


def _convert_word(word):
    if _is_foreign(word):
        return word
    out, i = [], 0
    while i < len(word):
        ch = word[i]
        low = ch.lower()
        nxt = word[i + 1] if i + 1 < len(word) else ""

        if low in DIGRAPHS and nxt and nxt in APOSTROPHE:
            small, big = DIGRAPHS[low]
            out.append(small if ch == low else big)
            i += 2
            continue

        # The pair loses to an o'/g' beginning inside it: "yo'l" is y + o' (йўл),
        # not yo + a loose apostrophe.
        after = word[i + 2] if i + 2 < len(word) else ""
        swallows = nxt.lower() in ("o", "g") and after and after in APOSTROPHE
        pair = None if swallows else PAIRS.get(low + nxt.lower())
        if pair:
            out.append(_cased(pair, word[i:i + 2]))
            i += 2
            continue

        # A lone apostrophe is the hard sign: ma'lumot → маълумот.
        if ch in APOSTROPHE:
            out.append("ъ")
            i += 1
            continue

        if low in LETTERS:
            # Uzbek Cyrillic opens a word with э and uses е after it: eshik → эшик,
            # but ber → бер.
            cyr = "э" if (low == "e" and i == 0) else LETTERS[low]
            out.append(cyr if ch == low else cyr.upper())
            i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def to_kiril(text):
    """Uzbek Latin → Uzbek Cyrillic.

    Idempotent by construction: only Latin runs are matched, so text that is already
    Cyrillic comes back unchanged."""
    if not text:
        return ""
    return _WORD.sub(lambda m: _convert_word(m.group(0)), str(text))


def reads_the_same(one, other):
    """Would these two strings be INDISTINGUISHABLE on screen?

    True only for strings that differ — a name is not a lookalike of itself. Compared
    in kiril because that is the app's default script and the direction the collision
    runs: latin `i` becomes `и`, while a cyrillic и was already there."""
    one, other = (one or "").strip(), (other or "").strip()
    return one != other and to_kiril(one) == to_kiril(other)


# ── Marka nomlari ─────────────────────────────────────────────────────────────────
#
# Server-only, and deliberately outside the parity with yozuv.js: the browser never
# rewrites a marka (its pickers carry data-lotin), so there is nothing for it to agree
# with. These answer the question ContractLineForm asks the moment a marka is typed —
# is this a name already on the books, written another way?

#: Words `to_kiril` refuses (a `c` outside "ch") that markalar are nonetheless made
#: of. Spelled out by hand for the reason migration 0070 gives: `c` is к in campaund
#: and с in repac, no rule can read that, and `2102 репас` is kept apart from
#: `2102 репак` on purpose.
MARKA_SOZLARI = {"campaund": "кампаунд", "repac": "репас"}


def _marka_sozi(word, fold):
    """One whitespace-separated token of a marka.

    A token carrying a digit is a grade or a code — 1561, 7000F, 209AA — and is kept
    letter for letter: `to_kiril` would turn the lone F of 7000F into Ф. The words
    around it are Uzbek, and go to kiril."""
    if any(ch.isdigit() for ch in word):
        return word.casefold() if fold else word
    base = MARKA_SOZLARI.get(word.casefold(), word.casefold() if fold else word)
    kiril = to_kiril(base)
    return kiril.casefold() if fold else kiril


def marka_nomi(typed):
    """How a NEW marka is stored: one space between tokens, words in kiril, codes as
    typed. `9000  repak` → `9000 репак`; `LLDPE` and `7000F` stay what they are."""
    return " ".join(_marka_sozi(word, fold=False) for word in (typed or "").split())


def marka_kaliti(name):
    """What every spelling of one marka has in common: `7000 repak`, `7000 Repak`,
    `7000 REPAK` and `7000  репак` all give `7000 репак`.

    Case is folded BEFORE transliterating, not after: in capitals `REPAK` reads as a
    code and `to_kiril` would leave it in lotin, where it could never meet `репак`."""
    return " ".join(_marka_sozi(word, fold=True) for word in (name or "").split())
