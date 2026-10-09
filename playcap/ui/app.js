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
let testWasRunning = false;      // to announce when a 25 s test finishes
let tokenStale = false;          // the server restarted with a new token; only a reload helps
const lastRender = {};           // section -> JSON of the inputs it was last drawn from

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

// The dashboard redraws from a poll every 2 s. Rebuilding a section that has
// not changed would throw away keyboard focus and swallow a click that lands
// mid-rebuild, so each section redraws only when the data it is drawn from
// changes. Functions are left out of the key: list everything a closure
// captures (ids, labels) in the inputs.
function changed(section, inputs) {
  const key = JSON.stringify(inputs);
  if (lastRender[section] === key) return false;
  lastRender[section] = key;
  return true;
}

// A message across the top of the page. Each source ("state", "setup",
// "save") owns its own message, so the dashboard poll recovering clears only
// its own and not, say, a wizard error. A stale page (tokenStale) outranks
// everything: only a reload helps, so the Reload button must stay.
const banners = new Map();             // source -> text, latest last
let serverDown = false;                // the last /api/state fetch did not connect

function showBanner(source, text) {
  banners.delete(source);
  banners.set(source, text);
  drawBanner();
}
function clearBanner(source) {
  if (banners.delete(source)) drawBanner();
}
function drawBanner() {
  let text = null;
  if (tokenStale) {
    text = serverDown
      ? "The playcap UI server is not responding. Start it again (python -m playcap ui), then reload this page."
      : "playcap was restarted. Reload this page to carry on.";
  } else if (banners.size) {
    text = [...banners.values()].pop();
  }
  if (text === null) { $("banner").hidden = true; return; }
  // role=alert: rewriting the same text makes a screen reader say it again.
  if ($("banner-text").textContent !== text) $("banner-text").textContent = text;
  $("banner-reload").hidden = !tokenStale;
  $("banner").hidden = false;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Playcap-Token": TOKEN },
    body: JSON.stringify(body),
  };
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    // The server went away. If it comes back it is a new process with a new
    // token, and the first poll that succeeds cannot tell (GETs carry no
    // token). So from here on the page counts as stale and keeps the Reload
    // banner rather than letting a good /api/state poll clear it.
    tokenStale = true;
    drawBanner();
    throw e;
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!data || typeof data !== "object" || Array.isArray(data)) data = {};
  if (!res.ok && data.ok === undefined) data = { ok: false, message: `HTTP ${res.status}` };
  // Each server start makes a new token. A page left open across a restart
  // still carries the old one, so every button would fail with "bad token".
  if (res.status === 403 && data.message === "bad token") {
    tokenStale = true;
    data.message = "playcap was restarted. Reload this page to carry on.";
    drawBanner();
  }
  return data;
}

function toast(message, ok = true) {
  const t = el("div", { class: "toast" + (ok ? "" : " bad"), text: message,
    title: "Click to dismiss", onclick: () => t.remove() });
  $("toasts").append(t);
  setTimeout(() => t.remove(), ok ? 4000 : 8000);
}

async function act(path, body, after) {
  if (busy) {
    // One action at a time; say so rather than swallowing the click.
    toast("Still working on the last action…");
    return;
  }
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

function hours(min) {
  if (!min) return "";
  return min < 90 ? `${Math.round(min)} min` : `${+(min / 60).toFixed(min < 600 ? 1 : 0)} h`;
}
function gb(x) { return typeof x !== "number" || !isFinite(x) ? "—" : (x >= 10 ? x.toFixed(0) : x.toFixed(2)) + " GB"; }
function clock(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

// ---------------------------------------------------------------- polling
async function refresh() {
  let s;
  try {
    s = await api("/api/state");
  } catch (e) {
    setHealth([["", "UI server stopped"]]);
    serverDown = true;
    drawBanner();                       // api() already set tokenStale
    return;
  }
  if (serverDown) { serverDown = false; drawBanner(); }   // back, but the page is still stale
  if (s.ok === false || s.configured === undefined) {
    // Keep the last good snapshot on screen; say why it is not updating.
    showBanner("state", "Could not read playcap's state: " + (s.message || "unexpected reply from the server."));
    return;
  }
  clearBanner("state");
  snap = s;
  firstRunTick();                           // Try it now + winget installs
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
// #health is an aria-live region: touch it only when its text changes, or a
// screen reader re-announces it on every poll.
function setHealth(dots) {
  if (!changed("health", dots)) return;
  $("health").replaceChildren(...dots.map(([cls, text]) => el("span", { class: "dot" + cls, text })));
}

function renderHealth() {
  const h = snap.health || {};
  const dots = [
    [h.browser ? " ok" : "", h.browser ? "Browser" : "Browser closed"],
    [h.obs ? " ok" : "", h.obs ? "OBS" : "OBS offline"],
  ];
  if (typeof h.disk_gb === "number") {
    dots.push([h.disk_gb >= 10 ? " ok" : "", `${h.disk_gb.toFixed(0)} GB free`]);
  }
  setHealth(dots);
  const show = snap.show ? "· " + snap.show : "";
  if ($("showname").textContent !== show) $("showname").textContent = show;
}

const FIXES = {
  setup: ["Open settings", () => openWizard(true)],
  launch_obs: ["Launch OBS", () => act("/api/obs/launch", {})],
  start_browser: ["Open browser", () => job("start", "browser")],
  build_queue: ["Refresh queue", () => job("start", "queue")],
};

function renderProblems() {
  const recording = isRunning("record");
  // The getting-started checklist already covers the browser and the queue.
  const covered = showStart() ? ["browser", "queue"] : [];
  const list = (snap.problems || []).filter((p) => p &&
    !(recording && p.code === "queue") && !covered.includes(p.code));
  if (!changed("problems", list)) return;
  $("problems").replaceChildren(...list.map((p) => {
    const fix = FIXES[p.fix];
    return el("li", { class: p.level === "info" ? "info" : null }, el("span", { text: p.text }),
      fix ? el("button", { class: "btn small", onclick: fix[1], text: fix[0] }) : null);
  }));
}

function isRunning(name) {
  const j = (snap.jobs || {})[name];
  return !!(j && j.running) || !!(snap.external && snap.external[name]);
}

// ---- first run: an ordered checklist instead of a pile of warnings ----
function showStart() {
  const c = snap.counts || {};
  return !c.done && !(snap.library || []).length && !isRunning("record");
}

function firstReady() {
  return (snap.items || []).find((i) => i.state === "waiting" || i.state === "failed");
}

// The test step ticks only when the latest test passed; activity.py keeps one
// entry per job (its last run), level "ok" for a real picture.
function lastTest() {
  return (snap.activity || []).find((a) => a && a.job === "test") || null;
}

function renderStart() {
  $("start").hidden = !showStart();
  if ($("start").hidden) return;
  const c = snap.counts || {}, h = snap.health || {};
  const ready = firstReady();
  const loading = isRunning("queue"), testing = isRunning("test"), opt = isRunning("optimize");
  const test = lastTest();
  const testHint = "Plays the first video for 25 seconds, records it and checks the picture is not black.";
  const steps = [
    { done: !!h.browser, title: "Open the playcap browser",
      hint: "A separate Chrome window that playcap controls. If your site needs an account, log in there once. playcap never sees your password.",
      btn: ["Open browser", () => job("start", "browser")] },
    { done: c.total > 0, title: "Load the queue",
      hint: snap.queue_offline ? "playcap reads your list of links."
        : "playcap reads the list of videos from your site. Log in first if it needs it.",
      btn: [loading ? "Loading…" : "Load queue", () => job("start", "queue"), loading] },
    { done: !!test && test.level === "ok", title: "Try one item (optional)",
      hint: test && test.level !== "ok" && !testing ? `Last try: ${test.text}` : testHint,
      btn: [testing ? "Testing…" : "Test 25 s", () => ready && job("start", "test", { item: ready.id }),
        testing || !ready || opt] },
    { done: false, title: "Start recording",
      hint: "Each video plays to the end, is recorded, and is filed in your library. You can leave it running; a rerun picks up where it stopped.",
      btn: ["Start recording", () => job("start", "record"), testing || !ready || opt, "primary"] },
  ];
  // ready.id is captured by the Test button's closure, so it is part of the key.
  const key = steps.map((st) => [st.done, st.title, st.hint, st.btn[0], !!st.btn[2], st.btn[3] || ""]);
  if (!changed("start", [key, ready && ready.id])) return;
  $("start-steps").replaceChildren(...steps.map((st) => el("li", { class: st.done ? "done" : null },
    el("div", {}, el("strong", { text: st.title }), el("div", { class: "muted small", text: st.hint })),
    st.done ? null : el("button", { class: "btn small " + (st.btn[3] || ""), text: st.btn[0],
      disabled: !!st.btn[2], onclick: st.btn[1] }))));
}

function noteTestFinished() {
  const running = isRunning("test");
  if (testWasRunning && !running) {
    const res = lastTest();
    toast(res ? res.text : "Test finished. See What happened.", !res || res.level !== "bad");
  }
  testWasRunning = running;
}

function stoppingTooLong(name) {
  const j = (snap.jobs || {})[name] || {};
  if (!j.running || !j.stopping) { delete stopSeen[name]; return false; }
  stopSeen[name] = stopSeen[name] || Date.now();
  return Date.now() - stopSeen[name] > FORCE_AFTER_MS;
}

function renderControls() {
  const rec = isRunning("record"), opt = isRunning("optimize"), testing = isRunning("test");
  const jobs = snap.jobs || {};
  const recJob = jobs.record || {}, optJob = jobs.optimize || {};
  const c = snap.counts || {};
  const toRecord = (c.waiting || 0) + (c.failed || 0);
  const external = snap.external || {};

  $("rec-start").disabled = rec || opt || testing || toRecord === 0;
  $("rec-start").textContent = rec ? "Recording…" : "Start recording";
  $("rec-stop-after").hidden = !rec || recJob.stopping_after || recJob.stopping;
  $("rec-stop-now").hidden = !rec || recJob.stopping;
  $("rec-kill").hidden = !stoppingTooLong("record");

  let note = "";
  if (opt) note = "Stop re-compressing before recording — both at once drops frames.";
  else if (recJob.stopping) note = "Stopping… the current recording is being closed cleanly.";
  else if (recJob.stopping_after) note = "Will stop when the current item finishes.";
  else if (rec && external.record) note = "Recording was started outside this window.";
  else if (testing) note = "A 25 s test is running. Recording can start once it finishes.";
  else if (!rec) note = toRecord ? readyNote(toRecord) : nothingReadyNote(c);
  $("rec-note").textContent = note;

  $("opt-start").disabled = rec || opt;
  $("opt-start").textContent = opt ? "Re-compressing…" : "Re-compress library";
  $("opt-stop").hidden = !opt || optJob.stopping;
  $("opt-kill").hidden = !stoppingTooLong("optimize");
  $("opt-note").textContent = optJob.stopping ? "Stopping after ffmpeg exits — the file in progress is discarded, the original is untouched."
    : (opt && external.optimize ? "Re-compressing was started outside this window." :
      "Optional: shrinks recordings made at a fixed bitrate (about 1.3x). Files recorded in a quality mode are skipped. Originals are kept until each new file is checked.");

  $("browser-start").disabled = !!(snap.health || {}).browser;
  $("browser-start").textContent = (snap.health || {}).browser ? "Browser open" : "Open browser";
  $("queue-start").disabled = rec || isRunning("queue");
  $("queue-start").textContent = isRunning("queue") ? "Refreshing…" : "Refresh queue";
}

function readyNote(n) {
  const eta = snap.avg_item_minutes ? ` · roughly ${hours(n * snap.avg_item_minutes)} in all` : "";
  return `${n} item${n > 1 ? "s" : ""} ready to record${eta}.`;
}

function nothingReadyNote(c) {
  if (!c.total) return "Load the queue first.";
  const why = [];
  if (c.not_aired) why.push(`${c.not_aired} not aired yet`);
  if (c.locked) why.push(`${c.locked} locked`);
  if (c.skipped) why.push(`${c.skipped} skipped`);
  if (!why.length) return "Everything in the queue is recorded.";
  return `Nothing ready to record: ${why.join(", ")}.` +
    (c.not_aired ? " Not-aired items become ready two hours after they air." : "");
}

function renderNow() {
  const n = snap.now;
  $("now").hidden = !n;
  if (n) {
    $("now-title").textContent = n.title || "";
    const pct = n.duration ? Math.min(100, (100 * (n.t || 0)) / n.duration) : 0;
    $("now-bar").style.width = pct.toFixed(1) + "%";
    const res = n.w && n.h ? ` · ${n.w}×${n.h}` : "";
    const place = n.n && n.of > 1 ? `Item ${n.n} of ${n.of} · ` : "";
    $("now-meta").textContent = place + `${clock(n.t)} of ${clock(n.duration)} (${pct.toFixed(0)}%)` +
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
  const items = (snap.items || []).filter((i) => i && typeof i === "object");
  const rec = isRunning("record"), optRunning = isRunning("optimize"), testRunning = isRunning("test");
  const nowId = snap.now && snap.now.id;
  // Everything the rows and their buttons are drawn from (see changed()).
  if (!changed("queue", [items, queueFilter, rec, optRunning, testRunning, nowId])) return;
  $("queue-tabs").replaceChildren(...TABS.map(([key, label, pred]) => {
    const n = items.filter(pred).length;
    const active = queueFilter === key;
    return el("button", { class: "tab" + (active ? " active" : ""), role: "tab",
      "aria-selected": active ? "true" : "false",
      onclick: () => {
        queueFilter = key; renderQueue();
        const now = $("queue-tabs").querySelector(".tab.active");   // keep focus on the tab row
        if (now) now.focus();
      }, text: `${label} ${n}` });
  }));
  const pred = (TABS.find((t) => t[0] === queueFilter) || TABS[0])[2];
  const rows = items.filter(pred);
  $("queue-body").replaceChildren(...rows.map((i) => {
    const recordingThis = rec && nowId === i.id;
    const actions = [];
    if (!rec && !optRunning && !testRunning && (i.state === "waiting" || i.state === "failed")) {
      actions.push(el("button", { class: "btn small", text: "Record",
        title: "Record just this one, then stop",
        onclick: () => job("start", "record", { item: i.id }) }));
      actions.push(el("button", { class: "btn small ghost", text: "Test 25 s",
        title: "Play this page for 25 s, record it, and check the picture is not black. Result under What happened.",
        onclick: () => job("start", "test", { item: i.id }) }));
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
      el("td", {}, el("div", { text: i.title || i.id || "" }),
        i.state === "failed" && i.error ? el("div", { class: "err-text", text: i.error, title: i.error_raw || "" }) : null),
      el("td", { class: "nowrap muted small", text: [i.day, i.time].filter((x) => x && x !== "-").join(" ") }),
      el("td", {}, el("span", { class: "chip " + (recordingThis ? "recording" : i.state),
        text: recordingThis ? "recording" : (CHIP[i.state] || i.state || "") })),
      el("td", { class: "actions" }, ...actions));
  }));
  const empty = rows.length === 0;
  $("queue-empty").hidden = !empty;
  $("queue-empty").textContent = items.length === 0
    ? "The queue is empty. Press “Refresh queue” to read it from your source."
    : (queueFilter === "attention" ? "Nothing waiting to be recorded." : "Nothing in this list.");
}

function renderLibrary() {
  const lib = (snap.library || []).filter((e) => e && typeof e === "object");
  const total = lib.reduce((a, e) => a + (Number(e.gb) || 0), 0);
  const shrunk = lib.filter((e) => e.optimized).length;
  $("lib-summary").textContent = lib.length
    ? `${lib.length} recording${lib.length > 1 ? "s" : ""} · ${gb(total)}` + (shrunk ? ` · ${shrunk} shrunk` : "") : "";
  $("lib-path").textContent = snap.library_dir ? "Saved in " + snap.library_dir : "";
  $("lib-empty").hidden = lib.length > 0;
  $("lib-body").closest("table").hidden = !lib.length;
  // Re-compressing is an occasional chore, so it lives here rather than
  // beside Start recording, and only once there is something to shrink.
  $("lib-opt").hidden = !lib.length && !isRunning("optimize");
  if (!changed("library", lib)) return;
  $("lib-body").replaceChildren(...lib.map((e) => el("tr", {},
    el("td", { text: e.name || "" }),
    el("td", { class: "num nowrap", text: gb(e.gb) + (e.before_gb ? `  (was ${gb(e.before_gb)})` : "") }),
    el("td", {}, el("span", { class: "chip " + (e.optimized ? "done" : ""), text: e.optimized ? "yes" : "not yet" })),
  )));
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

const MARK = { ok: "✓", bad: "!", info: "•" };
function renderActivity() {
  const list = (snap.activity || []).filter((a) => a && typeof a === "object");
  $("activity-card").hidden = !list.length;
  $("activity").replaceChildren(...list.map((a) => el("li", { class: a.level },
    el("span", { class: "mark", text: MARK[a.level] || "•" }),
    el("span", { class: "what", text: a.text || "" }),
    el("span", { class: "muted small nowrap", text: `${LOG_NAMES[a.job] || a.job} · ${ago(a.when)}` }))));
}

const LOG_NAMES = { test: "Test run", record: "Recording", optimize: "Re-compressing", queue: "Queue", browser: "Browser" };
function renderLogs() {
  if (!document.querySelector(".logs").open) return;
  const logs = snap.log || {};
  // Redrawing would reset each box's scroll position every poll.
  if (!changed("logs", Object.keys(LOG_NAMES).map((k) => logs[k] || ""))) return;
  $("logs").replaceChildren(...Object.keys(LOG_NAMES).map((k) =>
    el("div", {}, el("h4", { text: LOG_NAMES[k] }), el("pre", { text: logs[k] || "(empty)" }))));
}

function renderDash() {
  renderHealth();
  noteTestFinished();
  renderStart();
  renderProblems();
  renderControls();
  renderNow();
  renderQueue();
  renderLibrary();
  renderActivity();
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
$("banner-reload").onclick = () => location.reload();
$("lib-open").onclick = () => act("/api/open/library", {});
document.querySelector(".logs").addEventListener("toggle", () => snap && renderLogs());

// ---------------------------------------------------------------- wizard
const TOOL_LABELS = { chrome: "Chrome", obs: "OBS", ffmpeg: "ffmpeg", ffprobe: "ffprobe" };
const TOOL_KEYS = { chrome: "chrome_exe", obs: "obs_exe", ffmpeg: "ffmpeg", ffprobe: "ffprobe" };
const TOOL_HELP = {   // what each one is for, and where to get it when it is missing
  chrome: ["plays the video pages", "https://www.google.com/chrome/"],
  obs: ["records the screen", "https://obsproject.com/download"],
  ffmpeg: ["checks and shrinks finished recordings", "https://ffmpeg.org/download.html"],
  ffprobe: ["reads each recording's length (comes with ffmpeg)", "https://ffmpeg.org/download.html"],
};

// /api/setup with every field the wizard reads filled in, so a server error
// (or an older server) shows a message instead of a half-drawn wizard.
async function loadSetup() {
  let r;
  try {
    r = await api("/api/setup");
  } catch (e) {
    r = { ok: false, message: "the playcap UI server is not responding." };
  }
  if (r.ok === false) showBanner("setup", "Could not read the setup details: " + (r.message || "unknown error."));
  else clearBanner("setup");
  const out = { ...r };
  for (const k of ["config", "tools", "recording"]) {
    if (!out[k] || typeof out[k] !== "object") out[k] = {};
  }
  if (!Array.isArray(out.adapters)) out.adapters = [];
  return out;
}

async function openWizard(cancellable) {
  $("dash").hidden = true;
  $("wizard").hidden = false;
  $("settings-btn").hidden = true;
  $("wiz-cancel").hidden = !cancellable;
  setup = await loadSetup();
  renderTools();
  renderInstall(true);
  renderObsStatus();
  renderCapture();
  const cfg = setup.config || {};
  chosenSource = cfg.adapter || (setup.adapters[0] && setup.adapters[0].module);
  renderSources();
  $("output_dir").value = cfg.output_dir || (setup.root ? setup.root.replace(/[\\/]+$/, "") + (setup.root.includes("\\") ? "\\" : "/") + "recordings" : "");
  $("show").value = cfg.show || "My Recordings";
  renderOutputFull();
  renderQuality();
  renderLayout();
  showStep(1);
}

// A relative folder is relative to the playcap folder; say where that is.
function renderOutputFull() {
  const v = $("output_dir").value.trim();
  const root = (setup.root || "").replace(/[\\/]+$/, "");
  const sep = root.includes("\\") ? "\\" : "/";
  const absolute = /^([a-zA-Z]:)?[\\/]/.test(v);
  $("output-full").textContent = !v || absolute || !root ? ""
    : "Full path: " + root + sep + v.replace(/[\\/]+/g, sep);
}
$("output_dir").addEventListener("input", () => { if (setup) renderOutputFull(); });

// A row of radio cards. Built once per wizard opening; a change only moves the
// .selected class, because rebuilding the radios would drop keyboard focus in
// the middle of arrow-key navigation.
function radioCards(container, name, choices, current, onPick) {
  container.replaceChildren(...choices.map(([key, label, help]) => {
    const radio = el("input", { type: "radio", name, value: key, checked: key === current });
    radio.addEventListener("change", () => { markSelected(container); onPick(key); });
    return el("label", { class: "source" + (key === current ? " selected" : "") },
      radio, el("strong", { text: label }), help ? el("small", { class: "muted", text: " " + help }) : null);
  }));
}
function markSelected(container) {
  container.querySelectorAll("label.source").forEach((l) => {
    const r = l.querySelector("input");
    l.classList.toggle("selected", !!(r && r.checked));
  });
}

// ---- file names (organize.py holds the rules) ----
const LAYOUT_CHOICES = [
  ["folder", "Folder", "{show}/{n:02} - {title}", "One folder, numbered files. Works everywhere."],
  ["media_server", "Media server", "{show}/Season {season:02}/S{season:02}E{n:02} - {title}",
   "TV-series layout with .nfo files, for Jellyfin, Emby, Plex or Kodi."],
  ["custom", "Custom", null, "Your own template."],
];
let layoutChoice = "folder";

function previewName(template) {
  const show = $("show").value.trim() || "My Recordings";
  const v = { show, season: 1, n: 3, episode: 3, title: "Product webinar, part 1",
              date: "2026-03-14", time: "14-00", kind: "video", id: "a1b2c3" };
  const out = template.replace(/\{(\w+)(?::0?(\d+))?\}/g, (m, k, w) => {
    if (!(k in v)) return m;
    const val = String(v[k]);
    return w ? val.padStart(Number(w), "0") : val;
  });
  return out.split("/").map((p) => p.replace(/[<>:"\\|?*]/g, "-").trim()).filter(Boolean).join(" / ") + ".mp4";
}

function currentTemplate() {
  if (layoutChoice === "custom") return $("name_template").value.trim() || "{show}/{n:02} - {title}";
  return LAYOUT_CHOICES.find((c) => c[0] === layoutChoice)[2];
}

function renderLayout() {
  const cfg = setup.config || {};
  if (!setup._layoutFilled) {
    layoutChoice = cfg.library_layout || "folder";
    $("name_template").value = cfg.name_template || "";
    $("write_nfo").checked = cfg.write_nfo === true || (cfg.write_nfo == null && layoutChoice === "media_server");
    setup._layoutFilled = true;
  }
  radioCards($("layouts"), "layout", LAYOUT_CHOICES.map(([key, label, , help]) => [key, label, help]),
    layoutChoice, (key) => {
      layoutChoice = key;
      $("write_nfo").checked = key === "media_server";
      updateLayout();
    });
  updateLayout();
}
// The parts of the File names section that follow the choice and the inputs.
function updateLayout() {
  $("template-field").hidden = layoutChoice !== "custom";
  $("name-preview").textContent = previewName(currentTemplate());
}
$("name_template").addEventListener("input", () => updateLayout());
$("show").addEventListener("input", () => { if (setup) updateLayout(); });

function collectLayout() {
  const out = { library_layout: layoutChoice, write_nfo: $("write_nfo").checked };
  if (layoutChoice === "custom") out.name_template = $("name_template").value.trim();
  return out;
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
  radioCards($("quality"), "quality", choices, qualityChoice, (key) => {
    qualityChoice = key; setup._qualityTouched = true;
    const p = (rec.presets || {})[key];
    if (p) $("record_crf").value = p.record_crf;
  });
  if (!setup._qualityFilled) {
    $("record_crf").value = v.record_crf ?? 24;
    $("video_bitrate_kbps").value = v.video_bitrate_kbps ?? 2500;
    $("keyframe_seconds").value = v.keyframe_seconds ?? 2;
    // A config value outside the known list (hand-edited, or from a newer
    // playcap) is kept as an option, so saving does not quietly change it.
    const current = v.x264_preset || "veryfast";
    const presets = (Array.isArray(rec.x264_presets) && rec.x264_presets.length
      ? rec.x264_presets : ["veryfast"]).slice();
    if (!presets.includes(current)) presets.push(current);
    $("x264_preset").replaceChildren(...presets.map((p) =>
      el("option", { value: p, text: p, selected: p === current })));
    setup._qualityFilled = true;
  }
  $("quality-obs").textContent = rec.obs_in_sync
    ? "OBS is set to this already."
    : "OBS picks this up the next time playcap starts it: close OBS, then press Launch OBS (step 1).";
}

function collectQuality() {
  const out = {
    record_mode: qualityChoice === "bitrate" ? "bitrate" : "quality",
    x264_preset: $("x264_preset").value,
  };
  // An emptied number box is left out (the server keeps the saved value)
  // rather than sent as Number("") === 0, which would fail or mislead.
  for (const key of ["record_crf", "video_bitrate_kbps", "keyframe_seconds"]) {
    const raw = $(key).value.trim();
    if (raw !== "") out[key] = Number(raw);
  }
  return out;
}

function renderTools() {
  $("tools").replaceChildren(...Object.entries(setup.tools).map(([name, t]) => {
    t = t || {};
    const label = TOOL_LABELS[name] || name;
    const input = el("input", { type: "text", id: "tool-" + name, spellcheck: "false",
      placeholder: "not found — browse to it", "aria-label": label + " program path" });
    input.value = t.path || "";
    const [what, url] = TOOL_HELP[name] || ["", ""];
    const help = t.ok ? el("small", { class: "muted", text: "It " + what + "." })
      : el("small", { class: "muted" }, `Not found. It ${what}. `,
        el("a", { href: url, target: "_blank", rel: "noopener", text: "Download it" }),
        ", install it, then press Browse… or reopen this page.");
    return el("div", { class: "tool" },
      el("span", { class: "name", text: label }),
      el("span", { class: "mark " + (t.ok ? "ok" : "bad"), text: t.ok ? "✓" : "✗" }),
      el("div", {}, input, help, el("small", { class: "err", "data-err": TOOL_KEYS[name] })),
      el("button", { class: "btn", "data-browse": "tool-" + name, "data-kind": "file",
        "aria-label": "Browse for " + label, text: "Browse…" }));
  }));
}

function renderObsStatus() {
  const o = setup.obs;
  const obsOk = !!(setup.tools.obs && setup.tools.obs.ok);
  let text;
  if (!obsOk) text = "OBS is not installed. Get it free from obsproject.com, then reopen this page.";
  else if (!o) text = "OBS has not been started with its websocket yet. Press Launch OBS — playcap turns it on.";
  else if (!o.enabled) text = setup.obs_running
    ? "OBS's websocket server is off. Close OBS, then press Launch OBS — playcap turns it on."
    : "OBS's websocket server is off. Press Launch OBS — playcap turns it on.";
  else text = setup.obs_running ? "OBS is running and reachable. Press “Set up recording scene” once."
    : "OBS is ready. Launch it, then press “Set up recording scene” once.";
  $("obs-status").textContent = text;
  $("obs-launch").disabled = !obsOk;
}

// Refresh only the OBS facts: the wizard's own fill-once flags (_layoutFilled,
// _qualityFilled) live on `setup` and must survive.
async function refreshObs() {
  const fresh = await loadSetup();
  Object.assign(setup, { tools: fresh.tools, obs: fresh.obs, obs_running: fresh.obs_running });
  renderObsStatus();
}
// ---- screen recorder (capture.py holds the rules; detect.capture_backends reports) ----
let captureChoice = null;

function renderCapture() {
  const c = setup.capture || {};
  const ff = c.ffmpeg || {};
  const cfg = setup.config || {};
  if (!setup._captureFilled) captureChoice = cfg.capture_backend || c.backend || "obs";
  const ffHelp = ff.ok
    ? `No OBS needed. Captures with ${ff.grabber}${ff.why ? " (" + ff.why + ")" : ""}.`
    : "Not usable here: " + (ff.why || "ffmpeg was not checked.");
  radioCards($("capture"), "capture", [
    ["obs", "OBS", "Recommended. Records the screen and the computer's sound."],
    ["ffmpeg", "ffmpeg only", ffHelp],
  ], captureChoice, (key) => { captureChoice = key; renderCaptureDetails(); });
  if (!setup._captureFilled) {
    // auto, none, then every sound device ffmpeg lists; a saved device name
    // that is not plugged in right now is kept, so saving does not drop it.
    const current = cfg.capture_audio || "auto";
    const devices = Array.isArray(ff.audio_devices) ? ff.audio_devices : [];
    const opts = [["auto", ff.loopback ? `Automatic (${ff.loopback})` : "Automatic (none found: picture only)"],
      ["none", "No sound"], ...devices.map((d) => [d, d])];
    if (!opts.some(([v]) => v === current)) opts.push([current, current]);
    $("capture_audio").replaceChildren(...opts.map(([v, t]) =>
      el("option", { value: v, text: t, selected: v === current })));
    setup._captureFilled = true;
  }
  renderCaptureDetails();
}

function renderCaptureDetails() {
  const ff = (setup.capture || {}).ffmpeg || {};
  $("capture-audio-field").hidden = captureChoice !== "ffmpeg";
  $("capture-status").textContent = captureChoice !== "ffmpeg" ? ""
    : (ff.ok ? `ffmpeg can record here (${ff.grabber}; encoders: ${(ff.encoders || []).join(", ") || "none"}).`
      : "ffmpeg cannot record here: " + (ff.why || "unknown reason") + " Pick OBS, or install a newer ffmpeg.");
}

function collectCapture() {
  const out = { capture_backend: captureChoice || "obs" };
  const audio = $("capture_audio").value;
  if (audio) out.capture_audio = audio;
  return out;
}

$("obs-launch").onclick = () => act("/api/obs/launch", {}, () => setTimeout(refreshObs, 4000));
$("obs-setup").onclick = () => act("/api/obs/setup", {}, refreshObs);

function renderSources() {
  radioCards($("sources"), "source", setup.adapters.map((a) => [a.module, a.label, null]),
    chosenSource, (key) => { chosenSource = key; renderSourceFields(); });
  renderSourceFields();
}

// Only the fields under the radios follow the chosen source.
function renderSourceFields() {
  const a = setup.adapters.find((x) => x.module === chosenSource);
  const cfg = setup.config || {};
  $("source-fields").replaceChildren(...(a && Array.isArray(a.fields) ? a.fields : []).map((f) => {
    const id = "field-" + f.key;
    const input = f.kind === "links"
      ? el("textarea", { id, spellcheck: "false", placeholder: "https://example.com/video-page\nIntro | https://example.com/another" })
      : el("input", { id, type: f.kind === "url" ? "url" : "text", spellcheck: "false" });
    input.value = f.kind === "links" ? (setup.links || "") : (cfg[f.key] || "");
    // The bundled demo has its own button (Try it now, playcap.demo), which
    // works from any folder; pointing here at a checkout path would not.
    const demo = f.kind === "links" ? el("small", { class: "muted",
      text: "Just trying playcap? Use “Try it now” at the top of the page: it records a six-second " +
        "test video that ships with playcap, into its own demo folder." }) : null;
    return el("label", { class: "field" }, el("span", { text: f.label }), input,
      f.help ? el("small", { class: "muted", text: f.help }) : null, demo,
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
  for (const f of a && Array.isArray(a.fields) ? a.fields : []) {
    const input = $("field-" + f.key);
    if (input) out[f.key] = input.value;
  }
  out.output_dir = $("output_dir").value.trim();
  out.show = $("show").value.trim();
  Object.assign(out, collectQuality(), collectLayout(), collectCapture());
  return out;
}

const STEP_OF = { chrome_exe: 1, obs_exe: 1, ffmpeg: 1, ffprobe: 1, adapter: 2, output_dir: 3, show: 3,
  library_layout: 3, name_template: 3, write_nfo: 3,
  record_mode: 3, record_crf: 3, video_bitrate_kbps: 3, x264_preset: 3, keyframe_seconds: 3,
  capture_backend: 1, capture_audio: 1 };

// Errors with no field of their own ("_file" for an unreadable config.json,
// "adapter", keys the server refuses) are named here, so none goes unseen.
const ERR_NAMES = { _file: "config.json", adapter: "Source" };

async function finish() {
  document.querySelectorAll("[data-err]").forEach((e) => { e.textContent = ""; });
  clearBanner("save");
  let r;
  try {
    r = await api("/api/config", collect());
  } catch (e) {
    toast("The playcap UI server is not responding.", false);
    return;
  }
  if (!r.ok) {
    const errors = r.errors && typeof r.errors === "object" ? r.errors : {};
    const loose = [];
    let first = 3;
    for (const [k, msg] of Object.entries(errors)) {
      const slot = [...document.querySelectorAll("[data-err]")].find((s) => s.dataset.err === k);
      if (slot) {
        slot.textContent = msg;
        const panel = slot.closest("details");   // e.g. the collapsed Advanced quality panel
        if (panel) panel.open = true;
      } else {
        loose.push(`${ERR_NAMES[k] || k}: ${msg}`);
      }
      first = Math.min(first, STEP_OF[k] || 2);
    }
    if (loose.length) showBanner("save", "Settings not saved. " + loose.join(" · "));
    if (!Object.keys(errors).length) toast(r.message || "Could not save settings.", false);
    showStep(first);
    return;
  }
  toast("Settings saved.");
  $("wizard").hidden = true;
  await refresh();
  // A list of links needs no browser to read, so bring the queue in line with
  // what was just saved rather than leaving the user to find "Refresh queue".
  if (snap && snap.queue_offline && !isRunning("record") && !isRunning("queue")) job("start", "queue");
}

document.querySelectorAll(".stepper li").forEach((li) => {
  const go = () => showStep(Number(li.dataset.step));
  li.addEventListener("click", go);
  li.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
  });
});

$("wiz-next").onclick = () => (wizStep < 3 ? showStep(wizStep + 1) : finish());
$("wiz-back").onclick = () => showStep(Math.max(1, wizStep - 1));
$("wiz-cancel").onclick = () => { $("wizard").hidden = true; clearBanner("save"); refresh(); };

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-browse]");
  if (!b) return;
  ev.preventDefault();
  b.disabled = true;
  try {
    const r = await api("/api/browse", { kind: b.dataset.kind });
    if (r.path) {
      $(b.dataset.browse).value = r.path;
      if (b.dataset.browse === "output_dir") renderOutputFull();
    } else if (r.ok === false && r.message) {
      toast(r.message, false);
    }
  } catch (e) {
    toast("The playcap UI server is not responding.", false);
  } finally {
    b.disabled = false;
  }
});


// ---------------------------------------------------------------- try it now + installs
// A first run starts with the bundled demo (playcap.demo): one button sets up
// the browser (and OBS, unless the demo records with ffmpeg: demo.choose_backend)
// and records six seconds into <root>/playcap-demo. Missing tools get an
// "Install with winget" button where winget exists (playcap.install; the request
// names the tool, never a command), else the download link.
let demoInfo = null;
let installInfo = null;
let installWasRunning = false;
let demoWasRunning = false;
const NEED_TEXT = {
  chrome: "Chrome plays the video page.",
  obs: "OBS records the screen. ffmpeg alone works too: install either one.",
  ffmpeg: "ffmpeg records the screen (it is the screen recorder chosen in Settings).",
};

function tryItWanted() {
  if (!snap) return false;
  if (demoInfo && demoInfo.running) return true;
  const c = snap.counts || {};
  return !snap.configured || (!c.total && !(snap.library || []).length && !isRunning("record"));
}

async function firstRunTick() {
  const wizardOpen = !$("wizard").hidden;
  const want = tryItWanted();
  if (!want && !wizardOpen && !installWasRunning && !demoWasRunning) {
    $("tryit").hidden = true;
    return;
  }
  try {
    const [d, i] = await Promise.all([api("/api/demo"), api("/api/install")]);
    if (d && d.phase) demoInfo = d;
    if (i && i.packages) installInfo = i;
  } catch (e) {
    return;
  }
  const running = !!(installInfo && installInfo.running);
  if (installWasRunning && !running) await installFinished();
  installWasRunning = running;
  const demoRunning = !!(demoInfo && demoInfo.running);
  if (demoWasRunning && !demoRunning && demoInfo) toast(demoInfo.message || "The demo finished.", demoInfo.phase === "done");
  demoWasRunning = demoRunning;
  renderTryIt();
  if (wizardOpen) renderInstall(false);
}

// After winget exits, detect again: the Tools rows and the OBS box follow.
async function installFinished() {
  toast("Install finished. Checking for the programs again.");
  if ($("wizard").hidden || !setup) return;
  const fresh = await loadSetup();
  Object.assign(setup, { tools: fresh.tools, obs: fresh.obs, obs_running: fresh.obs_running });
  renderTools();
  renderInstall(true);
  renderObsStatus();
}

function installButton(tool) {
  const running = !!(installInfo && installInfo.running);
  return el("button", { class: "btn small install", text: running ? "Installing…" : "Install with winget",
    disabled: running, title: (installInfo.packages || {})[tool] || "",
    onclick: () => act("/api/install", { tool }, () => { installWasRunning = true; }) });
}

function setInstallLog(id) {
  const log = installInfo && installInfo.log;
  const show = !!(installInfo && log && (installInfo.running || installWasRunning));
  $(id).hidden = !show;
  if (show && $(id).textContent !== log) {
    $(id).textContent = log;
    $(id).scrollTop = $(id).scrollHeight;
  }
}

// Decorates the rows renderTools() drew; `fresh` after they were rebuilt.
function renderInstall(fresh) {
  if (!setup || !setup.tools) return;
  if (!installInfo) { if (fresh) firstRunTick(); return; }
  if (fresh) delete lastRender.install;
  if (changed("install", [installInfo.winget, installInfo.running, setup.tools])) {
    for (const [name, t] of Object.entries(setup.tools)) {
      const input = $("tool-" + name);
      if (!input) continue;
      const box = input.parentElement;
      box.querySelectorAll(".install").forEach((b) => b.remove());
      if (!(t && t.ok) && installInfo.winget && (installInfo.packages || {})[name]) box.append(installButton(name));
    }
  }
  setInstallLog("install-log");
}

function renderTryIt() {
  const show = tryItWanted() || !!(demoInfo && demoInfo.phase === "done" && !(snap && snap.configured));
  $("tryit").hidden = !show || !demoInfo;
  if ($("tryit").hidden) return;
  const d = demoInfo;
  const needs = Array.isArray(d.needs) ? d.needs : [];
  const winget = !!(installInfo && installInfo.winget);
  const instRunning = !!(installInfo && installInfo.running);
  if (changed("tryit-needs", [needs, winget, instRunning])) {
    $("tryit-needs").replaceChildren(...needs.map((tool) => {
      const [, url] = TOOL_HELP[tool] || ["", ""];
      return el("li", {}, el("span", { text: `${TOOL_LABELS[tool] || tool} is not installed. ${NEED_TEXT[tool] || ""} ` }),
        winget ? installButton(tool)
          : el("a", { href: url, target: "_blank", rel: "noopener", text: "Download it" }));
    }));
  }
  const start = $("tryit-start");
  start.disabled = d.running || needs.length > 0 || instRunning;
  start.textContent = d.running ? "Running…" : (d.phase === "done" ? "Try it again" : "Try it now");
  $("tryit-stop").hidden = !d.running;
  $("tryit-open").hidden = d.phase !== "done";
  let text = d.message || "";
  if (d.now && d.now.duration) text += ` ${clock(d.now.t)} of ${clock(d.now.duration)}.`;
  if (needs.length && !d.running) text = "Install what is missing above, then press Try it now.";
  if (d.error) text += " " + d.error;
  if ($("tryit-status").textContent !== text) $("tryit-status").textContent = text;
  $("tryit-file").textContent = d.phase === "done" && d.file ? "Saved as " + d.file : "";
  setInstallLog("tryit-install-log");
}

$("tryit-start").onclick = () => act("/api/demo/start", {}, (r) => { if (r.ok) demoWasRunning = true; });
$("tryit-stop").onclick = () => act("/api/demo/stop", {});
$("tryit-open").onclick = () => act("/api/demo/open", {});

// ---------------------------------------------------------------- notifications & automation
// notify.py / schedule.py hold the rules. Secrets never come back from the
// server: a saved one shows as a placeholder; blank keeps it, Clear removes it.
const AUTO_TEXT = ["notify_ntfy_server", "jellyfin_url", "plex_url", "plex_section_id", "schedule_time"];
const AUTO_NUM = ["library_refresh_minutes", "schedule_every_hours"];
const AUTO_SECRET = ["notify_ntfy_topic", "notify_ntfy_token", "notify_discord_webhook",
  "notify_webhook_url", "jellyfin_api_key", "plex_token"];
const EVENT_LABELS = { recorded: "Item recorded", failed: "Item failed", blocked: "Capture blocked or black",
  finished: "Run finished" };
let autoInfo = null;
const autoClear = new Set();       // secrets the user asked to remove

async function loadAutomation() {
  let r;
  try { r = await api("/api/automation"); } catch (e) { return; }
  if (!r || !r.settings) { toast(r && r.message ? r.message : "Could not read the automation settings.", false); return; }
  autoInfo = r;
  autoClear.clear();
  const s = r.settings;
  for (const k of AUTO_TEXT) $(k).value = s[k] == null ? "" : String(s[k]);
  for (const k of AUTO_NUM) $(k).value = s[k] == null ? "" : String(s[k]);
  $("notify_desktop").checked = s.notify_desktop === true;
  $("schedule_mode").value = s.schedule_mode || "off";
  for (const k of AUTO_SECRET) drawSecret(k, !!s["has_" + k]);
  const on = new Set(Array.isArray(s.notify_events) ? s.notify_events : []);
  $("auto-events").replaceChildren(...(r.events || []).map((ev) => el("label", { class: "check" },
    el("input", { type: "checkbox", "data-event": ev, checked: on.has(ev) }), " " + (EVENT_LABELS[ev] || ev))));
  drawSchedule();
}

function drawSecret(k, saved) {
  const input = $(k);
  input.value = "";
  input.placeholder = autoClear.has(k) ? "will be removed when you save" : (saved ? "saved (leave blank to keep)" : "");
  const old = input.parentNode.querySelector("button[data-clear]");
  if (old) old.remove();
  if (saved && !autoClear.has(k)) {
    input.after(el("button", { class: "btn small ghost", "data-clear": k, text: "Clear",
      onclick: (ev) => { ev.preventDefault(); autoClear.add(k); drawSecret(k, true); } }));
  }
}

function drawSchedule() {
  const sc = (autoInfo && autoInfo.schedule) || {};
  const task = sc.task || {};
  const mode = $("schedule_mode").value;
  $("schedule_every_hours").closest("label").hidden = mode !== "every";
  $("schedule_time").closest("label").hidden = mode === "off";
  let text = sc.next ? `Saved schedule: ${sc.describe}; next due ${sc.next}.` : "No schedule saved.";
  if (task.supported === false) text += " Scheduled runs need Windows; elsewhere run “python -m playcap schedule”.";
  else if (task.registered) text += ` Windows will run it (next: ${task.next_run || "unknown"}).`;
  else if (sc.next) text += " Not handed to Windows yet: press Run on schedule.";
  $("auto-schedule").textContent = text;
  $("auto-register").hidden = task.supported === false;
  $("auto-register").textContent = task.registered ? "Update scheduled task" : "Run on schedule (Windows Task Scheduler)";
  $("auto-unregister").hidden = !task.registered;
}
$("schedule_mode").addEventListener("change", drawSchedule);

function collectAutomation() {
  const out = {};
  for (const k of AUTO_TEXT) out[k] = $(k).value.trim();
  for (const k of AUTO_NUM) { const v = $(k).value.trim(); if (v !== "") out[k] = Number(v); }
  for (const k of AUTO_SECRET) out[k] = autoClear.has(k) ? null : $(k).value.trim();
  out.notify_desktop = $("notify_desktop").checked;
  out.schedule_mode = $("schedule_mode").value;
  out.notify_events = [...document.querySelectorAll("#auto-events input[data-event]")]
    .filter((i) => i.checked).map((i) => i.dataset.event);
  if (out.schedule_mode === "off" && !out.schedule_time) delete out.schedule_time;
  return out;
}

async function saveAutomation() {
  document.querySelectorAll("[data-auto-err]").forEach((e) => { e.textContent = ""; });
  let r;
  try { r = await api("/api/automation/save", collectAutomation()); } catch (e) {
    toast("The playcap UI server is not responding.", false);
    return false;
  }
  const errors = r.errors && typeof r.errors === "object" ? r.errors : {};
  const loose = [];
  for (const [k, msg] of Object.entries(errors)) {
    const slot = document.querySelector(`[data-auto-err="${CSS.escape(k)}"]`);
    if (slot) slot.textContent = msg; else loose.push(`${k}: ${msg}`);
  }
  if (Object.keys(errors).length) {
    toast("Not saved. " + (loose.join(" · ") || "Check the marked fields."), false);
    return false;
  }
  toast(r.message || "Saved.", r.ok !== false);
  await loadAutomation();
  return r.ok !== false;
}

async function autoButton(btn, work) {
  if (busy) { toast("Still working on the last action…"); return; }
  busy = true; btn.disabled = true;
  try { await work(); } finally { busy = false; btn.disabled = false; }
}

$("auto").addEventListener("toggle", () => { if ($("auto").open) loadAutomation(); });
$("auto-save").onclick = () => autoButton($("auto-save"), saveAutomation);
// The test uses what is saved, so save first.
$("auto-test").onclick = () => autoButton($("auto-test"), async () => {
  if (!(await saveAutomation())) return;
  try {
    const r = await api("/api/automation/test", {});
    toast(r.message || "Done.", r.ok !== false);
  } catch (e) { toast("The playcap UI server is not responding.", false); }
});
$("auto-register").onclick = () => autoButton($("auto-register"), async () => {
  if (!(await saveAutomation())) return;
  try {
    const r = await api("/api/schedule/register", {});
    toast(r.message || "Done.", r.ok !== false);
  } catch (e) { toast("The playcap UI server is not responding.", false); }
  await loadAutomation();
});
$("auto-unregister").onclick = () => autoButton($("auto-unregister"), async () => {
  try {
    const r = await api("/api/schedule/unregister", {});
    toast(r.message || "Done.", r.ok !== false);
  } catch (e) { toast("The playcap UI server is not responding.", false); }
  await loadAutomation();
});

refresh();
