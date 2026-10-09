"use strict";
// Recipes card: teach playcap a page, list / edit / export / import recipes.
// Loaded after app.js and uses its helpers ($, el, api, toast). Like the rest
// of the UI, everything is inserted with textContent (el's "text"), never
// innerHTML: recipe names and link titles come from other people's files and
// pages.

let recipeList = [];
let editingName = null;          // the recipe being edited (its old name), or null
let lastIndexUrl = "";           // index page of the last links pick

const RF = ["name", "match", "player", "play", "has", "cpage", "links", "regex", "title", "next"];

function rfClear() {
  for (const k of RF) $("rf-" + k).value = "";
  $("rf-pages").value = "5";
  $("rf-reverse").checked = false;
  $("teach-preview").replaceChildren();
  $("recipe-add-collect").hidden = true;
  editingName = null;
  lastIndexUrl = "";
}

function rfFill(r) {
  rfClear();
  $("rf-name").value = r.name || "";
  $("rf-match").value = (r.match && r.match.url) || "";
  $("rf-has").value = (r.match && r.match.page_has) || "";
  $("rf-player").value = r.player || "";
  $("rf-play").value = r.play_button || "";
  const c = r.collect;
  if (c) {
    $("rf-cpage").value = c.page || "";
    $("rf-links").value = c.links || "";
    $("rf-regex").value = c.url_regex || "";
    $("rf-title").value = c.title || "";
    $("rf-next").value = c.next || "";
    $("rf-pages").value = String(c.max_pages || 5);
    $("rf-reverse").checked = !!c.reverse;
  }
  editingName = r.name || null;
}

function rfRecipe() {
  const v = (k) => $("rf-" + k).value.trim();
  const recipe = { playcap_recipe: 1, name: v("name"), match: { url: v("match"), page_has: v("has") },
    player: v("player"), play_button: v("play") };
  if (v("cpage") || v("links") || v("regex")) {
    recipe.collect = { page: v("cpage"), links: v("links"), url_regex: v("regex"), title: v("title"),
      next: v("next"), max_pages: parseInt($("rf-pages").value, 10) || 1, reverse: $("rf-reverse").checked };
  }
  return recipe;
}

function slug(name) {
  return (name || "recipe").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "recipe";
}

function exportRecipe(r) {
  const blob = new Blob([JSON.stringify(r, null, 2) + "\n"], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: slug(r.name) + ".playcap-recipe.json" });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function renderRecipes() {
  $("recipe-empty").hidden = recipeList.length > 0;
  $("recipe-body").replaceChildren(...recipeList.map((r) => {
    const what = [r.player ? "player" : null, r.play_button ? "play button" : null,
      r.collect ? "index page" : null].filter(Boolean).join(", ");
    return el("tr", {},
      el("td", {}, el("div", { text: r.name }),
        el("small", { class: "muted path", text: (r.match && r.match.url) || "" })),
      el("td", { class: "muted small", text: what }),
      el("td", { class: "nowrap" },
        el("button", { class: "btn small", text: "Edit", onclick: () => { rfFill(r); $("teach").hidden = false; } }),
        el("button", { class: "btn small", text: "Export", onclick: () => exportRecipe(r) }),
        el("button", { class: "btn small danger", text: "Delete", onclick: () => deleteRecipe(r.name) })));
  }));
}

async function loadRecipes() {
  try {
    const r = await api("/api/recipes");
    recipeList = Array.isArray(r.recipes) ? r.recipes : [];
  } catch (e) {
    recipeList = [];
  }
  renderRecipes();
}

async function post(path, body) {
  try {
    return await api(path, body);
  } catch (e) {
    return { ok: false, message: "The playcap UI server is not responding." };
  }
}

async function deleteRecipe(name) {
  if (!confirm(`Delete the recipe “${name}”?`)) return;
  const r = await post("/api/recipes/delete", { name });
  toast(r.message || "Done.", r.ok !== false);
  loadRecipes();
}

function showPreview(p) {
  const lines = [];
  if (p.links) {
    lines.push(`${p.links.count} links match “${p.links.selector}”. First ones:`);
    for (const s of p.links.sample || []) lines.push("• " + (s.title || s.url));
  }
  for (const n of p.notes || []) lines.push(n);
  $("teach-preview").replaceChildren(...lines.map((t) => el("li", { text: t })));
}

$("teach-open").onclick = () => {
  $("teach").hidden = false;
  if (!editingName && !$("rf-name").value) rfClear();
  $("teach-url").focus();
};

$("teach-start").onclick = async () => {
  const r = await post("/api/teach/start", { url: $("teach-url").value.trim() });
  toast(r.message || "Done.", r.ok !== false);
};

$("teach-read").onclick = async () => {
  const r = await post("/api/teach/read", {});
  const p = r.proposal;
  if (p) {
    if (p.player) $("rf-player").value = p.player;
    if (p.play_button) $("rf-play").value = p.play_button;
    if ((p.player || p.play_button) && !$("rf-match").value) $("rf-match").value = p.match_url || "";
    if (p.links) {
      $("rf-links").value = p.links.selector;
      $("rf-cpage").value = p.links.page;
      lastIndexUrl = p.links.page;
    }
    if (!$("rf-name").value && p.url) {
      try { $("rf-name").value = new URL(p.url).hostname || "My page"; } catch (e) { $("rf-name").value = "My page"; }
    }
    showPreview(p);
  }
  toast(r.message || "Done.", r.ok !== false);
};

$("teach-stop").onclick = async () => {
  const r = await post("/api/teach/stop", {});
  toast(r.message || "Done.", r.ok !== false);
};

$("teach-cancel").onclick = async () => {
  await post("/api/teach/stop", {});
  rfClear();
  $("teach").hidden = true;
};

$("recipe-save").onclick = async () => {
  const recipe = rfRecipe();
  const r = await post("/api/recipes/save", { recipe, replace: editingName });
  toast(r.message || "Done.", r.ok !== false);
  if (r.ok) {
    editingName = recipe.name;
    $("recipe-add-collect").hidden = !(recipe.collect && /^(https?|file):\/\//i.test(lastIndexUrl || recipe.collect.page));
    loadRecipes();
  }
};

// "collect: URL" in the list of links stands for every video that page links to.
$("recipe-add-collect").onclick = async () => {
  const url = lastIndexUrl || $("rf-cpage").value.trim();
  let info;
  try { info = await api("/api/setup"); } catch (e) { toast("The playcap UI server is not responding.", false); return; }
  const links = (info.links || "").trim();
  const line = "collect: " + url;
  if (links.split(/\r?\n/).some((l) => l.trim() === line)) { toast("That index page is already in your list."); return; }
  const r = await post("/api/config", { links: (links ? links + "\n" : "") + line });
  const err = r.errors && (r.errors.links || Object.values(r.errors)[0]);
  toast(r.ok ? "Added. Press “Refresh queue” with the playcap browser open." : (err || r.message || "Not saved."), !!r.ok);
};

$("recipe-import").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  if (file.size > 512 * 1024) { toast("That file is too big to be a recipe.", false); return; }
  const r = await post("/api/recipes/import", { text: await file.text() });
  toast(r.message || "Done.", r.ok !== false);
  loadRecipes();
});

loadRecipes();
