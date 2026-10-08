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

  var VENDORS = { 0x1a86: "CH340 (QinHeng)", 0x10c4: "CP210x (Silicon Labs)",
                  0x0403: "FTDI", 0x067b: "PL2303 (Prolific)", 0x2341: "Arduino" };
  var STORE_KEY = "tarozi-test-v1";
  var LOG_MAX = 500;

  function init(el) {
    var $ = function (sel) { return el.querySelector(sel); };
    var $$ = function (sel) { return Array.prototype.slice.call(el.querySelectorAll(sel)); };

    var state = { port: null, reader: null, keepReading: false, closed: null,
                  device: null, ch: null, buf: [], bytes: 0, frames: 0, bad: 0,
                  last: null, log: [], connected: false };
    var stability = new Stability(5);

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
    stability.need = Math.max(1, +opt("stableCount") || 5);
    Object.keys(setEls).forEach(function (k) {
      setEls[k].addEventListener("change", function () {
        saveSettings();
        // A new way of reading the stream starts the reading over.
        state.buf = [];
        stability = new Stability(Math.max(1, +opt("stableCount") || 5));
        renderResult(null);
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

    // ---- status / result ----------------------------------------------------
    var statusEl = $("[data-tz-status]");
    function setStatus(text, tone) {
      statusEl.textContent = text;
      statusEl.className = "tz-status" + (tone ? " tz-status--" + tone : "");
    }
    function setConnected(on) {
      state.connected = on;
      $$("[data-tz-disconnect]").forEach(function (b) { b.hidden = !on; });
      $$("[data-tz-connect]").forEach(function (b) { b.disabled = on; });
      $("[data-tz-send]").hidden = !(on && state.port);
      if (!on) setStatus("Ulanmagan");
    }

    function renderResult(res) {
      $("[data-tz-frames]").textContent = state.frames;
      $("[data-tz-bad]").textContent = state.bad;
      $("[data-tz-bytes]").textContent = state.bytes;
      if (!res) {
        $("[data-tz-weight]").textContent = "—";
        $("[data-tz-stable]").textContent = "—";
        $("[data-tz-stable]").className = "tz-pill";
        $("[data-tz-repeat]").textContent = "0";
        return;
      }
      $("[data-tz-format]").textContent = res.format || "—";
      $("[data-tz-flag]").textContent = res.flag ? res.flag + (res.kind ? " · " + res.kind : "") : "yo'q";
      if (!res.ok) return;
      $("[data-tz-weight]").textContent = fmtKg(res.kg);
      $("[data-tz-unit]").textContent = res.unit && res.unit !== "kg" ? "kg (keldi: " + res.unit + ")" : "kg";
      $("[data-tz-repeat]").textContent = stability.repeat;
      var pill = $("[data-tz-stable]");
      var stable = stability.stable();
      pill.textContent = stable ? "Barqaror" : "O'zgaryapti";
      pill.className = "tz-pill " + (stable ? "tz-pill--ok" : "tz-pill--wait");
    }

    // ---- the pipeline: bytes → frames → weight -------------------------------
    function handleFrames(frames) {
      var o = opts();
      frames.forEach(function (frame) {
        state.frames++;
        $("[data-tz-last]").textContent = toVisible(frame);
        var res = decodeFrame(frame, o);
        if (res.ok) {
          stability.push(res.kg, res.flag);
          state.last = res;
        } else {
          state.bad++;
          if (res.error) addLog("note", null, "Kadr o'qilmadi: " + res.error);
        }
        renderResult(res);
      });
    }

    function onBytes(bytes, kind) {
      state.bytes += bytes.length;
      addLog(kind || "rx", bytes);
      var o = opts();
      if (o.framing === "chunk") { handleFrames([bytes]); return; }
      for (var i = 0; i < bytes.length; i++) state.buf.push(bytes[i]);
      var r = splitFrames(state.buf, o);
      state.buf = r.rest;
      handleFrames(r.frames);
    }

    /* Pasted text has no "next frame" to end the last one — read what is left. */
    function flush() {
      if (!state.buf.length) return;
      var frame = new Uint8Array(state.buf);
      state.buf = [];
      handleFrames([frame]);
    }

    // ---- log ----------------------------------------------------------------
    var logEl = $("[data-tz-log]");
    var paused = $("[data-tz-pause]");

    function stamp(d) {
      function p(n, w) { return String(n).padStart(w || 2, "0"); }
      return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "." + p(d.getMilliseconds(), 3);
    }
    var KIND = { rx: "keldi", tx: "yuborildi", manual: "qo'lda", note: "izoh" };

    function addLog(kind, bytes, text) {
      var entry = { t: new Date(), kind: kind, bytes: bytes, text: text };
      state.log.push(entry);
      if (state.log.length > LOG_MAX * 4) state.log.splice(0, state.log.length - LOG_MAX * 4);
      if (paused.checked) return;
      var empty = logEl.querySelector(".tz-empty");
      if (empty) empty.remove();
      var nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
      var row = document.createElement("div");
      row.className = "tz-logrow tz-logrow--" + kind;
      var t = document.createElement("span");
      t.className = "tz-t";
      t.textContent = stamp(entry.t) + " " + KIND[kind];
      row.appendChild(t);
      if (bytes) {
        var h = document.createElement("span");
        h.className = "tz-hex";
        h.textContent = toHex(bytes);
        var v = document.createElement("span");
        v.className = "tz-vis";
        v.textContent = toVisible(bytes);
        row.appendChild(h);
        row.appendChild(v);
      } else {
        var n = document.createElement("span");
        n.className = "tz-note";
        n.textContent = text;
        row.appendChild(n);
      }
      logEl.appendChild(row);
      while (logEl.children.length > LOG_MAX) logEl.removeChild(logEl.firstChild);
      if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
    }

    function logText() {
      return state.log.map(function (e) {
        var head = stamp(e.t) + "  " + KIND[e.kind];
        return e.bytes ? head + "  " + toHex(e.bytes) + "  |  " + toVisible(e.bytes) : head + "  " + e.text;
      }).join("\n");
    }

    $("[data-tz-clear]").addEventListener("click", function () {
      state.log = []; state.buf = []; state.bytes = 0; state.frames = 0; state.bad = 0;
      stability = new Stability(Math.max(1, +opt("stableCount") || 5));
      logEl.innerHTML = '<div class="tz-empty">Hali hech narsa kelmadi.</div>';
      $("[data-tz-last]").textContent = "—";
      $("[data-tz-format]").textContent = "—";
      $("[data-tz-flag]").textContent = "—";
      renderResult(null);
    });
    $("[data-tz-copy]").addEventListener("click", function () {
      var btn = this;
      navigator.clipboard.writeText(logText()).then(function () {
        btn.textContent = "Nusxa olindi ✓";
        setTimeout(function () { btn.textContent = "Nusxa olish"; }, 1500);
      }, function (e) { addLog("note", null, "Nusxa olib bo'lmadi: " + e.message); });
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

    async function connectSerial() {
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
      var openOpts = { baudRate: +opt("baud"), dataBits: +opt("dataBits"), stopBits: +opt("stopBits"),
                       parity: opt("parity"), flowControl: opt("flowControl") };
      try {
        await port.open(openOpts);
      } catch (e) {
        var msg = e.name === "InvalidStateError"
          ? "Port allaqachon ochiq — sahifani yangilang."
          : "Portni ochib bo'lmadi. Boshqa dastur (Hercules?) band qilgan bo'lishi mumkin — uni yoping. (" + e.message + ")";
        setStatus(msg, "bad");
        addLog("note", null, msg);
        return;
      }
      state.port = port;
      var info = port.getInfo();
      var what = info.bluetoothServiceClassId ? "Bluetooth (" + info.bluetoothServiceClassId + ")"
        : info.usbVendorId ? (VENDORS[info.usbVendorId] || "USB " + hex2(info.usbVendorId >> 8) + hex2(info.usbVendorId & 0xff))
          + " · VID " + info.usbVendorId.toString(16) + " PID " + (info.usbProductId || 0).toString(16)
        : "port";
      setConnected(true);
      setStatus("Ulandi: " + what + " · " + openOpts.baudRate + " " + openOpts.dataBits
        + openOpts.parity[0].toUpperCase() + openOpts.stopBits, "ok");
      addLog("note", null, "Ulandi: " + what + ", " + JSON.stringify(openOpts));
      state.keepReading = true;
      state.closed = readLoop(port);
    }

    async function readLoop(port) {
      while (port.readable && state.keepReading) {
        var reader = port.readable.getReader();
        state.reader = reader;
        try {
          for (;;) {
            var r = await reader.read();
            if (r.done) break;
            if (r.value && r.value.length) onBytes(r.value);
          }
        } catch (e) {
          // Framing/parity/break errors are per-byte and the loop carries on;
          // NetworkError means the device itself went away.
          addLog("note", null, "O'qish xatosi: " + e.name + " — " + e.message
            + (e.name === "FramingError" || e.name === "ParityError" ? " (baud rate yoki parity noto'g'ri bo'lishi mumkin)" : ""));
          if (e.name === "NetworkError") state.keepReading = false;
        } finally {
          reader.releaseLock();
          state.reader = null;
        }
      }
      try { await port.close(); } catch (e) { /* already gone */ }
      if (state.port === port) {
        state.port = null;
        setConnected(false);
        addLog("note", null, "Port yopildi.");
      }
    }

    if ("serial" in navigator) {
      navigator.serial.addEventListener("disconnect", function (e) {
        if (e.target === state.port) addLog("note", null, "Qurilma uzildi (kabel / qabul qilgich / Bluetooth).");
      });
    }

    async function sendCommand() {
      if (!state.port || !state.port.writable) return;
      var text = $("[data-tz-send-text]").value;
      var bytes;
      try {
        bytes = $("[data-tz-send-mode]").value === "hex" ? parseHex(text) : parseEscaped(text);
      } catch (e) { setStatus(e.message, "warn"); return; }
      var end = parseEscaped($("[data-tz-send-end]").value);
      var all = new Uint8Array(bytes.length + end.length);
      all.set(bytes); all.set(end, bytes.length);
      var writer = state.port.writable.getWriter();
      try {
        await writer.write(all);
        addLog("tx", all);
      } catch (e) {
        addLog("note", null, "Yuborib bo'lmadi: " + e.message);
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

    async function connectBle() {
      var svc = serviceId(setEls.bleService.value);
      var chr = setEls.bleChar.value.trim();
      var device;
      try {
        device = await navigator.bluetooth.requestDevice({ acceptAllDevices: true, optionalServices: [svc] });
      } catch (e) {
        setStatus(e.name === "NotFoundError" ? "Qurilma tanlanmadi" : "Xato: " + e.message, "warn");
        return;
      }
      state.device = device;
      device.addEventListener("gattserverdisconnected", function () {
        addLog("note", null, "BLE qurilma uzildi.");
        state.device = null; state.ch = null;
        setConnected(false);
      });
      try {
        setStatus("Ulanmoqda: " + (device.name || device.id) + "…");
        var server = await device.gatt.connect();
        var service = await server.getPrimaryService(svc);
        var chars = await service.getCharacteristics();
        addLog("note", null, "Characteristics: " + chars.map(function (c) {
          return c.uuid + " [" + props(c) + "]";
        }).join("; "));
        var ch = chr ? await service.getCharacteristic(serviceId(chr))
          : chars.filter(function (c) { return c.properties.notify || c.properties.indicate; })[0];
        if (!ch) throw new Error("Notify characteristic topilmadi — UUID ni tarozi ustasidan so'rang.");
        ch.addEventListener("characteristicvaluechanged", function (e) {
          var dv = e.target.value;
          onBytes(new Uint8Array(dv.buffer.slice(dv.byteOffset, dv.byteOffset + dv.byteLength)));
        });
        await ch.startNotifications();
        state.ch = ch;
        setConnected(true);
        setStatus("Ulandi (BLE): " + (device.name || device.id) + " · " + ch.uuid, "ok");
      } catch (e) {
        var msg = "BLE xato: " + e.message
          + (e.name === "NotFoundError" ? " (service UUID noto'g'ri bo'lishi mumkin)" : "");
        setStatus(msg, "bad");
        addLog("note", null, msg);
        if (device.gatt.connected) device.gatt.disconnect();
      }
    }

    // ---- connect / disconnect buttons -------------------------------------------
    $$("[data-tz-connect]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.dataset.tzConnect === "serial") {
          if (!("serial" in navigator)) { setStatus("Web Serial yo'q — Chrome yoki Edge kerak.", "bad"); return; }
          connectSerial();
        } else {
          if (!("bluetooth" in navigator)) { setStatus("Web Bluetooth yo'q — Chrome yoki Edge kerak.", "bad"); return; }
          connectBle();
        }
      });
    });

    async function disconnect() {
      state.keepReading = false;
      if (state.reader) { try { await state.reader.cancel(); } catch (e) { /* closing anyway */ } }
      if (state.closed) { await state.closed; state.closed = null; }
      if (state.ch) { try { await state.ch.stopNotifications(); } catch (e) { /* gone */ } state.ch = null; }
      if (state.device && state.device.gatt.connected) state.device.gatt.disconnect();
      state.device = null;
      setConnected(false);
    }
    $$("[data-tz-disconnect]").forEach(function (b) { b.addEventListener("click", disconnect); });
    window.addEventListener("beforeunload", function () { if (state.connected) disconnect(); });

    // ---- manual paste and keyboard-mode receivers ----------------------------------
    $("[data-tz-paste-go]").addEventListener("click", function () {
      var text = $("[data-tz-paste]").value;
      if (!text.trim()) return;
      var bytes;
      try {
        bytes = $("[data-tz-paste-mode]").value === "hex" ? parseHex(text) : parseEscaped(text);
      } catch (e) { setStatus(e.message, "warn"); return; }
      onBytes(bytes, "manual");
      flush();
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
      onBytes(parseEscaped(v + "\r"), "manual");
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
      if (state.last && state.last.ok && stability.stable()) {
        demo.value = fmtKg(state.last.kg);
        setStatus("Olindi: " + fmtKg(state.last.kg) + " kg", "ok");
      } else {
        demo.value = "";
        setStatus(state.last ? "Vazn hali barqaror emas — kuting." : "Tarozidan hali vazn kelmadi.", "warn");
      }
    });

    renderResult(null);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var el = document.querySelector("[data-tarozi]");
    if (el) init(el);
  });
})(typeof window !== "undefined" ? window : globalThis);
