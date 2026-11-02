/* PF Monitor UI — vanilla JS, no dependencies. */
"use strict";

const $ = (s) => document.querySelector(s);
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
let meters = [];
let currentMeter = null;

/* ------------------------------------------------------------- utilities */
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), 3500);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) {               // session missing/expired
    location.href = "/login";
    throw new Error("authentication required");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}
let ME = null;                           // {username, role} after boot
const isAdmin = () => ME && ME.role === "admin";
const esc = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pf3 = (v) => (v == null ? "—" : v.toFixed(3));

/* ------------------------------------------------------------------ tabs */
document.querySelectorAll("nav#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav#tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`#tab-${b.dataset.tab}`).classList.add("active");
    if (b.dataset.tab === "review") loadReview();
    if (b.dataset.tab === "alerts") loadAlerts();
    if (b.dataset.tab === "config") loadConfig();
  }));

/* ------------------------------------------------------------- simulate */
async function runSimulation() {
  const btns = [$("#btn-simulate"), ...document.querySelectorAll(".empty button")];
  btns.forEach((b) => b && (b.disabled = true));
  toast("Simulating 35 days of readings through the ingest pipeline…");
  try {
    const res = await api("/api/simulate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days: 35 }),
    });
    toast(`Done: ${res.ingest.accepted} accepted, ${res.ingest.flagged} flagged ` +
      `for review, ${res.alerts_fired} alert(s) fired`);
    await boot();
  } catch (e) {
    toast(`Simulation failed: ${e.message}`);
  } finally {
    btns.forEach((b) => b && (b.disabled = false));
  }
}
$("#btn-simulate").addEventListener("click", runSimulation);
window.runSimulation = runSimulation;
$("#btn-logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (e) { /* session gone */ }
  location.href = "/login";
});

/* ------------------------------------------------------------ dashboard */
async function boot() {
  if (!ME) {
    ME = await api("/api/me");
    $("#whoami").innerHTML = `${esc(ME.username)}<span class="role">${
      ME.role === "admin" ? "SUPER ADMIN" : "END USER"}</span>`;
    $("#btn-simulate").classList.toggle("hidden", !isAdmin());
  }
  meters = await api("/api/meters");
  const total = meters.reduce((a, m) => a + m.readings, 0);
  const pending = meters.reduce((a, m) => a + m.review_pending, 0);
  badge("#badge-review", pending);
  $("#dash-empty").classList.toggle("hidden", total > 0);
  $("#dash-body").classList.toggle("hidden", total === 0);
  if (total === 0 && !isAdmin()) {
    $("#dash-empty").querySelector("button").outerHTML =
      '<p class="muted"><em>Ask the administrator to load data.</em></p>';
  }
  renderPicker();
  if (total > 0) {
    if (!currentMeter) currentMeter = meters[0].meter_id;
    await loadSummary(currentMeter);
    api("/api/alerts").then((a) => badge("#badge-alerts",
      a.filter((x) => x.state === "active").length)).catch(() => {});
  }
}
function badge(sel, n) {
  const el = $(sel);
  el.textContent = n;
  el.classList.toggle("show", n > 0);
}
function renderPicker() {
  $("#meter-picker").innerHTML = meters.map((m) => `
    <button data-m="${esc(m.meter_id)}" class="${m.meter_id === currentMeter ? "active" : ""}">
      <span class="mname">${esc(m.name || m.meter_id)}</span>
      <span class="mid">${esc(m.meter_id)} · ${m.readings.toLocaleString()} readings</span>
    </button>`).join("");
  document.querySelectorAll("#meter-picker button").forEach((b) =>
    b.addEventListener("click", () => {
      currentMeter = b.dataset.m;
      renderPicker();
      loadSummary(currentMeter);
    }));
}

async function loadSummary(meterId) {
  const s = await api(`/api/summary?meter_id=${encodeURIComponent(meterId)}`);
  const c = s.cycle, p = s.projection, prev = s.previous_cycle;
  const chip = (band) => band ? `<span class="chip ${band}">${band}</span>` : "";
  const nat = (n) => n ? `<span class="chip ${n}">${n}</span>` : "";
  $("#tiles").innerHTML = `
    <div class="tile"><div class="k">Cycle-to-date PF</div>
      <div class="v">${pf3(c && c.pf)}${chip(c && c.band)}</div>
      <div class="s">since ${c ? c.start : "—"} · day ${c ? c.days_elapsed : "—"}/${c ? c.days_total : "—"} · ${nat(c && c.nature)}</div></div>
    <div class="tile"><div class="k">Projected cycle PF</div>
      <div class="v">${pf3(p && p.pf)}${chip(p && p.band)}</div>
      <div class="s">cycle-to-date + trailing ${p ? p.basis_days : 0}-day average</div></div>
    <div class="tile"><div class="k">Cost of poor PF (cycle)</div>
      <div class="v">₹${c && c.excess_cost_inr != null ? inr.format(c.excess_cost_inr) : "—"}</div>
      <div class="s">${c && c.excess_kvah != null ? inr.format(c.excess_kvah) + " excess kVAh" : ""} @ demo rate</div></div>
    <div class="tile"><div class="k">Last cycle PF</div>
      <div class="v">${pf3(prev && prev.pf)}${chip(prev && prev.band)}</div>
      <div class="s">${prev ? prev.start + " → " + prev.end : "no previous cycle"}
        · ₹${prev && prev.excess_cost_inr != null ? inr.format(prev.excess_cost_inr) : "—"}</div></div>
    <div class="tile"><div class="k">Average load</div>
      <div class="v">${c && c.avg_kw ? Math.round(c.avg_kw) : "—"}<span style="font-size:14px"> kW</span></div>
      <div class="s">cycle-to-date mean</div></div>`;

  const daily = s.daily.slice(-30);
  $("#chart-pf").innerHTML = pfChart(daily);
  $("#chart-excess").innerHTML = barChart(daily);

  if (c) {
    $("#cap-p").value = Math.round(c.avg_kw || 0);
    $("#cap-from").value = c.pf != null ? c.pf.toFixed(3) : "";
    updateCapacitor();
  }
}

/* -------------------------------------------------------------- charts */
function pfChart(daily) {
  const W = 520, H = 210, L = 44, R = 10, T = 12, B = 26;
  const pts = daily.filter((d) => d.pf != null);
  if (!pts.length) return '<p class="muted">no complete daily data yet</p>';
  const y0 = 0.80, y1 = 1.0;
  const x = (i) => L + (i * (W - L - R)) / Math.max(pts.length - 1, 1);
  const y = (v) => T + (y1 - Math.max(v, y0)) * (H - T - B) / (y1 - y0);
  const path = pts.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.pf).toFixed(1)}`).join("");
  const ticks = [0.80, 0.85, 0.90, 0.95, 1.0].map((v) =>
    `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e3e9ef"/>
     <text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="#5a6a79">${v.toFixed(2)}</text>`).join("");
  const xlabels = pts.map((d, i) =>
    i % Math.ceil(pts.length / 6) === 0
      ? `<text x="${x(i)}" y="${H - 8}" text-anchor="middle" font-size="9.5" fill="#5a6a79">${d.day.slice(5)}</text>` : "").join("");
  const dots = pts.map((d, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(d.pf).toFixed(1)}" r="2.6"
       fill="${d.band === "PENALTY" ? "#b3362a" : d.band === "INCENTIVE" ? "#2f6b2f" : "#a16207"}">
       <title>${d.day}: PF ${d.pf.toFixed(3)} (${d.nature}, ${d.band})</title></circle>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">
    <rect x="${L}" y="${y(0.9)}" width="${W - L - R}" height="${y(y0) - y(0.9)}" fill="#fbe4e0" opacity=".55"/>
    <rect x="${L}" y="${y(1.0)}" width="${W - L - R}" height="${y(0.95) - y(1.0)}" fill="#e3f2e3" opacity=".6"/>
    ${ticks}
    <path d="${path}" fill="none" stroke="#0b4f8a" stroke-width="1.8"/>
    ${dots}${xlabels}
    <text x="${W - R}" y="${y(0.9) - 4}" text-anchor="end" font-size="9.5" fill="#b3362a">penalty &lt; 0.90</text>
    <text x="${W - R}" y="${y(0.95) - 4}" text-anchor="end" font-size="9.5" fill="#2f6b2f">incentive &gt; 0.95 (lag only)</text>
  </svg>`;
}
function barChart(daily) {
  const W = 520, H = 210, L = 52, R = 10, T = 12, B = 26;
  const pts = daily.filter((d) => d.excess_cost_inr != null);
  if (!pts.length) return '<p class="muted">no kVAh data yet</p>';
  const max = Math.max(...pts.map((d) => d.excess_cost_inr), 1);
  const bw = (W - L - R) / pts.length;
  const y = (v) => T + (1 - v / max) * (H - T - B);
  const bars = pts.map((d, i) =>
    `<rect x="${(L + i * bw + 1).toFixed(1)}" y="${y(d.excess_cost_inr).toFixed(1)}"
       width="${Math.max(bw - 2, 1).toFixed(1)}" height="${(H - B - y(d.excess_cost_inr)).toFixed(1)}"
       fill="#c05621" opacity=".85">
       <title>${d.day}: ₹${inr.format(d.excess_cost_inr)} (${inr.format(d.excess_kvah)} kVAh excess)</title></rect>`).join("");
  const ticks = [0, 0.5, 1].map((f) =>
    `<line x1="${L}" x2="${W - R}" y1="${y(f * max)}" y2="${y(f * max)}" stroke="#e3e9ef"/>
     <text x="${L - 6}" y="${y(f * max) + 4}" text-anchor="end" font-size="10" fill="#5a6a79">₹${inr.format(f * max)}</text>`).join("");
  const xlabels = pts.map((d, i) =>
    i % Math.ceil(pts.length / 6) === 0
      ? `<text x="${L + i * bw + bw / 2}" y="${H - 8}" text-anchor="middle" font-size="9.5" fill="#5a6a79">${d.day.slice(5)}</text>` : "").join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">${ticks}${bars}${xlabels}</svg>`;
}

/* ----------------------------------------------------------- capacitor */
function updateCapacitor() {
  const p = parseFloat($("#cap-p").value), a = parseFloat($("#cap-from").value),
        b = parseFloat($("#cap-to").value);
  const out = $("#cap-result");
  if (!(p > 0) || !(a > 0 && a < 1) || !(b > a && b <= 1)) { out.textContent = "—"; return; }
  const q = p * (Math.tan(Math.acos(a)) - Math.tan(Math.acos(b)));
  out.textContent = `≈ ${Math.round(q)} kVAr`;
}
["#cap-p", "#cap-from", "#cap-to"].forEach((s) =>
  $(s).addEventListener("input", updateCapacitor));

/* -------------------------------------------------------------- review */
async function loadReview() {
  const rows = await api("/api/review");
  badge("#badge-review", rows.length);
  if (!rows.length) {
    $("#review-list").innerHTML = '<p class="muted">Review queue is empty.</p>';
    return;
  }
  const actions = (r) => isAdmin()
    ? `<td class="rowbtns">
        <button onclick="adjudicate(${r.id},'confirm')">Confirm</button>
        <button onclick="adjudicate(${r.id},'correct')">Correct…</button>
        <button onclick="adjudicate(${r.id},'reject')">Reject</button>
      </td>`
    : '<td class="muted">read-only</td>';
  $("#review-list").innerHTML = `<table class="grid">
    <tr><th>Timestamp</th><th>Meter</th><th>Register</th><th>Value</th>
        <th>Conf.</th><th>Flag reason</th><th>Actions</th></tr>
    ${rows.map((r) => `<tr>
      <td>${esc(r.ts)}</td><td>${esc(r.meter_id)}</td><td>${esc(r.register)}</td>
      <td class="num">${r.value.toLocaleString()}</td>
      <td class="num">${r.confidence != null ? r.confidence.toFixed(3) : "—"}</td>
      <td class="reason">${esc(r.flag_reason || "")}</td>
      ${actions(r)}</tr>`).join("")}
  </table>
  <p class="fine">${isAdmin()
    ? "In production each row shows the source meter image beside the OCR output (§7.4); the simulator carries no images."
    : "Adjudication requires the super-admin role — flagged readings are shown read-only."}</p>`;
}
window.adjudicate = async function (id, action) {
  let value = null;
  if (action === "correct") {
    value = prompt("Corrected register value (as read from the source image):");
    if (value === null || value === "") return;
  }
  try {
    await api("/api/review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, action, value, reviewer: "web-ui" }),
    });
    toast(action === "reject" ? "Reading rejected"
      : "Verified reading re-entered the pipeline as human_verified");
    loadReview();
    if (currentMeter) loadSummary(currentMeter);
  } catch (e) { toast(`Review failed: ${e.message}`); }
};

/* -------------------------------------------------------------- alerts */
async function loadAlerts() {
  const rows = await api("/api/alerts");
  badge("#badge-alerts", rows.filter((x) => x.state === "active").length);
  if (!rows.length) {
    $("#alerts-list").innerHTML = '<p class="muted">No alerts fired.</p>';
    return;
  }
  $("#alerts-list").innerHTML = `<table class="grid">
    <tr><th>Fired</th><th>Severity</th><th>Rule</th><th>Meter</th><th>Message</th></tr>
    ${rows.map((a) => `<tr>
      <td>${esc(a.fired_at)}</td>
      <td><span class="sev ${a.severity}">${a.severity}</span></td>
      <td>${esc(a.rule)}</td><td>${esc(a.meter_id)}</td>
      <td>${esc(a.message)}</td></tr>`).join("")}
  </table>`;
}

/* -------------------------------------------------------------- config */
async function loadConfig() {
  const cfg = await api("/api/config");
  $("#config-dump").textContent = JSON.stringify(cfg, null, 2);
}

boot().catch((e) => toast(`Load failed: ${e.message}`));
