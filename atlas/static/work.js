// The read-only work board and one item's trail, from the items index.
"use strict";
{
  const { el } = MD, root = document.getElementById("work"), live = document.getElementById("live");
  const q = new URLSearchParams(location.search), filter = q.get("f") || "all", team = q.get("team") || "";
  const id = decodeURIComponent(location.pathname.replace(/^\/work\/?/, ""));
  const expanded = new Set(), phone = matchMedia("(max-width: 640px)");
  let rising = null; // the list a "<n> more" tap just opened: its new rows rise in once
  document.addEventListener("click", (e) => { const d = e.target.closest("summary")?.parentNode; if (d && !d.open) d.classList.add("pop-in"); });
  let names = {}, snapshot, now, declared, byId = new Map();
  const closed = (i) => ["done", "dropped"].includes(i.status);
  const href = (id) => `/work/${encodeURIComponent(id)}`;
  const link = (text, url, cls) => { const a = el("a", cls, text); a.href = url; return a; };
  const color = (node, t) => node.style.setProperty("--tc", `var(--t-${CSS.escape(t)}, var(--ink-3))`);
  const span = (ms) => { const m = Math.floor(ms / 60000); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`; };
  const clock = (at) => new Date(at).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  const date = (at) => at ? `${new Date(at).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} ET` : "Time not written";
  const ago = (at) => { const ms = Math.max(0, now - Date.parse(at)); return !at ? "at an unknown time" : ms >= 86400000 ? `${Math.floor(ms / 86400000)} d ago` : `${span(ms)} ago`; };
  const get = (url) => POLL.get(url, 8000).catch((e) => { throw e.status === undefined ? e : new Error(e.status === 404 ? "No such item" : `Could not read work (${e.status})`); }); // poll.js
  function wait(i, detail = false) {
    const box = el("div", "work-wait");
    if (!i.waiting_on) return box;
    if (i.waiting_on_you) {
      box.append(el("span", "t-flag", "Waiting on you"));
      if (i.ask) {
        box.append(link(detail ? i.ask.question || "Open the ask" : "Open the ask", i.ask.href, "ask-link"));
        box.append(el("div", "quiet", `Default: ${i.ask.default || "Not written by the owner"}`));
      } else box.append(el("div", "clay", "no ask linked"));
    }
    const words = el("div", "wait-words");
    words.append("Waiting on: ", i.waiting_item ? link(i.waiting_on, href(i.waiting_item)) : i.waiting_on);
    box.append(words);
    return box;
  }
  function card(i) {
    const c = el("article", "team work-card"); c.dataset.item = i.id; c.dataset.status = i.status; color(c, i.team);
    c.append(el("div", "work-id", i.id), link(i.title, href(i.id), "work-title"));
    if (byId.has(i.parent)) c.append(link(`Part of ${byId.get(i.parent).title}`, href(i.parent), "work-subs"));
    if (i.status === "proving") {
      if (i.stands) c.append(el("div", "work-next work-stands", i.stands));
      if (i.proof) c.append(el("div", "work-proof", `Proof: ${i.proof}`));
    } else if (i.next) c.append(el("div", "work-next", `Next: ${i.next}`));
    c.append(el("div", "work-owner", i.owner), wait(i));
    const moved = el("div", i.untouched ? "clay" : "quiet", `${i.untouched ? "untouched, " : ""}moved ${ago(i.updated)}`);
    moved.title = date(i.updated); c.append(moved);
    if (i.children.total) c.append(el("div", "work-subs", `${i.children.done} of ${i.children.total} sub-items done`));
    if (closed(i)) {
      const end = el("div", "work-closure"); end.append(`${i.status}: `);
      if (i.closure.length) i.closure.forEach((r) => { end.append(r.href ? link(r.label, r.href) : el("span", null, r.label), " "); });
      else end.append(i.next || "No report, PR or reason written");
      c.append(end);
      if (i.status === "done" && i.proof) c.append(el("div", "work-proof", proofText(i)));
    }
    return c;
  }
  function cards(items, key) {
    const box = el("div", "work-cards"), limit = phone.matches && !expanded.has(key) ? 5 : items.length;
    box.append(...items.slice(0, limit).map((i, n) => { const c = card(i); if (rising === key && n >= 5) c.classList.add("pop"); return c; }));
    if (rising === key) rising = null;
    if (items.length > limit) {
      const more = el("button", "more", `${items.length - limit} more`); more.type = "button";
      more.addEventListener("click", () => { expanded.add(key); rising = key; drawBoard(); }); box.append(more);
    }
    return box;
  }
  function drawBoard() {
    const all = snapshot.items, ids = new Set(all.map((i) => i.id));
    byId = new Map(all.map((i) => [i.id, i]));
    const board = all.filter((i) => !ids.has(i.parent) || i.waiting_on_you);
    const laneOf = (i) => ids.has(i.parent) && i.waiting_on_you ? "waiting" : i.status;
    const open = board.filter((i) => !closed(i) && laneOf(i) !== "backlog");
    const you = board.filter((i) => i.waiting_on_you), old = board.filter((i) => i.untouched);
    const summary = el("p", "sub"); summary.dataset.summary = "";
    summary.append(el("b", null, `${open.length} open across the teams`));
    for (const [key, count, text, clay] of [["you", you.length, "waiting on you", false], ["no-ask", you.filter((i) => !i.ask).length, "with no ask linked", true], ["untouched", old.length, "untouched", true]]) {
      if (count) { const s = el("span", clay ? "clay" : null, `${count} ${text}`); s.dataset.count = key; summary.append(" · ", s); }
    }
    const chips = el("nav", "work-filters"); chips.setAttribute("aria-label", "Filter work");
    function chip(text, f, t, active) {
      const a = link(text, `?${new URLSearchParams({ ...(f !== "all" ? { f } : {}), ...(t ? { team: t } : {}) })}`, "work-filter");
      a.setAttribute("aria-pressed", String(active)); return a;
    }
    [["all", "All"], ["you", "Waiting on you"], ["untouched", "Untouched"]].forEach(([f, text]) => chips.append(chip(text, f, team, filter === f)));
    const teams = [...new Set(all.map((i) => i.team))];
    teams.forEach((t) => { const a = chip(names[t] || t, filter, team === t ? "" : t, team === t); color(a, t); a.prepend(el("i", "t-dot")); chips.append(a); });
    const keep = (i) => (!team || i.team === team) && (filter === "you" ? i.waiting_on_you : filter === "untouched" ? i.untouched : true);
    const lanes = el("div", "work-lanes");
    [["todo", "To do"], ["doing", "Doing"], ["waiting", "Waiting"], ["proving", "Proving"], ["done", "Done this week"]].forEach(([status, name]) => {
      const rows = board.filter((i) => keep(i) && (status === "done" ? i.closed_this_week : laneOf(i) === status));
      rows.sort((a, b) => Number(b.waiting_on_you) - Number(a.waiting_on_you) || Number(b.untouched) - Number(a.untouched) || String(b.updated).localeCompare(String(a.updated)));
      const lane = el("section", "work-lane"); lane.dataset.lane = status;
      const head = el("h2"); head.append(el("span", null, name), el("span", "lane-count", String(rows.length)));
      lane.append(head, cards(rows, status));
      if (!rows.length) lane.append(el("p", "quiet", "Nothing here"));
      const backlog = board.filter((i) => laneOf(i) === "backlog" && keep(i));
      if (status === "todo" && backlog.length) {
        const details = el("details", "work-backlog"), summary = el("summary", "more", `${backlog.length} in backlog`);
        details.open = expanded.has("backlog"); details.append(summary, cards(backlog, "backlog-cards"));
        // Record the click before a redraw; the browser dispatches `toggle` asynchronously.
        summary.addEventListener("click", () => { if (!details.open) expanded.add("backlog"); else expanded.delete("backlog"); });
        details.addEventListener("toggle", () => {
          if (details.isConnected) { if (details.open) expanded.add("backlog"); else expanded.delete("backlog"); }
        }); lane.append(details);
      }
      lanes.append(lane);
    });
    const empty = Object.keys(names).filter((t) => !teams.includes(t)).map((t) => names[t]);
    root.replaceChildren(el("h1", "pt", "Work"), summary, chips, el("p", "quiet", empty.length ? `${empty.join(", ")} ${empty.length === 1 ? "has" : "have"} no item files yet.` : ""), lanes);
  }
  // Open: Stands and Proof. Done: the Done card's proof line. Dropped: neither.
  const proofLines = (i) => closed(i) ? (i.status === "done" && i.proof ? [proofText(i)] : []) : [["Stands", i.stands], ["Proof", i.proof]].filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`);
  const proofText = (i) => /^Nothing to prove:/.test(i.proof) ? i.proof : `Proved: ${i.proof}`;
  function drawItem(i, repos) {
    root.classList.add("work-detail"); document.title = `${i.title} - Atlas`;
    const top = el("div", "work-kicker"); color(top, i.team);
    top.append(el("span", "work-id", i.id), el("span", "work-team", names[i.team] || i.team), el("span", null, `owner ${i.owner}`), el("span", null, i.status));
    const nodes = [link("Work", "/work", "quiet"), ...i.problems.map((p) => el("p", "clay", p)), top, el("h1", "pt", i.title), el("p", "sub", `Next: ${i.next || "Not written"}`),
      ...proofLines(i).map((t) => el("p", "sub", t)), wait(i, true)];
    if (i.parent) nodes.push(link(`Part of ${i.parent}`, href(i.parent), "work-parent"));
    if (i.ref) nodes.push(el("p", "quiet", `From ${i.ref}`));
    if (i.child_items.length) {
      const list = el("div", "list");
      i.child_items.forEach((c) => { const row = link(c.title, href(c.id), "work-child"); row.dataset.child = c.id; row.append(el("span", "quiet", `${c.status} · ${c.owner}`)); list.append(row); });
      nodes.push(el("h2", null, "Sub-items"), list);
    }
    const trail = el("div", "list work-trail"); let previous;
    i.trail.forEach((entry, index) => {
      if (previous && entry.at_iso && Date.parse(entry.at_iso) - Date.parse(previous) > 3600000) trail.append(el("p", "work-gap clay", `Nothing moved for ${span(Date.parse(entry.at_iso) - Date.parse(previous))}, ${clock(previous)} to ${clock(entry.at_iso)} ET`));
      const row = el("div", "work-event"); row.dataset.event = index;
      row.append(el("time", "quiet", entry.at_iso ? date(entry.at_iso) : entry.at || "Time not written"));
      const words = el("div");
      if (entry.kind !== "pr") words.append(el("b", null, entry.agent || entry.kind), " ");
      words.append(entry.href ? link(entry.label, entry.href) : el("span", null, entry.label));
      const m = /^https:\/\/github\.com\/your-org\/([\w.-]+)\/pull\/(\d+)$/.exec(entry.text || "") || /^([\w.-]+)#(\d+)$/.exec(entry.text || ""), g = m && MD.gh(repos, m[1], m[2]);
      if (g) words.append(" ", g);
      row.append(words); trail.append(row);
      if (entry.at_iso) previous = entry.at_iso;
    });
    nodes.push(el("h2", null, "Trail"), trail, el("p", "quiet", `From ${i.path_label}`)); root.replaceChildren(...nodes);
  }
  async function poll() {
    try {
      const [data, state] = await Promise.all([get(id ? `/api/item?${new URLSearchParams({ id })}` : "/api/items"), get("/api/state")]);
      names = state.teams || {}; now = Date.parse(data.as_of) || Date.now(); snapshot = data;
      if (id) drawItem(data, await (declared ||= MD.declared())); else drawBoard();
      live.classList.remove("off"); live.replaceChildren(el("i"), "Live");
    } catch (e) {
      live.classList.add("off"); live.replaceChildren(el("i"), "Could not update");
      if (!snapshot) root.replaceChildren(el("h1", "pt", e.message));
    }
  }
  phone.addEventListener("change", () => { if (snapshot && !id) drawBoard(); });
  poll(); setInterval(poll, 30000);
}
