"use strict";
// playcap UI. Talks only to the local server's /api; all data is inserted with
// textContent, never innerHTML, so a page or item title cannot inject markup.

const TOKEN = document.querySelector('meta[name="playcap-token"]').content;
const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
const FORCE_AFTER_MS = 45000;   // offer "Force stop" when a stop has not landed by then

let snap = null;
let setup = null;
let wizStep = 1;
let chosenSource = null;
let queueFilter = "attention";
let stopSeen = {};               // job -> first time we saw it "stopping"
let busy = false;

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Playcap-Token": TOKEN },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok && data.ok === undefined) data = { ok: false, message: `HTTP ${res.status}` };
  return data;
}

function toast(message, ok = true) {
  const t = el("div", { class: "toast" + (ok ? "" : " bad"), text: message });
  $("toasts").append(t);
  setTimeout(() => t.remove(), ok ? 4000 : 8000);
}

async function act(path, body, after) {
  if (busy) return;
  busy = true;
  try {
    const r = await api(path, body);
    if (r.message) toast(r.message, r.ok !== false);
    if (after) after(r);
  } catch (e) {
    toast("The playcap UI server is not responding.", false);
  } finally {
    busy = false;
    refresh();
  }
}

const job = (action, name, extra = {}) => act(`/api/job/${action}`, { job: name, ...extra });

function gb(x) { return x == null ? "—" : (x >= 10 ? x.toFixed(0) : x.toFixed(2)) + " GB"; }
function clock(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

// ---------------------------------------------------------------- polling
async function refresh() {
  try {
    snap = await api("/api/state");
  } catch (e) {
    $("health").replaceChildren(el("span", { class: "dot", text: "UI server stopped" }));
    return;
  }
  if (!snap || snap.configured === undefined) return;
  if (!snap.configured) {
    if ($("wizard").hidden) openWizard(false);
    return;
  }
  if (!$("wizard").hidden) return;          // don't yank the user out of settings
  $("dash").hidden = false;
  $("settings-btn").hidden = false;
  renderDash();
}

setInterval(() => { if (!document.hidden && !busy) refresh(); }, POLL_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

// ---------------------------------------------------------------- dashboard
function renderHealth() {
  const h = snap.health || {};
  const items = [
    el("span", { class: "dot" + (h.browser ? " ok" : ""), text: h.browser ? "Browser" : "Browser closed" }),
    el("span", { class: "dot" + (h.obs ? " ok" : ""), text: h.obs ? "OBS" : "OBS offline" }),
  ];
  if (h.disk_gb != null) {
    items.push(el("span", { class: "dot" + (h.disk_gb >= 10 ? " ok" : ""), text: `${h.disk_gb.toFixed(0)} GB free` }));
  }
  $("health").replaceChildren(...items);
  $("showname").textContent = snap.show ? "· " + snap.show : "";
}

const FIXES = {
  setup: ["Open settings", () => openWizard(true)],
  launch_obs: ["Launch OBS", () => act("/api/obs/launch", {})],
  start_browser: ["Open browser", () => job("start", "browser")],
  build_queue: ["Refresh queue", () => job("start", "queue")],
};

function renderProblems() {
  const recording = isRunning("record");
  const list = (snap.problems || []).filter((p) => !(recording && p.code === "queue"));
  $("problems").replaceChildren(...list.map((p) => {
    const fix = FIXES[p.fix];
    return el("li", {}, el("span", { text: p.text }),
      fix ? el("button", { class: "btn small", onclick: fix[1], text: fix[0] }) : null);
  }));
}

function isRunning(name) {
  return !!(snap.jobs[name] && snap.jobs[name].running) || !!(snap.external && snap.external[name]);
}

function stoppingTooLong(name) {
  const j = snap.jobs[name] || {};
  if (!j.running || !j.stopping) { delete stopSeen[name]; return false; }
  stopSeen[name] = stopSeen[name] || Date.now();
  return Date.now() - stopSeen[name] > FORCE_AFTER_MS;
}

function renderControls() {
  const rec = isRunning("record"), opt = isRunning("optimize");
  const recJob = snap.jobs.record || {}, optJob = snap.jobs.optimize || {};
  const c = snap.counts || {};
  const toRecord = (c.waiting || 0) + (c.failed || 0);
  const external = snap.external || {};

  $("rec-start").disabled = rec || opt || toRecord === 0;
  $("rec-start").textContent = rec ? "Recording…" : "Start recording";
  $("rec-stop-after").hidden = !rec || recJob.stopping_after || recJob.stopping;
  $("rec-stop-now").hidden = !rec || recJob.stopping;
  $("rec-kill").hidden = !stoppingTooLong("record");

  let note = "";
  if (opt) note = "Stop re-compressing before recording — both at once drops frames.";
  else if (recJob.stopping) note = "Stopping… the current recording is being closed cleanly.";
  else if (recJob.stopping_after) note = "Will stop when the current item finishes.";
  else if (rec && external.record) note = "Recording was started outside this window.";
  else if (!rec) note = toRecord ? `${toRecord} item${toRecord > 1 ? "s" : ""} ready to record.` : "Nothing ready to record.";
  $("rec-note").textContent = note;

  $("opt-start").disabled = rec || opt;
  $("opt-start").textContent = opt ? "Re-compressing…" : "Re-compress library";
  $("opt-stop").hidden = !opt || optJob.stopping;
  $("opt-kill").hidden = !stoppingTooLong("optimize");
  $("opt-note").textContent = optJob.stopping ? "Stopping after ffmpeg exits — the file in progress is discarded, the original is untouched."
    : (opt && external.optimize ? "Re-compressing was started outside this window." :
      "Optional. Only helps recordings made at a fixed bitrate (about 1.3x smaller); files recorded in a quality mode are skipped. Originals are kept until each new file is checked.");

  $("browser-start").disabled = !!(snap.health || {}).browser;
  $("browser-start").textContent = (snap.health || {}).browser ? "Browser open" : "Open browser";
  $("queue-start").disabled = rec || isRunning("queue");
  $("queue-start").textContent = isRunning("queue") ? "Refreshing…" : "Refresh queue";
}

function renderNow() {
  const n = snap.now;
  $("now").hidden = !n;
  if (n) {
    $("now-title").textContent = n.title || "";
    const pct = n.duration ? Math.min(100, (100 * (n.t || 0)) / n.duration) : 0;
    $("now-bar").style.width = pct.toFixed(1) + "%";
    const res = n.w && n.h ? ` · ${n.w}×${n.h}` : "";
    $("now-meta").textContent = `${clock(n.t)} of ${clock(n.duration)} (${pct.toFixed(0)}%)` +
      ` · recording for ${clock(n.elapsed)}${res}`;
    $("now-black").hidden = !n.black;
  }
  const e = snap.encode;
  $("encode").hidden = !(isRunning("optimize") && e);
  if (e) {
    $("encode-meta").textContent = `Encoded up to ${e.time || "—"} · speed ${e.speed || "—"}` +
      (e.bitrate ? ` · ${e.bitrate}` : "");
  }
}

const TABS = [
  ["attention", "To record", (i) => i.state === "waiting" || i.state === "failed"],
  ["done", "Done", (i) => i.state === "done"],
  ["not_aired", "Not aired", (i) => i.state === "not_aired"],
  ["locked", "Locked", (i) => i.state === "locked"],
  ["skipped", "Skipped", (i) => i.state === "skipped"],
  ["all", "All", () => true],
];
const CHIP = { done: "done", failed: "failed", skipped: "skipped", waiting: "waiting",
  not_aired: "not aired", locked: "locked" };

function renderQueue() {
  const items = snap.items || [];
  $("queue-tabs").replaceChildren(...TABS.map(([key, label, pred]) => {
    const n = items.filter(pred).length;
    return el("button", { class: "tab" + (queueFilter === key ? " active" : ""), role: "tab",
      onclick: () => { queueFilter = key; renderQueue(); }, text: `${label} ${n}` });
  }));
  const pred = (TABS.find((t) => t[0] === queueFilter) || TABS[0])[2];
  const rows = items.filter(pred);
  const rec = isRunning("record");
  const nowId = snap.now && snap.now.id;
  $("queue-body").replaceChildren(...rows.map((i) => {
    const recordingThis = rec && nowId === i.id;
    const actions = [];
    if (!rec && !isRunning("optimize") && (i.state === "waiting" || i.state === "failed")) {
      actions.push(el("button", { class: "btn small", text: "Record",
        title: "Record just this one, then stop",
        onclick: () => job("start", "record", { item: i.id }) }));
    }
    if (!rec) {
      if (i.state === "failed") actions.push(el("button", { class: "btn small", text: "Retry",
        onclick: () => act("/api/item/retry", { id: i.id }) }));
      if (i.state === "waiting" || i.state === "failed") actions.push(el("button", { class: "btn small ghost",
        text: "Skip", onclick: () => act("/api/item/skip", { id: i.id }) }));
      if (i.state === "skipped") actions.push(el("button", { class: "btn small", text: "Unskip",
        onclick: () => act("/api/item/unskip", { id: i.id }) }));
    }
    return el("tr", {},
      el("td", {}, el("div", { text: i.title }),
        i.state === "failed" && i.error ? el("div", { class: "err-text", text: i.error, title: i.error_raw || "" }) : null),
      el("td", { class: "nowrap muted small", text: [i.day, i.time].filter((x) => x && x !== "-").join(" ") }),
      el("td", {}, el("span", { class: "chip " + (recordingThis ? "recording" : i.state),
        text: recordingThis ? "recording" : (CHIP[i.state] || i.state) })),
      el("td", { class: "actions" }, ...actions));
  }));
  const empty = rows.length === 0;
  $("queue-empty").hidden = !empty;
  $("queue-empty").textContent = items.length === 0
    ? "The queue is empty. Press “Refresh queue” to read it from your source."
    : "Nothing in this list.";
}

function renderLibrary() {
  const lib = snap.library || [];
  const total = lib.reduce((a, e) => a + e.gb, 0);
  const shrunk = lib.filter((e) => e.optimized).length;
  $("lib-summary").textContent = lib.length
    ? `${lib.length} episodes · ${gb(total)} · ${shrunk} shrunk` : "No recordings yet";
  $("lib-body").replaceChildren(...lib.map((e) => el("tr", {},
    el("td", { text: e.name }),
    el("td", { class: "num nowrap", text: gb(e.gb) + (e.before_gb ? `  (was ${gb(e.before_gb)})` : "") }),
    el("td", {}, el("span", { class: "chip " + (e.optimized ? "done" : ""), text: e.optimized ? "yes" : "not yet" })),
  )));
}

const LOG_NAMES = { record: "Recording", optimize: "Re-compressing", queue: "Queue", browser: "Browser" };
function renderLogs() {
  if (!document.querySelector(".logs").open) return;
  const logs = snap.log || {};
  $("logs").replaceChildren(...Object.keys(LOG_NAMES).map((k) =>
    el("div", {}, el("h4", { text: LOG_NAMES[k] }), el("pre", { text: logs[k] || "(empty)" }))));
}

function renderDash() {
  renderHealth();
  renderProblems();
  renderControls();
  renderNow();
  renderQueue();
  renderLibrary();
  renderLogs();
}

$("rec-start").onclick = () => job("start", "record");
$("rec-stop-after").onclick = () => job("stop", "record", { mode: "after_current" });
$("rec-stop-now").onclick = () => {
  if (confirm("Stop now? The recording in progress is discarded; the item stays in the queue.")) {
    job("stop", "record", { mode: "now" });
  }
};
$("rec-kill").onclick = () => {
  if (confirm("Force stop the recorder? playcap will also stop OBS's recording.")) job("kill", "record");
};
$("opt-start").onclick = () => job("start", "optimize");
$("opt-stop").onclick = () => job("stop", "optimize", { mode: "now" });
$("opt-kill").onclick = () => {
  if (confirm("Force stop re-compressing? The half-written file may be left behind; the original is untouched.")) {
    job("kill", "optimize");
  }
};
$("browser-start").onclick = () => job("start", "browser");
$("queue-start").onclick = () => job("start", "queue");
$("settings-btn").onclick = () => openWizard(true);
document.querySelector(".logs").addEventListener("toggle", () => snap && renderLogs());

// ---------------------------------------------------------------- wizard
const TOOL_LABELS = { chrome: "Chrome", obs: "OBS", ffmpeg: "ffmpeg", ffprobe: "ffprobe" };
const TOOL_KEYS = { chrome: "chrome_exe", obs: "obs_exe", ffmpeg: "ffmpeg", ffprobe: "ffprobe" };

async function openWizard(cancellable) {
  $("dash").hidden = true;
  $("wizard").hidden = false;
  $("settings-btn").hidden = true;
  $("wiz-cancel").hidden = !cancellable;
  setup = await api("/api/setup");
  renderTools();
  renderObsStatus();
  const cfg = setup.config || {};
  chosenSource = cfg.adapter || (setup.adapters[0] && setup.adapters[0].module);
  renderSources();
  $("output_dir").value = cfg.output_dir || (setup.root ? setup.root.replace(/[\\/]+$/, "") + (setup.root.includes("\\") ? "\\" : "/") + "recordings" : "");
  $("show").value = cfg.show || "My Recordings";
  renderQuality();
  showStep(1);
}

// ---- recording quality (record_quality.py holds the rules) ----
const QUALITY_CHOICES = [
  ["small", "Small", "CRF 28 · slides, whiteboards, talking heads. Smallest files."],
  ["balanced", "Balanced", "CRF 24 · good for most video. Recommended."],
  ["high", "High", "CRF 20 · fast motion or fine detail. Bigger files."],
  ["bitrate", "Fixed bitrate", "Same size every hour. Bigger; can be re-compressed later."],
];
let qualityChoice = "balanced";

function renderQuality() {
  const rec = setup.recording || {};
  const v = rec.values || {};
  qualityChoice = qualityChoice && setup._qualityTouched ? qualityChoice : (rec.preset || "balanced");
  const choices = QUALITY_CHOICES.concat(rec.preset === "custom" ? [["custom", "Custom", "Your own values below."]] : []);
  $("quality").replaceChildren(...choices.map(([key, label, help]) => {
    const radio = el("input", { type: "radio", name: "quality", value: key, checked: key === qualityChoice });
    radio.addEventListener("change", () => {
      qualityChoice = key; setup._qualityTouched = true;
      const p = (rec.presets || {})[key];
      if (p) $("record_crf").value = p.record_crf;
      renderQuality();
    });
    return el("label", { class: "source" + (key === qualityChoice ? " selected" : "") },
      radio, el("strong", { text: label }), el("small", { class: "muted", text: " " + help }));
  }));
  if (!setup._qualityFilled) {
    $("record_crf").value = v.record_crf ?? 24;
    $("video_bitrate_kbps").value = v.video_bitrate_kbps ?? 2500;
    $("keyframe_seconds").value = v.keyframe_seconds ?? 2;
    $("x264_preset").replaceChildren(...(rec.x264_presets || ["veryfast"]).map((p) =>
      el("option", { value: p, text: p, selected: p === (v.x264_preset || "veryfast") })));
    setup._qualityFilled = true;
  }
  $("quality-obs").textContent = rec.obs_in_sync
    ? "OBS is set to this already."
    : "OBS picks this up the next time playcap starts it: close OBS, then press Launch OBS (step 1).";
}

function collectQuality() {
  const out = {
    record_mode: qualityChoice === "bitrate" ? "bitrate" : "quality",
    record_crf: Number($("record_crf").value),
    video_bitrate_kbps: Number($("video_bitrate_kbps").value),
    x264_preset: $("x264_preset").value,
    keyframe_seconds: Number($("keyframe_seconds").value),
  };
  return out;
}

function renderTools() {
  $("tools").replaceChildren(...Object.entries(setup.tools).map(([name, t]) => {
    const input = el("input", { type: "text", id: "tool-" + name, spellcheck: "false",
      placeholder: "not found — browse to it" });
    input.value = t.path || "";
    return el("div", { class: "tool" },
      el("span", { class: "name", text: TOOL_LABELS[name] }),
      el("span", { class: "mark " + (t.ok ? "ok" : "bad"), text: t.ok ? "✓" : "✗" }),
      el("div", {}, input, el("small", { class: "err", "data-err": TOOL_KEYS[name] })),
      el("button", { class: "btn", "data-browse": "tool-" + name, "data-kind": "file", text: "Browse…" }));
  }));
}

function renderObsStatus() {
  const o = setup.obs;
  let text;
  if (!setup.tools.obs.ok) text = "OBS is not installed. Get it free from obsproject.com, then reopen this page.";
  else if (!o) text = "OBS has not been started with its websocket yet. Press Launch OBS — playcap turns it on.";
  else if (!o.enabled) text = setup.obs_running
    ? "OBS's websocket server is off. Close OBS, then press Launch OBS — playcap turns it on."
    : "OBS's websocket server is off. Press Launch OBS — playcap turns it on.";
  else text = setup.obs_running ? "OBS is running and reachable. Press “Set up recording scene” once."
    : "OBS is ready. Launch it, then press “Set up recording scene” once.";
  $("obs-status").textContent = text;
  $("obs-launch").disabled = !setup.tools.obs.ok;
}

$("obs-launch").onclick = () => act("/api/obs/launch", {}, () => setTimeout(async () => {
  setup = await api("/api/setup"); renderObsStatus();
}, 4000));
$("obs-setup").onclick = () => act("/api/obs/setup", {});

function renderSources() {
  $("sources").replaceChildren(...setup.adapters.map((a) => {
    const radio = el("input", { type: "radio", name: "source", value: a.module,
      checked: a.module === chosenSource });
    const card = el("label", { class: "source" + (a.module === chosenSource ? " selected" : "") },
      radio, el("strong", { text: a.label }));
    radio.addEventListener("change", () => { chosenSource = a.module; renderSources(); });
    return card;
  }));
  const a = setup.adapters.find((x) => x.module === chosenSource);
  const cfg = setup.config || {};
  $("source-fields").replaceChildren(...(a ? a.fields : []).map((f) => {
    const id = "field-" + f.key;
    const input = f.kind === "links"
      ? el("textarea", { id, spellcheck: "false", placeholder: "https://example.com/video-page\nIntro | https://example.com/another" })
      : el("input", { id, type: f.kind === "url" ? "url" : "text", spellcheck: "false" });
    input.value = f.kind === "links" ? (setup.links || "") : (cfg[f.key] || "");
    return el("label", { class: "field" }, el("span", { text: f.label }), input,
      f.help ? el("small", { class: "muted", text: f.help }) : null,
      el("small", { class: "err", "data-err": f.key }));
  }));
}

function showStep(n) {
  wizStep = n;
  document.querySelectorAll(".step").forEach((s) => { s.hidden = Number(s.dataset.step) !== n; });
  document.querySelectorAll(".stepper li").forEach((li) => {
    const s = Number(li.dataset.step);
    li.className = s === n ? "active" : (s < n ? "done" : "");
  });
  $("wiz-back").disabled = n === 1;
  $("wiz-next").textContent = n === 3 ? "Finish" : "Next";
}

function collect() {
  const out = {};
  for (const [name, key] of Object.entries(TOOL_KEYS)) {
    const v = ($("tool-" + name) || {}).value;
    out[key] = (v || "").trim();
  }
  out.adapter = chosenSource;
  const a = setup.adapters.find((x) => x.module === chosenSource);
  for (const f of a ? a.fields : []) out[f.key] = $("field-" + f.key).value;
  out.output_dir = $("output_dir").value.trim();
  out.show = $("show").value.trim();
  Object.assign(out, collectQuality());
  return out;
}

const STEP_OF = { chrome_exe: 1, obs_exe: 1, ffmpeg: 1, ffprobe: 1, adapter: 2, output_dir: 3, show: 3,
  record_mode: 3, record_crf: 3, video_bitrate_kbps: 3, x264_preset: 3, keyframe_seconds: 3 };

async function finish() {
  document.querySelectorAll("[data-err]").forEach((e) => { e.textContent = ""; });
  const r = await api("/api/config", collect());
  if (!r.ok) {
    const errors = r.errors || {};
    let first = 3;
    for (const [k, msg] of Object.entries(errors)) {
      const slot = document.querySelector(`[data-err="${k}"]`);
      if (slot) slot.textContent = msg;
      first = Math.min(first, STEP_OF[k] || 2);
    }
    if (!Object.keys(errors).length) toast(r.message || "Could not save settings.", false);
    showStep(first);
    return;
  }
  toast("Settings saved.");
  $("wizard").hidden = true;
  await refresh();
  if (snap && snap.counts && snap.counts.total === 0) toast("Next: open the browser, log in if needed, then press Refresh queue.");
}

$("wiz-next").onclick = () => (wizStep < 3 ? showStep(wizStep + 1) : finish());
$("wiz-back").onclick = () => showStep(Math.max(1, wizStep - 1));
$("wiz-cancel").onclick = () => { $("wizard").hidden = true; refresh(); };

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-browse]");
  if (!b) return;
  ev.preventDefault();
  b.disabled = true;
  try {
    const r = await api("/api/browse", { kind: b.dataset.kind });
    if (r.path) $(b.dataset.browse).value = r.path;
  } finally {
    b.disabled = false;
  }
});

refresh();
