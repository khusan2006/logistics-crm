/* Joylashuv: the map picker on the mijoz form, the map on the joylashuv modal, and
 * the Ulashish buttons beside it.
 *
 * Leaflet is only fetched once a page actually has a map on it — most pages do
 * not, and it is the heaviest file the app would otherwise load. Everything is
 * wired on DOMContentLoaded and again on modal:loaded, because a <script> inside
 * modal HTML never runs.
 *
 * The picker only ever writes "lat, lng" into the form's one text box. The server
 * reads that box on save, so this file is a convenience: with it switched off the
 * operator can still type or paste a location and it saves.
 */
(function () {
  'use strict';

  var self = document.currentScript;
  var LEAFLET_JS = self.getAttribute('data-leaflet-js');
  var LEAFLET_CSS = self.getAttribute('data-leaflet-css');
  var TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  var ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
  // Nothing chosen yet: open on Toshkent rather than on the whole planet.
  var HOME = [41.3111, 69.2797];
  var HOME_ZOOM = 12;
  var PIN_ZOOM = 16;
  var NOMINATIM = 'https://nominatim.openstreetmap.org/search';
  var NOMINATIM_REVERSE = 'https://nominatim.openstreetmap.org/reverse';
  // Biases the search toward Uzbekistan without walling off a mijoz next door.
  var UZ_VIEWBOX = '55.9,45.6,73.2,37.1';
  // OpenStreetMap refuses tiles and searches that arrive with no Referer (the
  // yellow "Access blocked" squares), and Django's same-origin policy strips it
  // from every cross-site request. This sends the site's origin only — never the
  // page path, so which mijoz is open does not leave the app.
  var REFERRER = 'strict-origin-when-cross-origin';

  var leafletLoading = null;
  function loadLeaflet() {
    if (window.L) { return Promise.resolve(window.L); }
    if (leafletLoading) { return leafletLoading; }
    leafletLoading = new Promise(function (resolve, reject) {
      var css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = LEAFLET_CSS;
      document.head.appendChild(css);
      var js = document.createElement('script');
      js.src = LEAFLET_JS;
      js.onload = function () { resolve(window.L); };
      js.onerror = function () { leafletLoading = null; reject(new Error('leaflet')); };
      document.head.appendChild(js);
    });
    return leafletLoading;
  }

  function newMap(el, center, zoom, zoomAt) {
    var map = L.map(el, { center: center, zoom: zoom, scrollWheelZoom: true, zoomControl: false });
    // The picker keeps its top edge for the search bar, so its zoom goes low.
    L.control.zoom({ position: zoomAt || 'topleft' }).addTo(map);
    L.tileLayer(TILES, {
      maxZoom: 19, attribution: ATTRIBUTION, referrerPolicy: REFERRER
    }).addTo(map);
    // Drawn while its box may still be settling (a modal opening, a panel just
    // unhidden) — measured again once the browser has laid it out.
    requestAnimationFrame(function () { map.invalidateSize(); });
    setTimeout(function () { map.invalidateSize(); }, 250);
    return map;
  }

  // 41.311081 → "41.311081", 41.3 → "41.3": no trailing zeros, as everywhere else.
  function fmt(n) { return Number(n).toFixed(6).replace(/\.?0+$/, ''); }

  var PLAIN = /^\s*-?\d{1,3}(\.\d+)?\s*[,;\s]\s*-?\d{1,3}(\.\d+)?\s*$/;
  function plainPoint(text) {
    if (!PLAIN.test(text)) { return null; }
    var parts = text.trim().split(/\s*[,;\s]\s*/);
    var lat = parseFloat(parts[0]), lng = parseFloat(parts[1]);
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) { return null; }
    return [lat, lng];
  }

  function say(el, text, tone) {
    if (!el) { return; }
    el.textContent = text || '';
    el.className = 'geo-status' + (tone ? ' is-' + tone : '');
  }

  /* ---------- the picker on the mijoz form ---------- */

  // "Chorsu ko'chasi, Shayxontohur tumani, Toshkent, 100011, O'zbekiston" →
  // "Chorsu ko'chasi, Shayxontohur tumani, Toshkent": the three nearest names say
  // where it is; the postcode and the country say nothing the operator needs.
  function shortName(full) {
    return full.split(', ').filter(function (p) { return !/^\d{4,}$/.test(p); })
      .slice(0, 3).join(', ');
  }

  function busy(buttons, on) {
    buttons.forEach(function (b) { b.disabled = on; b.classList.toggle('is-busy', on); });
  }

  function initPicker(root) {
    if (root.__geo) { return; }
    root.__geo = true;

    function $(sel) { return root.querySelector(sel); }
    var input = $('.geo-input input');
    var status = $('[data-geo-status]');
    var actions = $('[data-geo-actions]');
    var panel = $('[data-geo-panel]');
    var mapEl = $('[data-geo-map]');
    var q = $('[data-geo-q]');
    var results = $('[data-geo-results]');
    var place = $('[data-geo-place]');
    var placeText = $('[data-geo-place-text]');
    var source = $('[data-geo-source]');
    var hint = $('[data-geo-hint]');
    var clearBtn = $('[data-geo-clear]');
    var pasteBtn = $('[data-geo-paste]');
    var hereBtns = Array.prototype.slice.call(root.querySelectorAll('[data-geo-here]'));
    var parseUrl = root.getAttribute('data-parse-url');
    var map = null, mapLoading = null, marker = null, point = null;
    var asked = 0, named = 0, timer = null;

    // Every visible bit follows from three facts: is there text, is there a point,
    // is the map open. Called after anything changes one of them.
    function sync() {
      actions.hidden = !panel.hidden;
      clearBtn.hidden = !(input.value.trim() || !panel.hidden);
      pasteBtn.hidden = !!input.value.trim() || !canPaste;
      place.hidden = !point;
      hint.textContent = point
        ? 'Aniqroq bo‘lishi uchun belgini suring.'
        : 'Xaritani bosing — belgi shu yerga qo‘yiladi.';
    }

    function openMap() {
      panel.hidden = false;
      // A mijoz with no pin yet is looked up by their Manzil; one who already has
      // a pin does not need their own address typed back at them.
      if (!q.value && !point) {
        var form = root.closest('form');
        var address = form && form.querySelector('[name="address"]');
        if (address && address.value) { q.value = address.value; }
      }
      sync();
      if (map) { map.invalidateSize(); return Promise.resolve(); }
      if (mapLoading) { return mapLoading; }
      mapLoading = loadLeaflet().then(function () {
        map = newMap(mapEl, point || HOME, point ? PIN_ZOOM : HOME_ZOOM, 'bottomright');
        map.on('click', function (e) {
          results.hidden = true;
          choose(e.latlng.lat, e.latlng.lng, 'Xaritadan');
        });
      }).catch(function () {
        mapLoading = null;
        say(status, "Xaritani yuklab bo‘lmadi. Internetni tekshiring yoki havola qo‘ying.", 'error');
      });
      return mapLoading;
    }

    // The place's name under the map, so a pin dropped one street off is caught
    // before it is saved. Coordinates stand in until (or unless) the name arrives.
    function nameIt(lat, lng) {
      var ticket = ++named;
      placeText.textContent = fmt(lat) + ', ' + fmt(lng);
      fetch(NOMINATIM_REVERSE + '?format=jsonv2&zoom=18&accept-language=uz,ru&lat=' + lat +
        '&lon=' + lng, { referrerPolicy: REFERRER })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (ticket === named && d && d.display_name) { placeText.textContent = shortName(d.display_name); }
        }).catch(function () {});
    }

    function show(lat, lng, from) {
      point = [lat, lng];
      source.textContent = from || '';
      nameIt(lat, lng);
      return openMap().then(function () {
        if (!map) { return; }
        if (marker) { marker.setLatLng(point); }
        else {
          marker = L.marker(point, { draggable: true, autoPan: true }).addTo(map);
          marker.on('dragend', function () {
            var at = marker.getLatLng();
            choose(at.lat, at.lng, 'Xaritadan');
          });
        }
        map.setView(point, Math.max(map.getZoom(), PIN_ZOOM));
        sync();
      });
    }

    // A point picked here (map, GPS, search) — written into the box as coordinates.
    function choose(lat, lng, from) {
      input.value = fmt(lat) + ', ' + fmt(lng);
      say(status, '');
      show(lat, lng, from);
    }

    function forget() {
      point = null;
      named++;
      if (marker) { marker.remove(); marker = null; }
      sync();
    }

    $('[data-geo-open]').addEventListener('click', openMap);

    clearBtn.addEventListener('click', function () {
      input.value = '';
      forget();
      panel.hidden = true;
      results.hidden = true;
      say(status, '');
      sync();
    });

    hereBtns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (!navigator.geolocation) {
          say(status, 'Bu brauzer joylashuvni aniqlay olmaydi.', 'error');
          return;
        }
        busy(hereBtns, true);
        say(status, 'Joylashuv aniqlanmoqda…');
        navigator.geolocation.getCurrentPosition(function (pos) {
          busy(hereBtns, false);
          var c = pos.coords;
          choose(c.latitude, c.longitude, 'GPS ±' + Math.round(c.accuracy) + ' m');
        }, function (err) {
          busy(hereBtns, false);
          say(status, err.code === 1
            ? 'Joylashuvga ruxsat berilmadi. Brauzer sozlamalarida ruxsat bering yoki xaritadan tanlang.'
            : 'Joylashuvni aniqlab bo‘lmadi. Xaritadan tanlang.', 'error');
        }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 });
      });
    });

    // Typed or pasted: coordinates are read here, a link is sent to the server,
    // which knows every map's format. Errors wait for `change` so a half-typed
    // "41.3" is not shouted at.
    function read(loud) {
      var text = input.value.trim();
      clearTimeout(timer);
      sync();
      if (!text) { forget(); say(status, ''); return; }
      var local = plainPoint(text);
      if (local) { say(status, ''); show(local[0], local[1], ''); return; }
      var ticket = ++asked;
      say(status, 'Havola o‘qilmoqda…');
      fetch(parseUrl + '?q=' + encodeURIComponent(text), {
        headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin'
      }).then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
      }).then(function (res) {
        if (ticket !== asked) { return; }
        if (!res.ok) { say(status, loud ? res.data.error : '', loud ? 'error' : ''); return; }
        input.value = res.data.text;
        say(status, '');
        show(res.data.lat, res.data.lng, 'Havoladan');
      }).catch(function () {
        if (ticket === asked) {
          say(status, loud ? "Havolani tekshirib bo‘lmadi — saqlashda qayta o‘qiladi." : '', loud ? 'error' : '');
        }
      });
    }
    input.addEventListener('input', function () {
      sync();
      clearTimeout(timer);
      timer = setTimeout(function () { read(false); }, 500);
    });
    input.addEventListener('change', function () { read(true); });

    // Pasting by long-press is fiddly on a phone; one tap reads the clipboard.
    // Offered only where the browser has the call — and it may still refuse.
    var canPaste = !!(navigator.clipboard && navigator.clipboard.readText && window.isSecureContext);
    pasteBtn.addEventListener('click', function () {
      navigator.clipboard.readText().then(function (text) {
        if (!text) { return; }
        input.value = text.trim();
        read(true);
      }, function () {
        say(status, 'Brauzer nusxani o‘qishga ruxsat bermadi — maydonni bosib turib qo‘ying.', 'error');
      });
    });

    function search() {
      var text = q.value.trim();
      if (!text) { q.focus(); return; }
      results.hidden = false;
      results.textContent = 'Qidirilmoqda…';
      var url = NOMINATIM + '?format=jsonv2&limit=6&accept-language=uz,ru&viewbox=' + UZ_VIEWBOX +
        '&q=' + encodeURIComponent(text);
      fetch(url, { referrerPolicy: REFERRER }).then(function (r) { return r.json(); }).then(function (rows) {
        results.textContent = '';
        if (!rows.length) {
          results.textContent = 'Hech narsa topilmadi. Xaritani bosib o‘zingiz tanlang.';
          return;
        }
        rows.forEach(function (row) {
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'geo-result';
          var parts = row.display_name.split(', ');
          var head = document.createElement('strong');
          head.textContent = parts[0];
          var tail = document.createElement('span');
          tail.textContent = shortName(parts.slice(1).join(', '));
          b.appendChild(head);
          b.appendChild(tail);
          b.addEventListener('click', function () {
            results.hidden = true;
            choose(parseFloat(row.lat), parseFloat(row.lon), 'Qidiruvdan');
          });
          results.appendChild(b);
        });
      }).catch(function () {
        results.textContent = 'Qidiruv ishlamadi. Xaritani bosib o‘zingiz tanlang.';
      });
    }
    $('[data-geo-search]').addEventListener('click', search);
    q.addEventListener('keydown', function (e) {
      // Enter searches — it must not submit the mijoz form.
      if (e.key === 'Enter') { e.preventDefault(); search(); }
      if (e.key === 'Escape' && !results.hidden) { e.preventDefault(); e.stopPropagation(); results.hidden = true; }
    });

    sync();
    // A saved joylashuv opens with its map already showing: that IS the preview.
    var initial = plainPoint(input.value);
    if (initial) { show(initial[0], initial[1], ''); }
  }

  /* ---------- the read-only map on the joylashuv modal ---------- */

  function initView(el) {
    if (el.__geo) { return; }
    el.__geo = true;
    var at = [parseFloat(el.getAttribute('data-lat')), parseFloat(el.getAttribute('data-lng'))];
    loadLeaflet().then(function () {
      var map = newMap(el, at, PIN_ZOOM);
      L.marker(at).addTo(map);
    }).catch(function () { el.hidden = true; });
  }

  /* ---------- Ulashish ---------- */

  // The textarea goes inside `host`, not <body>: an open modal <dialog> makes
  // everything outside it inert, and an inert textarea cannot be selected.
  function copyByHand(text, host) {
    var t = document.createElement('textarea');
    t.value = text;
    t.setAttribute('readonly', '');
    t.style.position = 'fixed';
    t.style.opacity = '0';
    host.appendChild(t);
    t.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    t.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error('copy'));
  }

  function copyText(text, host) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return copyByHand(text, host); });
    }
    return copyByHand(text, host);
  }

  function initShare(root) {
    if (root.__geo) { return; }
    root.__geo = true;
    var title = root.getAttribute('data-share-title');
    var text = root.getAttribute('data-share-text');
    var status = root.querySelector('[data-share-status]');
    var native = root.querySelector('[data-share-native]');
    var url = root.getAttribute('data-share-url');
    // The link in `url` as well as in the words: Telegram and WhatsApp draw a map
    // preview card from `url`, not from a link buried in `text`.
    var data = { title: title, text: text, url: url };

    if (navigator.share && (!navigator.canShare || navigator.canShare(data))) {
      native.hidden = false;
      native.addEventListener('click', function () {
        navigator.share(data).catch(function (err) {
          // Closing the sheet is not a failure.
          if (err && err.name !== 'AbortError') {
            say(status, "Ulashib bo'lmadi — pastdagi tugmalardan foydalaning.", 'error');
          }
        });
      });
    }

    root.querySelector('[data-share-copy]').addEventListener('click', function () {
      copyText(text, root).then(function () { say(status, '✓ Nusxa olindi', 'ok'); },
        function () { say(status, "Nusxa olib bo'lmadi.", 'error'); });
    });
  }

  function enhance(scope) {
    scope = scope || document;
    scope.querySelectorAll('[data-geo-picker]').forEach(initPicker);
    scope.querySelectorAll('[data-geo-view]').forEach(initView);
    scope.querySelectorAll('[data-geo-share]').forEach(initShare);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { enhance(); });
  } else { enhance(); }
  document.addEventListener('modal:loaded', function (e) { enhance(e.detail || document); });
})();
