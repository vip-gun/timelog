// Progressive enhancement only - every page works without JavaScript.
(function () {
  "use strict";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Confirmation dialogs: <form data-confirm="..."> or <button data-confirm="...">
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var btn = ev.submitter;
    var msg = (btn && btn.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { ev.preventDefault(); return; }
    if (btn && btn.classList.contains("btn")) btn.classList.add("busy");
  });

  // Auto-submit selects: <select data-autosubmit>
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  // Toasts: dismissable, auto-hide after a while (errors stay longer)
  document.querySelectorAll(".toasts .flash").forEach(function (toast, i) {
    function close() {
      toast.classList.add("leaving");
      setTimeout(function () { toast.remove(); }, 400);
    }
    var x = toast.querySelector(".x");
    if (x) x.addEventListener("click", close);
    var delay = toast.classList.contains("flash-error") ? 9000 : 5000;
    setTimeout(close, delay + i * 400);
  });

  // Staggered entrance: index children of .stagger and rows of .table.animate
  document.querySelectorAll(".stagger").forEach(function (el) {
    Array.prototype.forEach.call(el.children, function (child, i) { child.style.setProperty("--i", i); });
  });
  document.querySelectorAll(".table.animate tbody").forEach(function (tbody) {
    Array.prototype.forEach.call(tbody.rows, function (row, i) { row.style.setProperty("--i", Math.min(i, 40)); });
  });

  // Count-up numbers: <span data-count="123.45" data-decimals="2">
  var fmtCache = {};
  function fmt(value, decimals) {
    var key = String(decimals);
    if (!fmtCache[key]) fmtCache[key] = new Intl.NumberFormat("en-GB", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    return fmtCache[key].format(value);
  }
  document.querySelectorAll("[data-count]").forEach(function (el) {
    var target = Number.parseFloat(el.getAttribute("data-count")) || 0;
    var decimals = Number.parseInt(el.getAttribute("data-decimals") || "0", 10);
    if (reduceMotion || target === 0) { el.textContent = fmt(target, decimals); return; }
    var start = null, duration = 400;
    function step(ts) {
      if (start === null) start = ts;
      var p = Math.min((ts - start) / duration, 1);
      var eased = p;  // linear count-up
      el.textContent = fmt(target * eased, decimals);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });

  // Signature pads (<div data-signature>): draw with pointer events, store a PNG data URL in the hidden input.
  document.querySelectorAll("[data-signature]").forEach(function (block) {
    var canvas = block.querySelector(".js-signature-pad");
    var data = block.querySelector(".js-signature-data");
    var confirmBox = block.querySelector(".js-signature-confirm");
    var errorBox = block.querySelector(".signature-error");
    var ctx = canvas.getContext("2d");
    var drawing = false, drawn = false, last = null;

    ctx.lineWidth = 2.4; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#1A202C";
    function point(ev) {
      var r = canvas.getBoundingClientRect();
      return { x: (ev.clientX - r.left) * canvas.width / r.width, y: (ev.clientY - r.top) * canvas.height / r.height };
    }
    canvas.addEventListener("pointerdown", function (ev) {
      drawing = true; last = point(ev); canvas.setPointerCapture(ev.pointerId);
      ctx.beginPath(); ctx.arc(last.x, last.y, 1.1, 0, Math.PI * 2); ctx.fillStyle = ctx.strokeStyle; ctx.fill();
    });
    canvas.addEventListener("pointermove", function (ev) {
      if (!drawing) return;
      var p = point(ev);
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      last = p; drawn = true;
    });
    function end() {
      if (!drawing) return;
      drawing = false;
      if (drawn) { data.value = canvas.toDataURL("image/png"); block.classList.add("signed"); errorBox.hidden = true; }
    }
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    function clearPad() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      drawn = false; data.value = ""; block.classList.remove("signed");
    }
    block.querySelector(".js-signature-clear").addEventListener("click", clearPad);

    // A picture (upload or saved signature) is fitted into the pad and stored as PNG like a drawing.
    function showPicture(img) {
      clearPad();
      var scale = Math.min(canvas.width / img.width, canvas.height / img.height, 1);
      var w = img.width * scale, h = img.height * scale;
      ctx.drawImage(img, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
      data.value = canvas.toDataURL("image/png");
      drawn = true; block.classList.add("signed"); errorBox.hidden = true;
    }
    function fail(msg) { errorBox.textContent = msg; errorBox.hidden = false; }
    function loadSource(src) {
      var img = new Image();
      img.onload = function () { showPicture(img); };
      img.onerror = function () { fail("This file could not be read as a picture."); };
      img.src = src;
    }
    var fileInput = block.querySelector(".js-signature-file");
    if (fileInput) {
      fileInput.addEventListener("change", function () {
        var file = fileInput.files && fileInput.files[0];
        fileInput.value = "";
        if (!file) return;
        if (!/^image\//.test(file.type)) { fail("Please choose a picture (PNG or JPEG)."); return; }
        if (file.size > 5 * 1024 * 1024) { fail("This picture is too large (5 MB maximum)."); return; }
        var reader = new FileReader();
        reader.onload = function () { loadSource(reader.result); };
        reader.onerror = function () { fail("This file could not be read."); };
        reader.readAsDataURL(file);
      });
    }
    var savedUrl = block.getAttribute("data-saved-url");
    var savedBtn = block.querySelector(".js-signature-saved");
    if (savedBtn && savedUrl) {
      savedBtn.addEventListener("click", function () { loadSource(savedUrl); });
      loadSource(savedUrl);  // pre-filled: the user still has to confirm the statement
    }

    // Block the submission (not "Save draft") until signed and confirmed.
    block.closest("form").addEventListener("submit", function (ev) {
      var btn = ev.submitter;
      if (btn && btn.name === "action" && btn.value !== "submit") return;
      var msg = !data.value ? "Please sign in the signature box." : (!confirmBox.checked ? "Please tick the confirmation statement." : "");
      if (msg) {
        ev.preventDefault(); ev.stopImmediatePropagation();
        errorBox.textContent = msg; errorBox.hidden = false;
        block.classList.remove("shake"); void block.offsetWidth; block.classList.add("shake");
      }
    }, true);
  });

  // Backup schedule: the weekday only applies to a weekly schedule.
  var bkFrequency = document.getElementById("bk-frequency"), bkWeekday = document.getElementById("bk-weekday");
  if (bkFrequency && bkWeekday) {
    var syncWeekday = function () { bkWeekday.disabled = bkFrequency.value !== "weekly"; };
    bkFrequency.addEventListener("change", syncWeekday);
    syncWeekday();
  }

  // Typed times: "845", "8.45", "8h45" or "8" become HH:MM when the field loses focus.
  document.querySelectorAll("input.js-time").forEach(function (input) {
    input.addEventListener("blur", function () {
      var m = /^(\d{1,2})(?:[:.hH]?(\d{2}))?$/.exec(input.value.trim());
      if (m && (m[2] !== undefined || m[1].length <= 2)) {
        input.value = ("0" + m[1]).slice(-2) + ":" + (m[2] || "00");
      }
    });
  });

  // Overtime form: tell the user when the chosen date is one of their on-call days (detected server-side too).
  var otForm = document.getElementById("ot-form");
  if (otForm) {
    var onCallDates = (otForm.getAttribute("data-oncall-dates") || "").split(",").filter(Boolean);
    var dateInput = document.getElementById("ot-date"), hint = document.getElementById("ot-oncall-hint");
    var updateHint = function () { hint.hidden = onCallDates.indexOf(dateInput.value) === -1; };
    dateInput.addEventListener("change", updateHint);
    dateInput.addEventListener("input", updateHint);
    updateHint();
  }

  // On-call grid: keep checkboxes consistent and show live totals.
  var grid = document.getElementById("oncall-grid");
  if (!grid) return;
  // Rates are only present when amounts may be shown to the viewer.
  var ratesAttr = grid.getAttribute("data-rates");
  var rates = ratesAttr ? JSON.parse(ratesAttr) : null;
  var expert = grid.getAttribute("data-expert") === "1";  // only experts have intervention checkboxes

  function weekRows(key) { return grid.querySelectorAll('tbody tr[data-week="' + key + '"]'); }
  function weekToggle(key) { return grid.querySelector('.js-week[data-week="' + key + '"]'); }

  // Week switch: book / release every day of the rotation week shown in this month.
  function applyWeek(toggle) {
    weekRows(toggle.getAttribute("data-week")).forEach(function (tr) {
      var oc = tr.querySelector(".js-oc"), iv = tr.querySelector(".js-iv");
      if (oc.checked === toggle.checked) return;
      oc.checked = toggle.checked;
      if (!toggle.checked && iv) iv.checked = false;
      recompute(tr);
    });
  }

  // Keep the week switch consistent when single days change.
  function syncWeek(tr) {
    var toggle = weekToggle(tr.getAttribute("data-week"));
    if (!toggle) return;
    var all = Array.prototype.every.call(weekRows(toggle.getAttribute("data-week")), function (row) {
      return row.querySelector(".js-oc").checked;
    });
    if (!all) toggle.checked = false;
    // Never switch on automatically when the week spills into another month (would book those days too).
    else if (toggle.getAttribute("data-spill") !== "1") toggle.checked = true;
  }

  function recompute(changedRow) {
    var total = 0, days = 0, ivs = 0;
    grid.querySelectorAll("tbody tr").forEach(function (tr) {
      var oc = tr.querySelector(".js-oc"), iv = tr.querySelector(".js-iv");
      var ivOn = !!(iv && iv.checked);
      var cell = tr.querySelector(".js-amount");
      tr.classList.toggle("on", oc.checked);
      if (oc.checked) {
        days += 1; if (ivOn) ivs += 1;
        if (rates) {
          var amount = rates[tr.getAttribute("data-type")] * (expert && ivOn ? 2 : 1);
          total += amount;
          cell.textContent = fmt(amount, 2);
        }
      } else if (cell) {
        cell.textContent = "";
      }
    });
    var totalCell = grid.querySelector(".js-total");
    if (totalCell) totalCell.textContent = fmt(total, 2);
    grid.querySelector(".js-days").textContent = days;
    if (expert) grid.querySelector(".js-ivs").textContent = ivs;  // non-experts: derived from overtime (server)
    [totalCell, changedRow && changedRow.querySelector(".js-amount")].forEach(function (el) {
      if (!el) return;
      el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
    });
  }

  grid.addEventListener("change", function (ev) {
    if (ev.target.classList.contains("js-week")) { applyWeek(ev.target); return; }
    var tr = ev.target.closest("tr");
    if (!tr) return;
    if (ev.target.classList.contains("js-iv") && ev.target.checked) tr.querySelector(".js-oc").checked = true;
    if (ev.target.classList.contains("js-oc") && !ev.target.checked && tr.querySelector(".js-iv")) tr.querySelector(".js-iv").checked = false;
    syncWeek(tr);
    recompute(tr);
  });
})();
