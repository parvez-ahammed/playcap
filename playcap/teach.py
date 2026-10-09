"""Teach playcap a page: point at the player, the play button and the item
links in the debug browser, get a recipe (playcap.recipes) back.

    start(root, url)   open url (or use the current tab) and show the picker bar
    read(root)         turn what was picked on the current page into selectors
    stop(root)         take the picker away

The picker is plain JS injected over CDP: Runtime.evaluate for the page that
is open now, Page.addScriptToEvaluateOnNewDocument so it comes back on every
page the user navigates to in that tab (index page -> video page). Chrome
drops such scripts when the CDP session that added them disconnects, so the
session is kept open here, in the UI server process, until stop() or the next
start(). It runs in the top frame only.

The bar and its highlight live in a closed shadow root, so the page's CSS
cannot restyle them and its scripts cannot reach in; all text is set with
textContent. While picking, a transparent shield covers the page and the
element under the pointer is found with elementsFromPoint, so iframes (a
cross-origin player) can be picked too and a click on a link picks it instead
of following it. Picked elements get data-playcap-pick="<role>", which is how
the selector check below knows "the element the user meant"; the overlay never
reports its own marks as selector material.

read() does the thinking in Python where it can be tested: the page sends a
description of each pick (tag, id, classes, a few attributes, position, for
it and its ancestors), playcap.recipes proposes selectors from that, and the
page then counts what each proposal matches. A player or play button must be
matched exactly; a link list must include every example and as little else
as possible. Nothing is clicked or played here: picking is read-only.

UI code must not call config.load(); the debug port comes from
state._effective(settings.read(root)).
"""
import json
import re
import threading

from playcap import cdp, recipes, settings

ROLES = ("player", "play", "links")

OVERLAY_JS = r"""
(() => {
  if (window.top !== window) return 'frame';
  if (window.__playcapTeach) { window.__playcapTeach.show(); return 'again'; }
  const LABELS = {player: 'Video player', play: 'Play button', links: 'Item links'};
  const HINTS = {
    player: 'Click the video itself.',
    play: 'Click the button that starts playback (skip this if clicking the video plays it).',
    links: 'On an index page: click two or three of the links to the videos.'};
  const picks = {player: [], play: [], links: []};
  let mode = null;

  const host = document.createElement('div');
  host.style.cssText = 'all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  const root = host.attachShadow({mode: 'closed'});
  const style = document.createElement('style');
  style.textContent = `
    .bar{position:fixed;top:8px;left:50%;transform:translateX(-50%);pointer-events:auto;
      background:#1d2433;color:#e8ecf3;font:13px system-ui,sans-serif;border-radius:8px;
      box-shadow:0 4px 18px rgba(0,0,0,.45);padding:8px 10px;display:flex;gap:6px;
      align-items:center;flex-wrap:wrap;max-width:94vw}
    .bar b{margin-right:4px}
    button{font:inherit;color:inherit;background:#2f3a50;border:1px solid #46536d;
      border-radius:6px;padding:4px 9px;cursor:pointer}
    button.on{background:#3b82f6;border-color:#3b82f6}
    .hint{flex-basis:100%;color:#aab4c5;font-size:12px}
    .shield{position:fixed;inset:0;pointer-events:auto;cursor:crosshair;background:transparent}
    .hl{position:fixed;border:2px solid #f59e0b;background:rgba(245,158,11,.15);pointer-events:none}
    .mark{position:fixed;border:2px solid #22c55e;pointer-events:none}
    .mark span{position:absolute;top:-18px;left:-2px;background:#22c55e;color:#04210f;
      font:11px system-ui,sans-serif;padding:1px 4px;border-radius:3px}`;
  root.append(style);
  const marks = document.createElement('div');
  const shield = document.createElement('div'); shield.className = 'shield'; shield.hidden = true;
  const hl = document.createElement('div'); hl.className = 'hl'; hl.hidden = true;
  const bar = document.createElement('div'); bar.className = 'bar';
  const title = document.createElement('b'); title.textContent = 'playcap: pick';
  bar.append(title);
  const buttons = {};
  for (const role of Object.keys(LABELS)) {
    const b = document.createElement('button');
    b.addEventListener('click', () => setMode(mode === role ? null : role));
    buttons[role] = b; bar.append(b);
  }
  const clear = document.createElement('button'); clear.textContent = 'Clear';
  clear.addEventListener('click', () => { for (const r of Object.keys(picks)) picks[r] = []; sync(); });
  bar.append(clear);
  const hint = document.createElement('div'); hint.className = 'hint'; bar.append(hint);
  root.append(marks, shield, hl, bar);

  function setMode(m) { mode = m; shield.hidden = !m; hl.hidden = true; sync(); }
  function sync() {
    for (const [role, b] of Object.entries(buttons)) {
      const n = picks[role].length;
      b.textContent = LABELS[role] + (n ? ' (' + n + ')' : '');
      b.className = mode === role ? 'on' : '';
    }
    hint.textContent = mode ? HINTS[mode] + ' Esc to stop picking.'
      : 'Choose what to pick, click it on the page, then press "Use my picks" in playcap.';
    document.querySelectorAll('[data-playcap-pick]').forEach(e => e.removeAttribute('data-playcap-pick'));
    for (const [role, list] of Object.entries(picks))
      for (const e of list) {
        const had = e.getAttribute('data-playcap-pick');
        e.setAttribute('data-playcap-pick', had ? had + ' ' + role : role);
      }
    drawMarks();
  }
  function drawMarks() {
    marks.replaceChildren();
    for (const [role, list] of Object.entries(picks))
      for (const e of list) {
        const r = e.getBoundingClientRect();
        const m = document.createElement('div'); m.className = 'mark';
        Object.assign(m.style, {left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px'});
        const s = document.createElement('span'); s.textContent = LABELS[role]; m.append(s);
        marks.append(m);
      }
  }
  function under(x, y) {
    return document.elementsFromPoint(x, y).find(e => e !== host && e !== document.documentElement && e !== document.body) || null;
  }
  function snap(e, role) {
    if (!e) return null;
    if (role === 'links') {
      const a = e.closest('a[href]');
      if (a) return a;
      const inner = e.querySelectorAll('a[href]');
      return inner.length === 1 ? inner[0] : null;
    }
    if (role === 'player') {
      for (let n = e, i = 0; n && i < 8; n = n.parentElement, i++)
        if (n.matches('video,iframe') || n.querySelector('video,iframe')) return n;
      return null;
    }
    return e.closest('button,[role=button],a,input') || e;
  }
  shield.addEventListener('mousemove', ev => {
    const e = snap(under(ev.clientX, ev.clientY), mode);
    if (!e) { hl.hidden = true; return; }
    const r = e.getBoundingClientRect();
    Object.assign(hl.style, {left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px'});
    hl.hidden = false;
  });
  shield.addEventListener('click', ev => {
    ev.preventDefault(); ev.stopPropagation();
    const e = snap(under(ev.clientX, ev.clientY), mode);
    if (!e) return;
    if (mode === 'links') {
      const i = picks.links.indexOf(e);
      if (i >= 0) picks.links.splice(i, 1); else picks.links.push(e);
    } else {
      picks[mode] = [e];
      setMode(null);
      return;
    }
    sync();
  }, true);
  const onKey = ev => { if (ev.key === 'Escape' && mode) setMode(null); };
  const onMove = () => drawMarks();
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('scroll', onMove, true);
  window.addEventListener('resize', onMove);

  const ATTRS = ['data-testid', 'data-test', 'data-qa', 'data-cy', 'itemprop', 'name', 'role',
                 'aria-label', 'rel', 'type'];
  function describe(e) {
    const path = [];
    for (let n = e; n && n.nodeType === 1 && path.length < 12; n = n.parentElement) {
      const attrs = {};
      for (const a of n.attributes) {
        if (a.name.startsWith('data-playcap')) continue;
        if (ATTRS.includes(a.name) || a.name.startsWith('data-')) attrs[a.name] = a.value.slice(0, 120);
      }
      const same = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [n];
      path.push({tag: n.tagName.toLowerCase(), id: n.id || '', classes: [...n.classList].slice(0, 12),
                 attrs, nth: same.indexOf(n) + 1, same: same.length});
      if (n.tagName === 'BODY') break;
    }
    return path;
  }
  window.__playcapTeach = {
    picks: () => JSON.stringify({url: location.href,
      player: picks.player.filter(e => e.isConnected).map(describe),
      play: picks.play.filter(e => e.isConnected).map(describe),
      links: picks.links.filter(e => e.isConnected).map(describe)}),
    show: () => { if (!host.isConnected) document.documentElement.append(host); },
    remove: () => {
      document.querySelectorAll('[data-playcap-pick]').forEach(e => e.removeAttribute('data-playcap-pick'));
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onMove, true);
      window.removeEventListener('resize', onMove);
      host.remove(); delete window.__playcapTeach;
    },
  };
  const mount = () => { document.documentElement.append(host); sync(); };
  if (document.documentElement) mount(); else document.addEventListener('DOMContentLoaded', mount);
  return 'ok';
})()
"""

PICKS_JS = "window.__playcapTeach ? window.__playcapTeach.picks() : null"
REMOVE_JS = "window.__playcapTeach && window.__playcapTeach.remove()"

# How many links a selector yields and the first few titles: the user checks
# these before saving. %s: links selector, JSON.
PREVIEW_LINKS_JS = r"""
JSON.stringify((() => {
  let nodes = [];
  try { nodes = [...document.querySelectorAll(%s)]; } catch (e) { return null; }
  const urls = [];
  for (const n of nodes) {
    const a = n.matches('a[href]') ? n : (n.querySelector('a[href]') || n.closest('a[href]'));
    if (a && !urls.some(u => u.url === a.href))
      urls.push({url: a.href, title: (a.textContent || a.title || '').replace(/\s+/g, ' ').trim().slice(0, 80)});
  }
  return {count: urls.length, sample: urls.slice(0, 5)};
})())
"""

_lock = threading.Lock()
_live = {"sess": None, "script": None}


def _port(root):
    from playcap import state      # deferred: state imports a lot
    try:
        cfg, _ = state._effective(settings.read(root))
        cdp.set_port(cfg.get("chrome_debug_port", 9222))
    except Exception:
        pass


def _drop():
    sess, script = _live["sess"], _live["script"]
    _live["sess"] = _live["script"] = None
    if not sess:
        return
    try:
        sess.js(REMOVE_JS, timeout=5)
        if script:
            sess.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script}, timeout=5)
    except Exception:
        pass
    sess.close()


def _open(url):
    """A new tab on url. /json/new answers with the new target itself; using
    that (rather than cdp.open_page's look-up by host) matters for file://
    pages, whose empty host would match whatever tab happens to be first."""
    import requests
    try:
        t = requests.put(f"{cdp.CDP_HTTP}/json/new?{url}", timeout=10).json()
        if isinstance(t, dict) and t.get("webSocketDebuggerUrl"):
            return t
    except Exception:
        pass
    return cdp.open_page(url)


def start(root, url=""):
    url = (url or "").strip()
    if url and not re.match(r"^(https?|file)://", url, re.I):
        return False, "Give a web address starting with https:// (or leave it empty for the current tab)."
    with _lock:
        _drop()
        _port(root)
        try:
            if url:
                target = _open(url)
            else:
                pages = [t for t in cdp.targets(("page",))
                         if not t["url"].startswith(("devtools://", "chrome-extension://"))]
                if not pages:
                    return False, "The playcap browser has no open tab. Give a page address."
                target = pages[0]
            sess = cdp.Session(target)
            res = sess.call("Page.addScriptToEvaluateOnNewDocument", {"source": OVERLAY_JS})
            _live["sess"], _live["script"] = sess, res.get("identifier")
            sess.bring_to_front()
            sess.js(OVERLAY_JS)
        except cdp.CdpError as exc:
            _drop()
            return False, ("The playcap browser is not open. Press “Open browser” first. "
                           f"({str(exc).splitlines()[0]})")
        except Exception as exc:
            _drop()
            return False, f"Could not show the picker: {exc}"
    return True, ("The pick bar is at the top of the playcap browser window. Pick, then press "
                  "“Use my picks” here.")


def stop(root):
    with _lock:
        had = _live["sess"] is not None
        _drop()
    return True, "Picker closed." if had else "The picker was not open."


def _check(sess, sels, role):
    if not sels:
        return []
    return sess.js_json(recipes.CHECK_SELECTORS_JS % (json.dumps(sels), json.dumps(role))) or []


def propose(sess, picks):
    """Selectors for what was picked on the page sess is on. -> proposal dict
    (each part None when nothing was picked or nothing fitted)."""
    url = picks.get("url") or ""
    out = {"url": url, "match_url": recipes.suggest_match(url),
           "player": None, "play_button": None, "links": None, "notes": []}
    for role, key in (("player", "player"), ("play", "play_button")):
        paths = picks.get(role) or []
        if not paths:
            continue
        sel = recipes.choose_single(_check(sess, recipes.candidates(paths[0]), role))
        if sel:
            out[key] = sel
        else:
            out["notes"].append(f"No selector matched only the picked {key.replace('_', ' ')}; "
                                "type one in by hand.")
    paths = picks.get("links") or []
    if paths:
        if len(paths) < 2:
            out["notes"].append("Pick at least two links so playcap can see what they share.")
        sel = recipes.choose_set(_check(sess, recipes.set_candidates(paths), "links"))
        if sel:
            preview = sess.js_json(PREVIEW_LINKS_JS % json.dumps(sel)) or {}
            out["links"] = {"selector": sel, "page": url, "count": preview.get("count", 0),
                            "sample": preview.get("sample", [])}
        else:
            out["notes"].append("The picked links share no selector; type one in by hand.")
    return out


def read(root):
    with _lock:
        sess = _live["sess"]
        if not sess:
            return {"ok": False, "message": "Open the picker first."}
        try:
            raw = sess.js_json(PICKS_JS)
            if raw is None:
                sess.js(OVERLAY_JS)          # a page that loaded before the script was added
                return {"ok": False, "message": "The pick bar was not on this page; it is now. Pick again."}
            proposal = propose(sess, raw)
        except Exception as exc:
            _drop()
            return {"ok": False, "message": f"Lost the playcap browser tab ({exc}). Open the picker again."}
    if not (proposal["player"] or proposal["play_button"] or proposal["links"]):
        return {"ok": False, "message": " ".join(proposal["notes"]) or
                "Nothing picked yet on this page.", "proposal": proposal}
    return {"ok": True, "message": "Picks read.", "proposal": proposal}
