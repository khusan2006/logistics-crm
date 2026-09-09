/* Yozuv — kiril / lotin.
 *
 * The app is written in Uzbek Latin, in the templates and in base.html's own JS,
 * with no gettext anywhere: there is no catalogue to translate, and there never
 * needs to be one. Uzbek Latin and Uzbek Cyrillic are the same language in two
 * alphabets, so the second script is a TRANSLITERATION of the first rather than a
 * translation of it — one function, applied to whatever text is on the page.
 *
 * It runs in the browser rather than on the server because a good part of the
 * Uzbek on screen never passes through a template: base.html builds reys headers,
 * confirm messages and money previews in JS, and every modal arrives by fetch after
 * the page has loaded. A response filter would convert the first paint and miss all
 * of that. A DOM walk plus a MutationObserver catches everything that ever enters
 * the page, whoever wrote it.
 *
 * Kiril is the default; `localStorage.yozuv === 'lotin'` turns it off. The choice is
 * read before the first paint (see base.html), the same way the theme is.
 */
(function (root) {
  'use strict';

  // --- the alphabet --------------------------------------------------------
  //
  // Ordered longest-first and applied in this order: the digraphs have to be taken
  // before their own letters are, or "sh" is read as s + h ("сҳ") and "o'" as o
  // followed by a stray apostrophe.
  //
  // The four apostrophes are all the same character to a reader and four different
  // ones to a keyboard: the templates type ASCII ', the Windows keyboard produces
  // the typographic ' and ', and Unicode's own answer is the modifier letter ʻ.
  var APOSTROPHE = "'‘’ʻʼ`";

  var DIGRAPHS = [
    ["o", "ў", "Ў"],   // o' → ў / Ў   (apostrophe form, handled below)
    ["g", "ғ", "Ғ"],   // g' → ғ / Ғ
  ];

  var PAIRS = [
    ["sh", "ш"], ["ch", "ч"],
    ["yo", "ё"], ["yu", "ю"], ["ya", "я"], ["ye", "е"],
  ];

  var LETTERS = {
    a: "а", b: "б", d: "д", e: "е", f: "ф",
    g: "г", h: "ҳ", i: "и", j: "ж", k: "к",
    l: "л", m: "м", n: "н", o: "о", p: "п",
    q: "қ", r: "р", s: "с", t: "т", u: "у",
    v: "в", x: "х", y: "й", z: "з",
  };

  // A word is letters and the apostrophes that belong inside them ("bo'sh",
  // "ma'lumot"). Everything between words — digits, punctuation, spaces, the
  // Cyrillic a previous pass already wrote — is carried through untouched.
  var WORD = new RegExp("[A-Za-z" + APOSTROPHE + "]+", "g");
  var IS_APOSTROPHE = new RegExp("[" + APOSTROPHE + "]");

  //: Names that are spelled the way they are spelled. `data-lotin` covers the ones
  //: sitting in an element of their own; <title> is a single string and cannot be
  //: marked up, so the app's own name is kept here as well.
  var KEEP = { granulalog: 1 };

  /** Words this must not touch, however Uzbek they look. */
  function isForeign(word) {
    if (KEEP[word.toLowerCase()]) { return true; }
    // Two capitals in a row is an acronym or a code, not a word: LLDPE, HDPE, QR,
    // MSCU, USD. Transliterating one produces a string nobody can search for.
    if (/[A-Z]{2}/.test(word)) { return true; }
    // `c` outside "ch" and `w` are not in the Uzbek Latin alphabet at all, so a word
    // carrying either is a foreign one — a marka name, a container prefix. Guessing
    // at half of it and leaving the rest would spell the word in two scripts.
    if (/w/i.test(word)) { return true; }
    if (/c(?!h)/i.test(word.replace(/C(?=H)/g, "Ch"))) { return true; }
    return false;
  }

  /** Give `cyr` the case `latin` was written in. */
  function cased(cyr, latin) {
    if (latin[0] !== latin[0].toLowerCase()) {
      // A digraph typed in full capitals (SHU) stays in full capitals.
      return latin.length > 1 && latin[1] && latin[1] === latin[1].toUpperCase()
        && latin[1] !== latin[1].toLowerCase()
        ? cyr.toUpperCase()
        : cyr[0].toUpperCase() + cyr.slice(1);
    }
    return cyr;
  }

  function convertWord(word) {
    if (isForeign(word)) { return word; }
    var out = "";
    var i = 0;
    while (i < word.length) {
      var ch = word[i];
      var low = ch.toLowerCase();
      var next = word[i + 1] || "";

      // o' and g' — the apostrophe is part of the letter, not a sign of its own
      var digraph = null;
      for (var d = 0; d < DIGRAPHS.length; d++) {
        if (low === DIGRAPHS[d][0] && IS_APOSTROPHE.test(next)) { digraph = DIGRAPHS[d]; }
      }
      if (digraph) {
        out += (ch === low) ? digraph[1] : digraph[2];
        i += 2;
        continue;
      }

      // sh, ch, yo, yu, ya, ye — unless the pair's own second letter is the start of
      // an o'/g', which is the longer letter and wins. "yo'l" is y + o' ("йўл"), not
      // yo + a loose apostrophe ("ёъл"); "sho'r" is unaffected, because there the
      // o' begins after the pair rather than inside it.
      var pair = null;
      var swallowsDigraph = (next === "o" || next === "g" || next === "O" || next === "G")
        && IS_APOSTROPHE.test(word[i + 2] || "");
      for (var p = 0; p < PAIRS.length && !swallowsDigraph; p++) {
        if (low + next.toLowerCase() === PAIRS[p][0]) { pair = PAIRS[p]; }
      }
      if (pair) {
        out += cased(pair[1], word.slice(i, i + 2));
        i += 2;
        continue;
      }

      // A lone apostrophe is the hard sign: ma'lumot → маълумот, san'at → санъат.
      if (IS_APOSTROPHE.test(ch)) {
        out += "ъ";
        i += 1;
        continue;
      }

      if (LETTERS[low]) {
        // Uzbek Cyrillic opens a word with э and uses е everywhere after it:
        // "eshik" → "эшик", but "ber" → "бер".
        var cyr = (low === "e" && i === 0) ? "э" : LETTERS[low];
        out += (ch === low) ? cyr : cyr.toUpperCase();
        i += 1;
        continue;
      }

      out += ch;
      i += 1;
    }
    return out;
  }

  /**
   * Uzbek Latin → Uzbek Cyrillic.
   *
   * Idempotent by construction: only Latin letters are matched, so text that is
   * already Cyrillic comes back unchanged. That is what lets the MutationObserver
   * below re-run over anything without having to remember what it has seen.
   */
  function toKiril(text) {
    if (!text || !/[A-Za-z]/.test(text)) { return text; }
    return text.replace(WORD, convertWord);
  }

  // --- applying it to the page ---------------------------------------------

  // Never entered: <script>/<style> hold code, and an input's VALUE is data on its
  // way back to the server — converting one would post Cyrillic into the database
  // the next time the operator pressed Saqlash. `[data-lotin]` is the opt-out for a
  // name that is spelled the way it is spelled (the app's own, above all).
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };
  //: attributes that are read by a human rather than by the app
  var ATTRS = ["placeholder", "title", "aria-label", "data-line-reys", "alt"];

  function skipped(node) {
    for (var el = node; el; el = el.parentNode) {
      if (el.nodeType !== 1) { continue; }
      if (SKIP[el.nodeName] || el.hasAttribute("data-lotin")) { return true; }
    }
    return false;
  }

  var busy = false;

  function convertElement(el) {
    for (var a = 0; a < ATTRS.length; a++) {
      var name = ATTRS[a];
      if (!el.hasAttribute(name)) { continue; }
      var was = el.getAttribute(name);
      var now = toKiril(was);
      if (now !== was) { el.setAttribute(name, now); }
    }
  }

  // FILTER_REJECT drops the node AND everything under it, which is the whole reason
  // the walk is built this way: a <script> or a [data-lotin] is refused once, at its
  // root, instead of every text node inside it being tested on the way past.
  var FILTER = {
    acceptNode: function (node) {
      if (node.nodeType === 1) {
        return (SKIP[node.nodeName] || node.hasAttribute("data-lotin"))
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  };

  function convertTree(node) {
    if (!node || skipped(node)) { return; }
    if (node.nodeType === 3) {
      var now = toKiril(node.nodeValue);
      if (now !== node.nodeValue) { node.nodeValue = now; }
      return;
    }
    if (node.nodeType !== 1) { return; }
    convertElement(node);
    var walker = document.createTreeWalker(
      node, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, FILTER);
    var current;
    while ((current = walker.nextNode())) {
      if (current.nodeType === 1) {
        convertElement(current);
      } else {
        var text = toKiril(current.nodeValue);
        if (text !== current.nodeValue) { current.nodeValue = text; }
      }
    }
  }

  function convertPage() {
    if (busy) { return; }
    busy = true;
    try {
      document.title = toKiril(document.title);
      convertTree(document.body);
    } finally {
      busy = false;
    }
  }

  function watch() {
    if (!root.MutationObserver) { return; }
    var observer = new MutationObserver(function (records) {
      if (busy) { return; }
      busy = true;
      try {
        for (var i = 0; i < records.length; i++) {
          var record = records[i];
          if (record.type === "characterData") {
            if (!skipped(record.target.parentNode)) {
              var text = toKiril(record.target.nodeValue);
              if (text !== record.target.nodeValue) { record.target.nodeValue = text; }
            }
            continue;
          }
          for (var n = 0; n < record.addedNodes.length; n++) {
            convertTree(record.addedNodes[n]);
          }
          if (record.type === "attributes" && record.target.nodeType === 1
              && !skipped(record.target)) {
            convertElement(record.target);
          }
        }
      } finally {
        busy = false;
      }
    });
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
  }

  root.Yozuv = {
    KIRIL: "kiril",
    LOTIN: "lotin",
    toKiril: toKiril,
    /** Which script the page should be in. Kiril unless it was turned off. */
    current: function () {
      try {
        return localStorage.getItem("yozuv") === "lotin" ? "lotin" : "kiril";
      } catch (e) {
        return "kiril";
      }
    },
    /**
     * Convert what is on the page now and keep converting whatever arrives later.
     * Nothing here can turn Cyrillic back into Latin — switching to lotin reloads,
     * which is both simpler and the only way to be sure the page is the server's
     * own text rather than a round trip through two alphabets.
     */
    start: function () {
      convertPage();
      watch();
    },
  };
})(window);
