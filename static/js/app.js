(function () {
  "use strict";
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ---------- theme ---------- */
  $$("[data-set-theme]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = b.getAttribute("data-set-theme");
      document.documentElement.setAttribute("data-theme", t);
      try { localStorage.setItem("fc-theme", t); } catch (e) {}
    });
  });

  /* ---------- ambient sound (generated in the browser, no audio files) ---------- */
  var soundBtn = $("#sound"), ctx = null, nodes = null;
  function startSound() {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return false;
    ctx = ctx || new AC();
    if (ctx.state === "suspended") ctx.resume();
    var master = ctx.createGain(); master.gain.value = 0;
    var filter = ctx.createBiquadFilter(); filter.type = "lowpass"; filter.frequency.value = 520; filter.Q.value = 6;
    var lfo = ctx.createOscillator(), lfoGain = ctx.createGain();
    lfo.frequency.value = 0.08; lfoGain.gain.value = 260;
    lfo.connect(lfoGain); lfoGain.connect(filter.frequency);
    var freqs = [55, 82.41, 110.3, 164.8], oscs = freqs.map(function (f, i) {
      var o = ctx.createOscillator(); o.type = i % 2 ? "sawtooth" : "sine"; o.frequency.value = f;
      o.detune.value = (i - 1.5) * 6;
      var g = ctx.createGain(); g.gain.value = i % 2 ? 0.05 : 0.12;
      o.connect(g); g.connect(filter); o.start(); return o;
    });
    filter.connect(master); master.connect(ctx.destination); lfo.start();
    master.gain.linearRampToValueAtTime(0.35, ctx.currentTime + 2.5);
    nodes = { master: master, oscs: oscs.concat([lfo]) };
    return true;
  }
  function stopSound() {
    if (!nodes) return;
    var n = nodes; nodes = null;
    n.master.gain.cancelScheduledValues(ctx.currentTime);
    n.master.gain.linearRampToValueAtTime(0, ctx.currentTime + 0.8);
    setTimeout(function () { n.oscs.forEach(function (o) { try { o.stop(); } catch (e) {} }); }, 900);
  }
  if (soundBtn) soundBtn.addEventListener("click", function () {
    var on = soundBtn.getAttribute("aria-pressed") === "true";
    if (on) { stopSound(); } else if (!startSound()) { return; }
    soundBtn.setAttribute("aria-pressed", on ? "false" : "true");
    soundBtn.querySelector(".lbl").textContent = on ? "Sound off" : "Sound on";
  });

  /* ---------- confirm dialogs for destructive forms ---------- */
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  /* ---------- game page ---------- */
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "X-CSRF": csrf }, body: body || new FormData(), credentials: "same-origin" });
  }
  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }

  $$(".item .check").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var li = btn.closest(".item"), id = li.getAttribute("data-id");
      btn.disabled = true;
      post("/item/" + id + "/toggle").then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
        .then(function (d) {
          li.classList.toggle("done", d.done);
          btn.setAttribute("aria-checked", d.done ? "true" : "false");
          var cat = li.closest(".cat").id.replace("cat", "");
          setText("c" + cat + "-done", d.cat_done); setText("c" + cat + "-total", d.cat_total);
          var bar = document.getElementById("c" + cat + "-bar"); if (bar) bar.style.width = d.cat_pct + "%";
          setText("g-done", d.game_done); setText("g-total", d.game_total);
          var ring = document.getElementById("g-ring");
          if (ring) {
            ring.setAttribute("stroke-dashoffset", (326.73 * (1 - d.game_pct / 100)).toFixed(2));
            var num = ring.parentNode.querySelector(".ring-num"); if (num) num.textContent = d.game_pct + "%";
          }
          applyFilter();
        })
        .catch(function () { window.alert("Could not save. Check your connection and try again."); })
        .then(function () { btn.disabled = false; });
    });
  });

  $$(".note").forEach(function (inp) {
    inp.addEventListener("change", function () {
      var fd = new FormData(); fd.append("note", inp.value);
      post("/item/" + inp.getAttribute("data-note") + "/note", fd);
      var li = inp.closest(".item");
      li.setAttribute("data-text", (li.querySelector(".txt").textContent + " " + inp.value).toLowerCase());
    });
  });

  var filter = "all", query = "";
  function applyFilter() {
    $$(".item").forEach(function (li) {
      var done = li.classList.contains("done");
      var ok = (filter === "all" || (filter === "done" ? done : !done)) &&
               (!query || (li.getAttribute("data-text") || "").indexOf(query) > -1);
      li.hidden = !ok;
    });
  }
  var q = $("#q");
  if (q) q.addEventListener("input", function () { query = q.value.trim().toLowerCase(); applyFilter(); });
  $$("[data-filter]").forEach(function (b) {
    b.addEventListener("click", function () {
      filter = b.getAttribute("data-filter");
      $$("[data-filter]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      applyFilter();
    });
  });

  /* ---------- payment return: wait for the webhook to unlock ---------- */
  var pay = $("#pay");
  if (pay && pay.getAttribute("data-key")) {
    var key = pay.getAttribute("data-key"), tries = 0;
    var timer = setInterval(function () {
      tries++;
      fetch("/api/entitlements", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.owned.indexOf(key) > -1) {
          clearInterval(timer);
          setText("pay-title", "Unlocked");
          setText("pay-msg", "Your purchase is ready. Taking you there now.");
          setTimeout(function () { window.location.href = pay.getAttribute("data-next"); }, 1200);
        } else if (tries > 40) {
          clearInterval(timer);
          setText("pay-msg", "Still waiting for confirmation. If you were charged, it will unlock within a few minutes. Refresh this page to check.");
        }
      }).catch(function () {});
    }, 3000);
  }
})();
