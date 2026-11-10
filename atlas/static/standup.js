// The stand-up: for each team, what was done since the previous working day, what is in progress, what is blocked and
// what is waiting. Read-only, composed in the browser from /api/items, /api/changes, /api/briefing and /api/state.
// Every value from an API reaches the page as text.
"use strict";
{
  const { el, line } = MD, root = document.getElementById("standup"), live = document.getElementById("live");
  const LIMIT = 5, EVERY_MS = 10000;
  const fmt = (o) => new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", ...o });
  const DAYFMT = fmt({ weekday: "short", month: "short", day: "numeric" }), DAY = { format: (d) => DAYFMT.format(d).replace(",", "") }, PARTS = fmt({ year: "numeric", month: "numeric", day: "numeric", hour: "numeric", hourCycle: "h23" });
  const TIME = fmt({ hour: "numeric", minute: "2-digit" }), WD = fmt({ weekday: "short" }), ISO_DAY = fmt({ year: "numeric", month: "2-digit", day: "2-digit" });
  const part = (d, type) => +PARTS.formatToParts(d).find((p) => p.type === type).value;

  // The start of the previous working day in ET: yesterday, then back over Saturday and Sunday (no holidays).
  function sinceStart(now) {
    const d = new Date(now);
    let day = new Date(Date.UTC(part(d, "year"), part(d, "month") - 1, part(d, "day")) - 86400000);
    while ([0, 6].includes(day.getUTCDay())) day = new Date(day - 86400000);
    const [y, m, n] = [day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate()];
    // ET midnight is 04:00 or 05:00 UTC; the one that reads as hour 0 on that date is it.
    return [4, 5].map((h) => new Date(Date.UTC(y, m, n, h))).find((c) => part(c, "hour") === 0 && part(c, "day") === n) || new Date(Date.UTC(y, m, n, 5));
  }

  const TEAM = { chief: "var(--t-chief)", atlas: "var(--t-atlas)", forge: "var(--t-forge)", forecast: "var(--t-forecast)", orion: "var(--t-orion)" };
  const tone = (key) => (Object.hasOwn(TEAM, key) ? TEAM[key] : "var(--ink-3)");
  const closed = (i) => ["done", "dropped"].includes(i.status);
  const ROLE = ["chief", "team-lead", "product-lead", "engineering-lead"];
  const link = (text, url, cls) => { const a = el("a", cls, text); a.href = url; return a; };
  const age = (at) => {
    const s = Math.max(0, (Date.now() - Date.parse(at)) / 1000);
    return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
  };
  const when = (at) => { const d = new Date(at); return `${ISO_DAY.format(d) === ISO_DAY.format(Date.now()) ? "" : WD.format(d) + " "}${TIME.format(d)}`; };
  const byTime = (field, newest) => (a, b) => (newest ? -1 : 1) * (Date.parse(a[field]) - Date.parse(b[field]) || 0);
  const get = (url) => POLL.get(url, 8000); // poll.js

  // ---- The model: each team's four lists, from the four APIs ----
  function model(items, changes, brief, state, since) {
    const board = items.filter((i) => i.status !== "backlog");
    // A sub-item is left out of a list only when its parent is in the same list.
    const alone = (list) => { const here = new Set(list.map((i) => i.id)); return list.filter((i) => !here.has(i.parent)); };
    const rankOf = (name) => { const a = state.agents.find((x) => x.name === name), r = a ? ROLE.indexOf(a.mascot_kind) : -1; return r < 0 ? ROLE.length : r; };
    // Home's rule for what waits on Dylan, the same function: open asks, by team.
    const asks = ASKS.open(brief), askHref = new Set(asks.map((k) => k.href));
    const sinceDay = ISO_DAY.format(since).replace(/(\d+)\/(\d+)\/(\d+)/, "$3-$1-$2");
    return brief.teams.map((t) => {
      const mine = board.filter((i) => i.team === t.key);
      const done = alone(mine.filter((i) => i.status === "done" && Date.parse(i.updated) >= +since)).sort(byTime("updated", true));
      const linked = new Set(done.flatMap((i) => i.closure.map((c) => c.href).filter(Boolean)));
      const path = (c) => `${encodeURIComponent(c.team)}/${c.name.split("/").map(encodeURIComponent).join("/")}`;
      const recent = (c) => ((c.written_at || "").includes("T") ? Date.parse(c.written_at) >= +since : (c.written_at || "") >= sinceDay);
      const reports = changes.filter((c) => c.team === t.key && c.has_change && recent(c) && !linked.has(`/change/${path(c)}`) && !linked.has(`/ask/${path(c)}`))
        .sort((a, b) => String(b.written_at).localeCompare(String(a.written_at)));
      const doing = alone(mine.filter((i) => i.status === "doing" && !i.waiting_on_you))
        .sort((a, b) => rankOf(a.owner) - rankOf(b.owner) || Date.parse(b.updated) - Date.parse(a.updated));
      const blocked = [
        ...asks.filter((k) => k.team === t.key).sort((a, b) => String(a.ask.asked_at).localeCompare(String(b.ask.asked_at))).map((k) => ({ kind: "ask", k })),
        ...alone(mine.filter((i) => i.waiting_on_you && !(i.ask && askHref.has(i.ask.href)))).sort(byTime("updated", false)).map((i) => ({ kind: "item", i })),
        ...state.agents.filter((a) => a.team === t.key && a.state === "blocked" && a.gone === false).sort(byTime("state_since", false)).map((a) => ({ kind: "pane", a })),
      ];
      const waiting = alone(mine.filter((i) => ["waiting", "proving"].includes(i.status) && !i.waiting_on_you)).sort(byTime("updated", false));
      return { t, done: [...done.map((i) => ({ kind: "item", i })), ...reports.map((c) => ({ kind: "report", c }))], doing: doing.map((i) => ({ kind: "item", i })),
        blocked, waiting: waiting.map((i) => ({ kind: "item", i })) };
    });
  }

  // ---- Drawing ----
  const href = (id) => `/work/${encodeURIComponent(id)}`;
  const row = (title, url, meta, cls) => {
    const li = el("li", `su-row${cls ? " " + cls : ""}`), m = el("div", "su-meta");
    li.append(link(title, url, "su-title"), m);
    meta.forEach((x, n) => m.append(...(n ? [" · "] : []), x));
    return li;
  };
  const kindOf = { done: "Nothing done", doing: "Nothing in progress", blocked: "Nothing blocked", waiting: "Nothing waiting" };
  const rowOf = {
    done: (x) => {
      if (x.kind === "report") return row(x.c.title, `/${x.c.has_ask ? "ask" : "change"}/${encodeURIComponent(x.c.team)}/${x.c.name.split("/").map(encodeURIComponent).join("/")}`, ["Report", (x.c.written_at || "").includes("T") ? when(x.c.written_at) : x.c.written_at || "Time not written"]);
      const i = x.i, c = i.closure.find((r) => r.href);
      return row(i.title, href(i.id), [i.owner, when(i.updated), ...(c ? [link(c.label, c.href)] : [])]);
    },
    doing: (x) => row(x.i.title, href(x.i.id), [x.i.owner, ...(x.i.next ? [`Next: ${x.i.next}`] : []), el("span", x.i.untouched ? "clay" : null, `${x.i.untouched ? "untouched, " : ""}moved ${age(x.i.updated)}`)]),
    blocked: (x) => {
      if (x.kind === "ask") return row(x.k.ask.question || x.k.name, x.k.href || "/", [el("span", "su-flag", "Waiting on you"), `Default: ${x.k.ask.default || "Not written"}`], "you");
      if (x.kind === "item") return row(x.i.title, href(x.i.id), [el("span", "su-flag", "Waiting on you"), `Default: ${(x.i.ask && x.i.ask.default) || x.i.waiting_default || "Not written"}`, ...(x.i.ask ? [] : ["No ask written"])], "you");
      return row(`Blocked in ${x.a.name}'s terminal`, `/agent/${encodeURIComponent(x.a.name)}`, [x.a.parent ? `For ${x.a.parent}` : "No parent named"]);
    },
    waiting: (x) => row(x.i.title, href(x.i.id), [x.i.status === "proving" ? `Proving: ${x.i.stands || "stands not written"}` : `with ${x.i.waiting_on || "no one named"}`, ...(x.i.next ? [`Next: ${x.i.next}`] : [])]),
  };
  function list(key, name, rows, team) {
    const box = el("section", "su-list"), ul = el("ul");
    box.dataset.list = key; box.dataset.count = String(rows.length);
    box.append(el("h3", null, name));
    if (!rows.length) box.append(el("p", "su-empty", kindOf[key]));
    else {
      ul.append(...rows.slice(0, LIMIT).map(rowOf[key])); box.append(ul);
      if (rows.length > LIMIT) box.append(link(`and ${rows.length - LIMIT} more`, `/work?team=${encodeURIComponent(team)}`, "su-more"));
    }
    return box;
  }
  function draw(m, since) {
    const now = new Date(Date.now()), sinceLabel = DAY.format(since);
    const nodes = [el("h1", "pt", `Stand-up, ${DAY.format(now)}`), el("p", "sub", `Since the start of ${sinceLabel}.`)];
    for (const x of m) {
      const c = el("article", "team su-team"), t = x.t;
      c.dataset.team = t.key; c.style.setProperty("--tc", tone(t.key));
      const head = el("div", "su-head"), counts = [x.done.length, x.doing.length, x.blocked.length, x.waiting.length];
      head.append(el("h2", null, t.name));
      head.append(t.working ? Object.assign(el("p", "su-working"), {}) : el("p", "su-working", "No status line yet"));
      if (t.working) head.lastChild.append(...line(t.working));
      c.append(head);
      if (!counts.some(Boolean)) c.append(el("p", "su-none", `Nothing since ${sinceLabel}`));
      else {
        head.append(el("p", "su-counts", `${counts[0]} done, ${counts[1]} in progress, ${counts[2]} blocked, ${counts[3]} waiting`));
        const lists = el("div", "su-lists");
        lists.append(list("done", "Yesterday", x.done, t.key), list("doing", "Today", x.doing, t.key), list("blocked", "Blocked", x.blocked, t.key), list("waiting", "Waiting", x.waiting, t.key));
        c.append(lists);
      }
      nodes.push(c);
    }
    return nodes;
  }
  let shown = "", last;
  async function poll() {
    try {
      const [items, changes, brief, state] = await Promise.all([get("/api/items"), get("/api/changes"), get("/api/briefing"), get("/api/state?all=1")]);
      const since = sinceStart(Date.now()), box = document.createElement("div");
      box.append(...draw(model(items.items, changes.changes, brief, state, since), since));
      if (box.innerHTML !== shown) { shown = box.innerHTML; root.replaceChildren(...box.childNodes); }
      last = Date.now();
      live.classList.remove("off"); live.replaceChildren(el("i"), "Live");
    } catch (e) {
      live.classList.add("off"); live.replaceChildren(el("i"), "Could not update");
      if (!shown) root.replaceChildren(el("h1", "pt", "Could not read the stand-up"));
    }
  }
  poll(); setInterval(poll, EVERY_MS);
}
