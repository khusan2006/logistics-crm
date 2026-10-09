/* Tarozi sinovi — read a weighing scale from the browser and show what it sends.
 *
 * Two halves. `Core` is pure: it turns bytes into frames and frames into a weight,
 * for the formats scale indicators commonly use. It touches no DOM, so it can be
 * reused by the kg fields later and checked from node. The page controller below
 * it wires Core to the transports a browser can open — Web Serial (USB receiver,
 * RS232 adapter, paired Bluetooth Classic) and Web Bluetooth (BLE) — plus pasted
 * text for anything else (TCP, a Hercules screenshot, a keyboard-mode receiver).
 */
(function (root) {
  "use strict";

  var STX = 0x02, ETX = 0x03, CR = 0x0d, LF = 0x0a;
  var NAMES = { 0: "NUL", 2: "STX", 3: "ETX", 4: "EOT", 6: "ACK", 9: "TAB",
                10: "LF", 13: "CR", 21: "NAK", 27: "ESC" };

  function hex2(b) { return (b < 16 ? "0" : "") + b.toString(16).toUpperCase(); }

  function toHex(bytes) {
    var out = [];
    for (var i = 0; i < bytes.length; i++) out.push(hex2(bytes[i]));
    return out.join(" ");
  }

  /* Printable ASCII as is; everything else named, so an STX or a CR is seen. */
  function toVisible(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) {
      var b = bytes[i];
      s += (b >= 0x20 && b < 0x7f) ? String.fromCharCode(b) : "[" + (NAMES[b] || hex2(b)) + "]";
    }
    return s;
  }

  function latin1(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return s;
  }

  /* "ST,GS\r\n" or "\x02+012450019\x03" typed by hand → bytes. */
  function parseEscaped(text) {
    var out = [];
    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (c === "\\" && i + 1 < text.length) {
        var n = text[i + 1];
        if (n === "r") { out.push(CR); i++; continue; }
        if (n === "n") { out.push(LF); i++; continue; }
        if (n === "t") { out.push(9); i++; continue; }
        if (n === "\\") { out.push(0x5c); i++; continue; }
        if (n === "x" && /^[0-9a-fA-F]{2}$/.test(text.substr(i + 2, 2))) {
          out.push(parseInt(text.substr(i + 2, 2), 16)); i += 3; continue;
        }
      }
      var code = c.charCodeAt(0);
      out.push(code < 256 ? code : 0x3f);
    }
    return new Uint8Array(out);
  }

  function parseHex(text) {
    var clean = text.replace(/0x/gi, "").replace(/[^0-9a-fA-F]/g, "");
    if (clean.length % 2) throw new Error("Hex belgilar soni juft emas: " + clean.length);
    var out = new Uint8Array(clean.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
    return out;
  }

  function indexOfAny(buf, codes, from) {
    for (var i = from || 0; i < buf.length; i++) {
      if (codes.indexOf(buf[i]) !== -1) return i;
    }
    return -1;
  }

  /* Pick a framing from what is in the buffer: STX/ETX beats line ends beats a
     start character. Nothing recognisable yet → keep waiting. */
  function guessFraming(buf, opts) {
    if (buf.indexOf(STX) !== -1) return "stxetx";
    if (buf.indexOf(CR) !== -1 || buf.indexOf(LF) !== -1) return "line";
    var start = (opts.startChar || "").charCodeAt(0);
    if (start && buf.indexOf(start) !== -1) return "start";
    return null;
  }

  /* Cut complete frames off the front of `buf` (a plain array of bytes).
     Returns {frames, rest, framing}; `rest` is the unfinished tail to keep. */
  function splitFrames(buf, opts) {
    var framing = opts.framing === "auto" ? guessFraming(buf, opts) : opts.framing;
    var frames = [], i, j;
    if (framing === "line") {
      for (;;) {
        i = indexOfAny(buf, [CR, LF]);
        if (i === -1) break;
        if (i > 0) frames.push(new Uint8Array(buf.slice(0, i)));
        buf = buf.slice(i + 1);
      }
    } else if (framing === "stxetx") {
      for (;;) {
        i = buf.indexOf(STX);
        if (i === -1) { buf = []; break; }          // noise before any STX
        j = buf.indexOf(ETX, i + 1);
        if (j === -1) { buf = buf.slice(i); break; }
        frames.push(new Uint8Array(buf.slice(i, j + 1)));
        buf = buf.slice(j + 1);
      }
    } else if (framing === "start") {
      var s = (opts.startChar || "=").charCodeAt(0);
      for (;;) {
        i = buf.indexOf(s);
        if (i === -1) { buf = []; break; }
        j = buf.indexOf(s, i + 1);
        if (j === -1) { buf = buf.slice(i); break; }
        frames.push(new Uint8Array(buf.slice(i, j)));
        buf = buf.slice(j);
      }
    } else if (framing === "fixed") {
      var n = Math.max(1, opts.fixedLen | 0);
      while (buf.length >= n) {
        frames.push(new Uint8Array(buf.slice(0, n)));
        buf = buf.slice(n);
      }
    } else if (framing === "chunk") {
      if (buf.length) frames.push(new Uint8Array(buf));
      buf = [];
    }
    // A stream that never matches must not grow without end.
    if (buf.length > 4096) buf = buf.slice(-1024);
    return { frames: frames, rest: buf, framing: framing };
  }

  function toKg(value, unit) {
    var u = (unit || "kg").toLowerCase();
    if (u === "g") return { kg: value / 1000, unit: "g" };
    if (u === "t") return { kg: value * 1000, unit: "t" };
    return { kg: value, unit: u };
  }

  function num(str) { return parseFloat(String(str).replace(",", ".").replace(/\s+/g, "")); }

  function printable(frame) { return latin1(frame).replace(/[\x00-\x1f\x7f-\xff]/g, ""); }

  /* Yaohua XK3190-A9 continuous mode: STX, sign, 6 digits, decimal position, two
     XOR check characters over bytes 2–9, ETX. The manual gives the check as "high
     4 bits, low 4 bits" without saying how each is written, so both common
     spellings are accepted: a hex digit, or the nibble plus 0x30. */
  function decodeA9(frame) {
    if (frame.length !== 12 || frame[0] !== STX || frame[11] !== ETX) {
      return { ok: false, error: "A9 kadri emas (12 bayt, STX…ETX kerak)" };
    }
    var body = latin1(frame.slice(1, 9));
    if (!/^[+-]\d{6}[0-4]$/.test(body)) return { ok: false, error: "A9 tanasi noto'g'ri: " + body };
    var x = 0;
    for (var i = 1; i < 9; i++) x ^= frame[i];
    var hi = x >> 4, lo = x & 0xf;
    var got = latin1(frame.slice(9, 11));
    var asHex = hex2(x);
    var asNibble = String.fromCharCode(0x30 + hi) + String.fromCharCode(0x30 + lo);
    var value = parseInt(body.substr(1, 6), 10) / Math.pow(10, +body[7]);
    if (body[0] === "-") value = -value;
    var checked = got === asHex || got === asNibble;
    return { ok: checked, kg: value, unit: "kg", flag: null, format: "Yaohua A9",
             error: checked ? null : "XOR mos emas (keldi " + got + ", kutilgan " + asHex + ")" };
  }

  /* "ST,GS,+0012450kg" and its relatives: ST/US/OL, then GS/NT, then the number. */
  var TEXT_RE = /\b(ST|US|OL)\b\s*,?\s*(GS|NT|TR|GW|NW|G|N)?\s*,?\s*([+-])?\s*(\d+(?:[.,]\d+)?)\s*(kg|g|t|lb)?/i;

  function decodeText(frame) {
    var s = printable(frame);
    var m = TEXT_RE.exec(s);
    if (!m) return { ok: false, error: "ST/US formati topilmadi" };
    var flag = m[1].toUpperCase();
    if (flag === "OL") return { ok: false, flag: "OL", format: "ST/US matn", error: "Ortiqcha yuk (OL)" };
    var v = num(m[4]);
    if (m[3] === "-") v = -v;
    var k = toKg(v, m[5]);
    return { ok: true, kg: k.kg, unit: k.unit, flag: flag, kind: (m[2] || "").toUpperCase(),
             format: "ST/US matn" };
  }

  /* Indicators like the XK3190-A12 send "=" and the digits lowest first. */
  function decodeA12(frame) {
    var s = printable(frame).replace(/^=/, "");
    var rev = s.split("").reverse().join("");
    var m = /([+-])?\s*(\d+(?:\.\d+)?)/.exec(rev);
    if (!m) return { ok: false, error: "Teskari son topilmadi" };
    var v = num(m[2]);
    if (m[1] === "-") v = -v;
    return { ok: true, kg: v, unit: "kg", flag: null, format: "Teskari raqamlar (=)" };
  }

  /* Last resort: the first number in the frame, a flag if one is lying about. */
  function decodeNumber(frame, opts) {
    var s = printable(frame);
    if (opts.reverse) s = s.split("").reverse().join("");
    var m = /([+-])?\s*(\d+(?:[.,]\d+)?)\s*(kg|g|t|lb)?/i.exec(s);
    if (!m) return { ok: false, error: "Son topilmadi" };
    var v = num(m[2]);
    if (m[1] === "-") v = -v;
    var k = toKg(v, m[3]);
    var f = /\b(ST|US)\b/i.exec(s);
    return { ok: true, kg: k.kg, unit: k.unit, flag: f ? f[1].toUpperCase() : null,
             format: opts.reverse ? "Son (teskari)" : "Son" };
  }

  function decodeFrame(frame, opts) {
    var d = opts.decoder;
    if (d === "a9") return decodeA9(frame);
    if (d === "text") return decodeText(frame);
    if (d === "a12") return decodeA12(frame);
    if (d === "number") return decodeNumber(frame, opts);
    // auto
    if (frame.length === 12 && frame[0] === STX) return decodeA9(frame);
    if (TEXT_RE.test(printable(frame))) return decodeText(frame);
    if (frame[0] === 0x3d) return decodeA12(frame);              // "="
    return decodeNumber(frame, opts);
  }

  /* A weight is barqaror when the scale says ST, or — when it says nothing — when
     the same value has arrived `need` times in a row. */
  function Stability(need) {
    this.need = need || 5;
    this.last = null;
    this.repeat = 0;
    this.flag = null;
  }
  Stability.prototype.push = function (kg, flag) {
    if (this.last !== null && kg === this.last) this.repeat++;
    else { this.repeat = 1; this.last = kg; }
    this.flag = flag || null;
    return this.stable();
  };
  Stability.prototype.stable = function () {
    if (this.last === null) return false;
    if (this.flag === "ST") return true;
    if (this.flag === "US") return false;
    return this.repeat >= this.need;
  };

  /* No trailing zeros, NBSP between thousands — the app-wide number rule. */
  function fmtKg(n) {
    var neg = n < 0;
    var s = Math.abs(n).toFixed(3).replace(/\.?0+$/, "");
    var parts = s.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return (neg ? "-" : "") + parts.join(".");
  }

  var Core = {
    toHex: toHex, toVisible: toVisible, parseEscaped: parseEscaped, parseHex: parseHex,
    splitFrames: splitFrames, decodeFrame: decodeFrame, Stability: Stability, fmtKg: fmtKg
  };
  root.TaroziCore = Core;
  if (typeof module !== "undefined" && module.exports) module.exports = Core;

  if (typeof document === "undefined") return;

  // ------------------------------------------------------------------ page ----
  //
  // Every source of bytes is a SESSION: a serial port (several at once, e.g. scales
  // on a USB hub), a BLE device, or the manual box. Each keeps its own buffer,
  // stability and counters, so two scales never mix their frames. The big result
  // panel follows the ACTIVE session; the list shows every one of them live.

  var VENDORS = { 0x1a86: "CH340 (QinHeng)", 0x10c4: "CP210x (Silicon Labs)",
                  0x0403: "FTDI", 0x067b: "PL2303 (Prolific)", 0x2341: "Arduino" };
  var STORE_KEY = "tarozi-test-v1";
  var LOG_MAX = 500;
  var KIND_LABEL = { serial: "COM", ble: "BLE", manual: "Qo'lda" };

  function init(el) {
    var $ = function (sel) { return el.querySelector(sel); };
    var $$ = function (sel) { return Array.prototype.slice.call(el.querySelectorAll(sel)); };

    var sessions = [];
    var active = null;
    var seq = 0;
    var log = [];

    // ---- settings -----------------------------------------------------------
    var setEls = {};
    $$("[data-tz-set]").forEach(function (inp) { setEls[inp.dataset.tzSet] = inp; });

    function opt(name) {
      var inp = setEls[name];
      return inp.type === "checkbox" ? inp.checked : inp.value;
    }
    function opts() {
      return { framing: opt("framing"), decoder: opt("decoder"), startChar: opt("startChar"),
               fixedLen: +opt("fixedLen"), reverse: opt("reverse") };
    }
    function need() { return Math.max(1, +opt("stableCount") || 5); }
    function saveSettings() {
      var data = {};
      Object.keys(setEls).forEach(function (k) { data[k] = opt(k); });
      try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); } catch (e) { /* private window */ }
    }
    function loadSettings() {
      var data = null;
      try { data = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (e) { data = null; }
      if (!data) return;
      Object.keys(data).forEach(function (k) {
        var inp = setEls[k];
        if (!inp) return;
        if (inp.type === "checkbox") inp.checked = !!data[k]; else inp.value = data[k];
      });
    }
    loadSettings();

    // Only the reading settings restart the reading; baud and the rest apply the
    // next time a port is opened.
    var PARSE_KEYS = ["framing", "decoder", "startChar", "fixedLen", "stableCount", "reverse"];
    Object.keys(setEls).forEach(function (k) {
      setEls[k].addEventListener("change", function () {
        saveSettings();
        if (PARSE_KEYS.indexOf(k) === -1) return;
        sessions.forEach(function (s) { s.buf = []; s.stability = new Stability(need()); s.last = null; });
        renderAll();
      });
    });

    var preset = $("[data-tz-ble-preset]");
    preset.addEventListener("change", function () {
      if (!preset.value) return;
      var p = preset.value.split("|");
      setEls.bleService.value = p[0];
      setEls.bleChar.value = p[1];
      saveSettings();
    });

    // ---- tabs ---------------------------------------------------------------
    $$("[data-tz-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        $$("[data-tz-tab]").forEach(function (t) { t.classList.toggle("is-on", t === tab); });
        $$("[data-tz-pane]").forEach(function (p) { p.hidden = p.dataset.tzPane !== tab.dataset.tzTab; });
      });
    });

    // ---- support banner -----------------------------------------------------
    var support = [];
    if (!window.isSecureContext) support.push("Sahifa HTTPS orqali ochilishi kerak — aks holda brauzer portlarga ruxsat bermaydi.");
    if (!("serial" in navigator)) support.push("Bu brauzerda Web Serial yo'q: USB / Bluetooth (COM) ishlamaydi. Kompyuterda Chrome yoki Edge kerak.");
    if (!("bluetooth" in navigator)) support.push("Bu brauzerda Web Bluetooth yo'q: Bluetooth LE ishlamaydi.");
    if (support.length) {
      var sb = $("[data-tz-support]");
      sb.textContent = support.join(" ");
      sb.hidden = false;
    }

    var statusEl = $("[data-tz-status]");
    function setStatus(text, tone) {
      statusEl.textContent = text;
      statusEl.className = "tz-status" + (tone ? " tz-status--" + tone : "");
    }

    // ---- sessions -------------------------------------------------------------
    function newSession(kind, extra) {
      var count = sessions.filter(function (s) { return s.kind === kind; }).length + 1;
      var s = { id: ++seq, kind: kind,
                label: kind === "manual" ? "Qo'lda" : (kind === "ble" ? "BLE " : "Port ") + count,
                port: null, device: null, ch: null, reader: null, keepReading: false, closed: null,
                buf: [], stability: new Stability(need()), bytes: 0, frames: 0, bad: 0,
                last: null, lastRes: null, lastFrame: null, connected: false, info: "", settings: "",
                row: null };
      Object.keys(extra || {}).forEach(function (k) { s[k] = extra[k]; });
      sessions.push(s);
      buildRow(s);
      if (!active) setActive(s);
      return s;
    }

    function removeSession(s) {
      sessions = sessions.filter(function (x) { return x !== s; });
      if (s.row) s.row.remove();
      if (active === s) setActive(sessions[0] || null);
      renderListEmpty();
    }

    function sessionForPort(port) {
      return sessions.filter(function (s) { return s.port === port; })[0] || null;
    }

    function describePort(port) {
      var info = port.getInfo();
      if (info.bluetoothServiceClassId) return "Bluetooth (" + info.bluetoothServiceClassId + ")";
      if (info.usbVendorId) {
        return (VENDORS[info.usbVendorId] || "USB")
          + " · VID " + info.usbVendorId.toString(16) + " PID " + (info.usbProductId || 0).toString(16);
      }
      return "Port";
    }

    function setActive(s) {
      active = s;
      sessions.forEach(function (x) { if (x.row) x.row.classList.toggle("is-active", x === s); });
      renderActive();
      if ($("[data-tz-log-active]").checked) renderLog();
    }

    // ---- sessions list (one row per scale) ---------------------------------------
    var listEl = $("[data-tz-sessions]");

    function renderListEmpty() {
      var empty = listEl.querySelector(".tz-empty");
      if (sessions.length && empty) empty.remove();
      if (!sessions.length && !empty) {
        listEl.insertAdjacentHTML("beforeend", '<div class="tz-empty">Hali port yo\'q. "Yangi port qo\'shish" ni bosing.</div>');
      }
    }

    function buildRow(s) {
      var row = document.createElement("div");
      row.className = "tz-srow";
      row.innerHTML =
        '<span class="tz-sdot"></span>' +
        '<input type="text" class="tz-sname" aria-label="Nomi">' +
        '<span class="tz-sinfo"></span>' +
        '<span class="tz-sweight"></span>' +
        '<span class="tz-sbtns">' +
        '<button type="button" class="btn btn-sm" data-act="connect">Ulash</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-act="disconnect">Uzish</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-act="show">Ko\'rsatish</button>' +
        '<button type="button" class="btn btn-danger btn-sm" data-act="forget" title="Ro\'yxatdan olib tashlash">✕</button>' +
        '</span>';
      var name = row.querySelector(".tz-sname");
      name.value = s.label;
      name.addEventListener("input", function () {
        s.label = name.value || "—";
        if (active === s) renderActive();
      });
      row.addEventListener("click", function (e) {
        var act = e.target.dataset && e.target.dataset.act;
        if (act === "connect") connectSession(s);
        else if (act === "disconnect") disconnectSession(s);
        else if (act === "forget") forgetSession(s);
        else if (e.target !== name) setActive(s);
      });
      s.row = row;
      listEl.appendChild(row);
      renderListEmpty();
      updateRow(s);
    }

    function updateRow(s) {
      if (!s.row) return;
      var r = s.row;
      r.classList.toggle("is-active", s === active);
      r.querySelector(".tz-sdot").className = "tz-sdot" + (s.connected ? " is-on" : "");
      r.querySelector(".tz-sinfo").textContent = KIND_LABEL[s.kind] + " · " + (s.info || "—")
        + (s.connected ? " · " + (s.settings || "ulangan") : " · ulanmagan");
      var w = r.querySelector(".tz-sweight");
      if (s.last && s.last.ok) {
        var stable = s.stability.stable();
        w.textContent = fmtKg(s.last.kg) + " kg " + (stable ? "✓" : "~");
        w.className = "tz-sweight " + (stable ? "is-stable" : "is-moving");
      } else {
        w.textContent = s.bytes ? "kadr yo'q" : "—";
        w.className = "tz-sweight";
      }
      var canLink = s.kind !== "manual";
      r.querySelector('[data-act="connect"]').hidden = !canLink || s.connected;
      r.querySelector('[data-act="disconnect"]').hidden = !canLink || !s.connected;
    }

    // ---- result panel (the active session) ------------------------------------------
    function renderActive() {
      var s = active;
      $("[data-tz-active]").textContent = s ? s.label + " · " + KIND_LABEL[s.kind] : "—";
      $("[data-tz-send]").hidden = !(s && s.kind === "serial" && s.connected);
      var res = s && s.lastRes;
      $("[data-tz-frames]").textContent = s ? s.frames : 0;
      $("[data-tz-bad]").textContent = s ? s.bad : 0;
      $("[data-tz-bytes]").textContent = s ? s.bytes : 0;
      $("[data-tz-last]").textContent = s && s.lastFrame ? toVisible(s.lastFrame) : "—";
      $("[data-tz-format]").textContent = res && res.format || "—";
      $("[data-tz-flag]").textContent = !res ? "—" : res.flag ? res.flag + (res.kind ? " · " + res.kind : "") : "yo'q";
      var pill = $("[data-tz-stable]");
      if (!s || !s.last || !s.last.ok) {
        $("[data-tz-weight]").textContent = "—";
        $("[data-tz-unit]").textContent = "kg";
        $("[data-tz-repeat]").textContent = "0";
        pill.textContent = "—";
        pill.className = "tz-pill";
        return;
      }
      $("[data-tz-weight]").textContent = fmtKg(s.last.kg);
      $("[data-tz-unit]").textContent = s.last.unit && s.last.unit !== "kg" ? "kg (keldi: " + s.last.unit + ")" : "kg";
      $("[data-tz-repeat]").textContent = s.stability.repeat;
      var stable = s.stability.stable();
      pill.textContent = stable ? "Barqaror" : "O'zgaryapti";
      pill.className = "tz-pill " + (stable ? "tz-pill--ok" : "tz-pill--wait");
    }

    function renderAll() {
      sessions.forEach(updateRow);
      renderActive();
    }

    // ---- the pipeline: bytes → frames → weight, per session -----------------------------
    function handleFrames(s, frames) {
      var o = opts();
      frames.forEach(function (frame) {
        s.frames++;
        s.lastFrame = frame;
        var res = decodeFrame(frame, o);
        s.lastRes = res;
        if (res.ok) {
          s.stability.push(res.kg, res.flag);
          s.last = res;
        } else {
          s.bad++;
          if (res.error) addLog(s, "note", null, "Kadr o'qilmadi: " + res.error);
        }
      });
      updateRow(s);
      if (s === active) renderActive();
    }

    function onBytes(s, bytes, kind) {
      s.bytes += bytes.length;
      addLog(s, kind || "rx", bytes);
      var o = opts();
      if (o.framing === "chunk") { handleFrames(s, [bytes]); return; }
      for (var i = 0; i < bytes.length; i++) s.buf.push(bytes[i]);
      var r = splitFrames(s.buf, o);
      s.buf = r.rest;
      handleFrames(s, r.frames);
    }

    /* Pasted text has no "next frame" to end the last one — read what is left. */
    function flush(s) {
      if (!s.buf.length) return;
      var frame = new Uint8Array(s.buf);
      s.buf = [];
      handleFrames(s, [frame]);
    }

    // ---- log ----------------------------------------------------------------
    var logEl = $("[data-tz-log]");
    var paused = $("[data-tz-pause]");
    var onlyActive = $("[data-tz-log-active]");

    function stamp(d) {
      function p(n, w) { return String(n).padStart(w || 2, "0"); }
      return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "." + p(d.getMilliseconds(), 3);
    }
    var KIND = { rx: "keldi", tx: "yuborildi", manual: "qo'lda", note: "izoh" };

    function visible(entry) {
      return !onlyActive.checked || !entry.session || entry.session === active;
    }

    function logRow(entry) {
      var row = document.createElement("div");
      row.className = "tz-logrow tz-logrow--" + entry.kind;
      var t = document.createElement("span");
      t.className = "tz-t";
      t.textContent = stamp(entry.t) + " " + (entry.session ? entry.session.label + " " : "") + KIND[entry.kind];
      row.appendChild(t);
      if (entry.bytes) {
        var h = document.createElement("span");
        h.className = "tz-hex";
        h.textContent = toHex(entry.bytes);
        var v = document.createElement("span");
        v.className = "tz-vis";
        v.textContent = toVisible(entry.bytes);
        row.appendChild(h);
        row.appendChild(v);
      } else {
        var n = document.createElement("span");
        n.className = "tz-note";
        n.textContent = entry.text;
        row.appendChild(n);
      }
      return row;
    }

    function addLog(s, kind, bytes, text) {
      var entry = { t: new Date(), session: s, kind: kind, bytes: bytes, text: text };
      log.push(entry);
      if (log.length > LOG_MAX * 8) log.splice(0, log.length - LOG_MAX * 8);
      if (paused.checked || !visible(entry)) return;
      var empty = logEl.querySelector(".tz-empty");
      if (empty) empty.remove();
      var nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
      logEl.appendChild(logRow(entry));
      while (logEl.children.length > LOG_MAX) logEl.removeChild(logEl.firstChild);
      if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
    }

    function renderLog() {
      logEl.innerHTML = "";
      var rows = log.filter(visible).slice(-LOG_MAX);
      if (!rows.length) {
        logEl.innerHTML = '<div class="tz-empty">Hali hech narsa kelmadi.</div>';
        return;
      }
      rows.forEach(function (e) { logEl.appendChild(logRow(e)); });
      logEl.scrollTop = logEl.scrollHeight;
    }
    onlyActive.addEventListener("change", renderLog);
    paused.addEventListener("change", function () { if (!paused.checked) renderLog(); });

    function logText() {
      return log.map(function (e) {
        var head = stamp(e.t) + "  " + (e.session ? e.session.label + "  " : "") + KIND[e.kind];
        return e.bytes ? head + "  " + toHex(e.bytes) + "  |  " + toVisible(e.bytes) : head + "  " + e.text;
      }).join("\n");
    }

    $("[data-tz-clear]").addEventListener("click", function () {
      log = [];
      sessions.forEach(function (s) {
        s.buf = []; s.bytes = 0; s.frames = 0; s.bad = 0;
        s.last = null; s.lastRes = null; s.lastFrame = null;
        s.stability = new Stability(need());
      });
      renderLog();
      renderAll();
    });
    $("[data-tz-copy]").addEventListener("click", function () {
      var btn = this;
      navigator.clipboard.writeText(logText()).then(function () {
        btn.textContent = "Nusxa olindi ✓";
        setTimeout(function () { btn.textContent = "Nusxa olish"; }, 1500);
      }, function (e) { addLog(null, "note", null, "Nusxa olib bo'lmadi: " + e.message); });
    });
    $("[data-tz-download]").addEventListener("click", function () {
      var blob = new Blob([logText() + "\n"], { type: "text/plain" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "tarozi-" + new Date().toISOString().replace(/[:.]/g, "-") + ".txt";
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    });

    // ---- Web Serial ----------------------------------------------------------
    function serviceId(text) {
      var s = text.trim().toLowerCase();
      if (/^(0x)?[0-9a-f]{4}$/.test(s)) return parseInt(s.replace(/^0x/, ""), 16);
      return s;
    }

    /* The browser's picker shows the COM name ("USB-SERIAL CH340 (COM3)"); the page
       never sees it again. Adding ports one at a time and naming each row is how
       the tarozichi keeps track of which is which. */
    async function addPort() {
      if (!("serial" in navigator)) { setStatus("Web Serial yo'q — Chrome yoki Edge kerak.", "bad"); return; }
      var reqOpts = {};
      var svc = setEls.btService.value.trim();
      if (svc) {
        var id = serviceId(svc);
        reqOpts.allowedBluetoothServiceClassIds = [id];
        reqOpts.filters = [{ bluetoothServiceClassId: id }];
      }
      var port;
      try {
        port = await navigator.serial.requestPort(reqOpts);
      } catch (e) {
        setStatus(e.name === "NotFoundError" ? "Port tanlanmadi" : "Xato: " + e.message, "warn");
        return;
      }
      var s = sessionForPort(port) || newSession("serial", { port: port, info: describePort(port) });
      setActive(s);
      connectSession(s);
    }
    $("[data-tz-add-port]").addEventListener("click", addPort);

    $("[data-tz-connect-all]").addEventListener("click", function () {
      sessions.forEach(function (s) {
        if (s.kind === "serial" && !s.connected) connectSession(s);
      });
    });

    async function connectSerial(s) {
      var o = { baudRate: +opt("baud"), dataBits: +opt("dataBits"), stopBits: +opt("stopBits"),
                parity: opt("parity"), flowControl: opt("flowControl") };
      try {
        await s.port.open(o);
      } catch (e) {
        var msg = s.label + ": " + (e.name === "InvalidStateError"
          ? "port allaqachon ochiq."
          : "portni ochib bo'lmadi. Boshqa dastur (Hercules?) band qilgan bo'lishi mumkin — uni yoping. (" + e.message + ")");
        setStatus(msg, "bad");
        addLog(s, "note", null, msg);
        return;
      }
      s.settings = o.baudRate + " " + o.dataBits + o.parity[0].toUpperCase() + o.stopBits;
      s.connected = true;
      s.buf = [];
      setStatus("Ulandi: " + s.label + " · " + s.info + " · " + s.settings, "ok");
      addLog(s, "note", null, "Ulandi: " + s.info + ", " + JSON.stringify(o));
      renderAll();
      s.keepReading = true;
      s.closed = readLoop(s);
    }

    async function readLoop(s) {
      var port = s.port;
      while (port.readable && s.keepReading) {
        var reader = port.readable.getReader();
        s.reader = reader;
        try {
          for (;;) {
            var r = await reader.read();
            if (r.done) break;
            if (r.value && r.value.length) onBytes(s, r.value);
          }
        } catch (e) {
          // Framing/parity/break errors are per-byte and the loop carries on;
          // NetworkError means the device itself went away.
          addLog(s, "note", null, "O'qish xatosi: " + e.name + " — " + e.message
            + (e.name === "FramingError" || e.name === "ParityError" ? " (baud rate yoki parity noto'g'ri bo'lishi mumkin)" : ""));
          if (e.name === "NetworkError") s.keepReading = false;
        } finally {
          reader.releaseLock();
          s.reader = null;
        }
      }
      try { await port.close(); } catch (e) { /* already gone */ }
      s.connected = false;
      s.closed = null;
      addLog(s, "note", null, "Port yopildi.");
      renderAll();
    }

    if ("serial" in navigator) {
      // Ports this site was allowed before come back without the picker.
      navigator.serial.getPorts().then(function (ports) {
        ports.forEach(function (port) {
          if (!sessionForPort(port)) newSession("serial", { port: port, info: describePort(port) });
        });
        renderAll();
      });
      // A known port plugged back in (the hub, a cable) reappears in the list.
      navigator.serial.addEventListener("connect", function (e) {
        if (!sessionForPort(e.target)) newSession("serial", { port: e.target, info: describePort(e.target) });
        addLog(sessionForPort(e.target), "note", null, "Qurilma ulandi (USB).");
        renderAll();
      });
      navigator.serial.addEventListener("disconnect", function (e) {
        var s = sessionForPort(e.target);
        if (s) addLog(s, "note", null, "Qurilma uzildi (kabel / hub / Bluetooth).");
      });
    }

    async function sendCommand() {
      var s = active;
      if (!s || s.kind !== "serial" || !s.connected || !s.port.writable) {
        setStatus("Avval tanlangan portni ulang.", "warn");
        return;
      }
      var text = $("[data-tz-send-text]").value;
      var bytes;
      try {
        bytes = $("[data-tz-send-mode]").value === "hex" ? parseHex(text) : parseEscaped(text);
      } catch (e) { setStatus(e.message, "warn"); return; }
      var end = parseEscaped($("[data-tz-send-end]").value);
      var all = new Uint8Array(bytes.length + end.length);
      all.set(bytes); all.set(end, bytes.length);
      var writer = s.port.writable.getWriter();
      try {
        await writer.write(all);
        addLog(s, "tx", all);
      } catch (e) {
        addLog(s, "note", null, "Yuborib bo'lmadi: " + e.message);
      } finally {
        writer.releaseLock();
      }
    }
    $("[data-tz-send-go]").addEventListener("click", sendCommand);

    // ---- Web Bluetooth (BLE) ---------------------------------------------------
    function props(c) {
      return ["read", "write", "writeWithoutResponse", "notify", "indicate"].filter(function (k) {
        return c.properties[k];
      }).join(",");
    }

    async function addBle() {
      if (!("bluetooth" in navigator)) { setStatus("Web Bluetooth yo'q — Chrome yoki Edge kerak.", "bad"); return; }
      var device;
      try {
        device = await navigator.bluetooth.requestDevice({
          acceptAllDevices: true, optionalServices: [serviceId(setEls.bleService.value)] });
      } catch (e) {
        setStatus(e.name === "NotFoundError" ? "Qurilma tanlanmadi" : "Xato: " + e.message, "warn");
        return;
      }
      var s = sessions.filter(function (x) { return x.device === device; })[0]
        || newSession("ble", { device: device, info: device.name || device.id });
      if (!s.listening) {
        s.listening = true;
        device.addEventListener("gattserverdisconnected", function () {
          addLog(s, "note", null, "BLE qurilma uzildi.");
          s.connected = false; s.ch = null;
          renderAll();
        });
      }
      setActive(s);
      connectSession(s);
    }
    $("[data-tz-add-ble]").addEventListener("click", addBle);

    async function connectBle(s) {
      var svc = serviceId(setEls.bleService.value);
      var chr = setEls.bleChar.value.trim();
      try {
        setStatus("Ulanmoqda: " + s.label + "…");
        var server = await s.device.gatt.connect();
        var service = await server.getPrimaryService(svc);
        var chars = await service.getCharacteristics();
        addLog(s, "note", null, "Characteristics: " + chars.map(function (c) {
          return c.uuid + " [" + props(c) + "]";
        }).join("; "));
        var ch = chr ? await service.getCharacteristic(serviceId(chr))
          : chars.filter(function (c) { return c.properties.notify || c.properties.indicate; })[0];
        if (!ch) throw new Error("Notify characteristic topilmadi — UUID ni tarozi ustasidan so'rang.");
        ch.addEventListener("characteristicvaluechanged", function (e) {
          var dv = e.target.value;
          onBytes(s, new Uint8Array(dv.buffer.slice(dv.byteOffset, dv.byteOffset + dv.byteLength)));
        });
        await ch.startNotifications();
        s.ch = ch;
        s.connected = true;
        s.settings = ch.uuid;
        setStatus("Ulandi (BLE): " + s.label + " · " + ch.uuid, "ok");
      } catch (e) {
        var msg = s.label + ": BLE xato: " + e.message
          + (e.name === "NotFoundError" ? " (service UUID noto'g'ri bo'lishi mumkin)" : "");
        setStatus(msg, "bad");
        addLog(s, "note", null, msg);
        if (s.device.gatt.connected) s.device.gatt.disconnect();
      }
      renderAll();
    }

    // ---- connect / disconnect / forget, whatever the session is ---------------------
    function connectSession(s) {
      if (s.connected) return;
      if (s.kind === "serial") connectSerial(s);
      else if (s.kind === "ble") connectBle(s);
    }

    async function disconnectSession(s) {
      s.keepReading = false;
      if (s.reader) { try { await s.reader.cancel(); } catch (e) { /* closing anyway */ } }
      if (s.closed) { await s.closed; }
      if (s.ch) { try { await s.ch.stopNotifications(); } catch (e) { /* gone */ } s.ch = null; }
      if (s.device && s.device.gatt.connected) s.device.gatt.disconnect();
      s.connected = false;
      renderAll();
    }

    async function forgetSession(s) {
      await disconnectSession(s);
      // Forgetting drops the browser's permission too, so the port needs the picker
      // again — which is what "remove it from the list" should mean.
      if (s.port && s.port.forget) { try { await s.port.forget(); } catch (e) { /* older Chrome */ } }
      if (s.device && s.device.forget) { try { await s.device.forget(); } catch (e) { /* older Chrome */ } }
      removeSession(s);
    }

    window.addEventListener("beforeunload", function () {
      sessions.forEach(function (s) { if (s.connected) disconnectSession(s); });
    });

    // ---- manual paste and keyboard-mode receivers ----------------------------------
    function manualSession() {
      return sessions.filter(function (s) { return s.kind === "manual"; })[0]
        || newSession("manual", { info: "matn / hex / klaviatura" });
    }

    $("[data-tz-paste-go]").addEventListener("click", function () {
      var text = $("[data-tz-paste]").value;
      if (!text.trim()) return;
      var bytes;
      try {
        bytes = $("[data-tz-paste-mode]").value === "hex" ? parseHex(text) : parseEscaped(text);
      } catch (e) { setStatus(e.message, "warn"); return; }
      var s = manualSession();
      setActive(s);
      onBytes(s, bytes, "manual");
      flush(s);
    });

    /* A keyboard-mode receiver types the weight and usually ends with Enter (some
       send Tab, some nothing) — so Enter/Tab or a short pause ends the reading. */
    var wedge = $("[data-tz-wedge]");
    var wedgeTimer = null;
    function wedgeTake() {
      clearTimeout(wedgeTimer);
      var v = wedge.value;
      if (!v) return;
      wedge.value = "";
      var s = manualSession();
      setActive(s);
      onBytes(s, parseEscaped(v + "\r"), "manual");
    }
    wedge.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); wedgeTake(); }
    });
    wedge.addEventListener("input", function () {
      clearTimeout(wedgeTimer);
      wedgeTimer = setTimeout(wedgeTake, 800);
    });

    // ---- what a kg field will do: take the weight only when it has settled ----------
    $("[data-tz-take]").addEventListener("click", function () {
      var demo = $("[data-tz-demo]");
      var s = active;
      if (s && s.last && s.last.ok && s.stability.stable()) {
        demo.value = fmtKg(s.last.kg);
        setStatus("Olindi (" + s.label + "): " + fmtKg(s.last.kg) + " kg", "ok");
      } else {
        demo.value = "";
        setStatus(s && s.last ? "Vazn hali barqaror emas — kuting." : "Tanlangan tarozidan hali vazn kelmadi.", "warn");
      }
    });

    renderListEmpty();
    renderActive();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var el = document.querySelector("[data-tarozi]");
    if (el) init(el);
  });
})(typeof window !== "undefined" ? window : globalThis);
