// Atlas: the helpers the pages share (words, times, the seating order the terminal view's Prev and Next walk), and the
// Teams page at /office (spec 2026-10-31-org-chart): Dylan, the chief, then one column per team, each agent under its
// parent on square lines. Read-only: nothing here sends anything to an agent or a pane.
"use strict";

const WORDS = { working: "Working", blocked: "Blocked", idle: "Idle", unknown: "Unknown" };
const KINDS = ["chief", "team-lead", "product-lead", "engineering-lead", "design-lead", "reviewer", "standing", "worker"];
const ROLES = { chief: "chief of staff", "team-lead": "team lead", "product-lead": "product lead", "engineering-lead": "engineering lead",
                "design-lead": "design lead", reviewer: "reviewer", standing: "standing agent", worker: "worker" };
const HEADS = ["chief", "team-lead"]; // a team's head: the team lead, or the chief
const LEADS = ["product-lead", "engineering-lead", "design-lead"];
const POLL_MS = 5000, DEADLINE_MS = 4000; // a read not answered within DEADLINE_MS counts as failed
// A fold opened by a tap: its rows rise in once. The class is added by the tap, so a redraw (which builds new nodes) never plays it.
document.addEventListener("click", (e) => { const d = e.target.closest("summary")?.parentNode; if (d && !d.open) d.classList.add("pop-in"); });

// ---- Data logic (no DOM; runs under node for the worker's checks) ----

const fmt = (opts) => new Intl.DateTimeFormat("en-US", opts);
const ET = fmt({ timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
const ET_S = fmt({ timeZone: "America/New_York", hour: "numeric", minute: "2-digit", second: "2-digit" });
const UTC = fmt({ timeZone: "UTC", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const UTC_S = fmt({ timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const DAY = fmt({ timeZone: "America/New_York", month: "short", day: "numeric" });

// "4:26 PM ET (20:26 UTC)", with the date in front when it is not today in ET.
function when(iso, seconds, now = new Date()) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "not known";
  const day = DAY.format(d) === DAY.format(now) ? "" : `${DAY.format(d)}, `;
  // ICU puts a narrow no-break space before AM/PM, which the vendored mono subset does not carry.
  const et = (seconds ? ET_S : ET).format(d).replace(/ /g, " ");
  return `${day}${et} ET (${(seconds ? UTC_S : UTC).format(d)} UTC)`;
}

// An agent herdr has not named carries its pane as its name; it shows as "<runtime> in <pane>".
const label = (a) => (a.name === a.pane ? `${a.runtime || "agent"} in ${a.pane}` : a.name);
// A blocked pane is its parent's to clear, not Dylan's: it reads "Blocked for <parent>" when the parent is known.
const word = (a) => (a.state === "blocked" && a.parent ? `Blocked for ${a.parent}` : WORDS[a.state] || "Unknown");
// Own keys only, so a kind such as "toString" or "__proto__" gets no role word, not an inherited value.
const role = (a) => (Object.hasOwn(ROLES, a.mascot_kind) ? ROLES[a.mascot_kind] : "");
const rank = (a) => (KINDS.includes(a.mascot_kind) ? KINDS.indexOf(a.mascot_kind) : KINDS.length);
const byLabel = (a, b) => (label(a) < label(b) ? -1 : label(a) > label(b) ? 1 : 0);
const count = (agents) => {
  const n = agents.length, m = agents.filter((a) => a.state === "working").length;
  return `${n} ${n === 1 ? "agent" : "agents"}${m ? `, ${m} working` : ""}`;
};

// The live agents grouped by team, in the API's team order (state.team_order, the home page's: teams that need Dylan
// first) or, without one, the structure file's (the order of state.teams), each team in seating order. A live agent
// with no team, or a team the file does not list, goes in a last group, "No team". Gone agents are left out, and so
// are teams with nobody live. The terminal view's Prev and Next walk the agents in this order.
function office(state, maxCols = Infinity) {
  const live = state.agents.filter((a) => a.gone === false);
  const order = Array.isArray(state.team_order) ? state.team_order : [];
  const place = (key) => (order.includes(key) ? order.indexOf(key) : order.length);
  const carpets = Object.entries(state.teams || {}).map(([key, name]) => ({ key, name, agents: [] }))
    .sort((a, b) => place(a.key) - place(b.key));
  const none = { key: null, name: "No team", agents: [] };
  for (const a of live) (carpets.find((c) => c.key === a.team) || none).agents.push(a);
  return {
    live,
    carpets: [...carpets, none].filter((c) => c.agents.length).map((c) => ({
      ...c, count: count(c.agents), ...(c.key ? seating(c.agents, maxCols) : seating(c.agents, maxCols + 1, true)),
    })),
  };
}

// A team's seating order by rank: the head (the team lead, or the chief), the other leads in kind order then name,
// then each lead's agents in turn (found by walking parent down from the lead), then everyone else. Returns the head,
// the seats as { a, row, col }, the grid's size, and agents in seating order.
function seating(members, maxCols = Infinity, headless = false) {
  const sorted = [...members].sort((a, b) => rank(a) - rank(b) || byLabel(a, b));
  const head = headless ? null : sorted.find((a) => HEADS.includes(a.mascot_kind)) || null;
  const leads = sorted.filter((a) => a !== head && LEADS.includes(a.mascot_kind));
  const rest = sorted.filter((a) => a !== head && !leads.includes(a));
  const behind = [];
  for (const lead of leads) {
    const walk = (name) => rest.filter((r) => r.parent === name && !behind.includes(r)).forEach((r) => { behind.push(r); walk(r.name); });
    walk(lead.name);
  }
  behind.push(...rest.filter((a) => !behind.includes(a)));
  const wide = Math.max(maxCols, leads.length), top = leads.length ? 2 : 1;
  const seats = [...leads.map((a, k) => ({ a, row: 1, col: k })),
                 ...behind.map((a, k) => ({ a, row: top + Math.floor(k / wide), col: k % wide }))];
  return {
    head, seats, agents: [...(head ? [head] : []), ...[...seats].sort((p, q) => p.row - q.row || p.col - q.col).map((x) => x.a)],
    cols: Math.max(1, ...seats.map((x) => x.col + 1)), rows: Math.max(1, ...seats.map((x) => x.row)),
  };
}

// Seconds in the current state, from state_since; 0 when the API gives no time.
const secondsIn = (a, now) => (a.state_since ? Math.max(0, (now - Date.parse(a.state_since)) / 1000) : 0);

// Time in the state, in the largest unit only: "40 s", "12 min", "3 h", "2 d"; "at least" when the start of the
// state was not seen (state_since_exact false). Nothing when the API gives no time.
function since(a, now = Date.now()) {
  if (!a.state_since) return "";
  const s = secondsIn(a, now);
  const v = s < 60 ? `${Math.floor(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`;
  return a.state_since_exact === false ? `at least ${v}` : v;
}

// A card's name: a pane herdr has not named reads by its kind and folder ("codex, chief folder"), never its pane id.
function cardName(a) {
  if (a.name !== a.pane) return a.name;
  const folder = String(a.cwd || "").split("/").filter(Boolean).pop();
  return `${a.runtime || "agent"}, ${folder ? `${folder} folder` : "no folder"}`;
}

// The chart from /api/state. The chief is the root (the live agent of kind chief). Under it, one column per
// team in the API's team order, each holding the chief's live reports in that team (its team lead; for the chief's own
// team, its standing agents), and under each agent its live reports, by rank then name. A live agent the walk from the
// chief does not reach goes in the row, with its reason; it is never put under a guessed parent. A worker whose team is
// known but whose parent is not (placement_rule parent-unknown) stays in its team's column, under "Parent not known".
// Gone agents take no place, the chief included: with no live chief, its live reports go in the row.
function chart(state) {
  const live = state.agents.filter((a) => a.gone === false);
  const names = new Set(live.map((a) => a.name));
  const chief = live.find((a) => a.mascot_kind === "chief") || null;
  const seen = new Set(chief ? [chief.name] : []);
  const byRank = (a, b) => rank(a) - rank(b) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  const kids = (name) => live.filter((a) => a.parent === name && !seen.has(a.name)).sort(byRank);
  const grow = (a) => { seen.add(a.name); return { a, kids: [], done: [] }; };
  const fill = (node) => { node.kids = kids(node.a.name).map(grow); node.kids.forEach(fill); return node; };
  const teams = state.teams || {};
  const order = [...(Array.isArray(state.team_order) ? state.team_order : []), ...Object.keys(teams)];
  const top = chief ? kids(chief.name) : [];
  const columns = [...new Set(order)].filter((k) => Object.hasOwn(teams, k))
    .map((key) => ({ key, name: teams[key], heads: top.filter((a) => a.team === key) }));
  columns.push({ key: null, name: "No team", heads: top.filter((a) => !Object.hasOwn(teams, a.team)) });
  // Heads are claimed for every column before any column's tree is walked, so no head is taken as another's report.
  const cols = columns.filter((c) => c.heads.length).map((c) => ({ ...c, heads: c.heads.map(grow) }));
  cols.forEach((c) => c.heads.forEach(fill));
  cols.forEach((c) => {
    c.unknown = live.filter((a) => a.placement_rule === "parent-unknown" && a.team === c.key && !seen.has(a.name)).sort(byRank).map(grow);
    c.unknown.forEach(fill);
  });
  // A finished worker (its report is written, state.agents[].finished) that is idle and has no reports of its own folds
  // under its parent, or under "Parent not known": the tree keeps the rest, node.done the folded.
  const folds = (n) => n.a.finished === true && n.a.state === "idle" && !n.kids.length && !n.done.length;
  const fold = (node) => { node.kids.forEach(fold); node.done = node.kids.filter(folds); node.kids = node.kids.filter((k) => !folds(k)); };
  cols.forEach((c) => {
    c.heads.forEach(fold);
    const u = { kids: c.unknown };
    fold(u);
    c.unknown = u.kids;
    c.doneUnknown = u.done;
  });
  const out = live.filter((a) => !seen.has(a.name)).sort(byRank).map((a) => ({ a, why: why(a, names) }));
  const n = (st) => live.filter((a) => a.state === st).length;
  return { chief, cols, out, agents: live.length, teams: cols.filter((c) => !chief || c.key !== chief.team).length, working: n("working") };
}

// Why a live agent is not in the chart, in plain words.
function why(a, live) {
  if (a.placement_rule === "unplaced") return "No team's folder or workspace holds it, and the structure file does not name it.";
  if (!a.parent) return "Nobody it reports to is known.";
  return live.has(a.parent) ? `It reports to ${a.parent}, who is not in the chart.` : `It reports to ${a.parent}, who is not live.`;
}

// RainCLI words (spec 2026-11-02-raincli-org-chart): the presence words and the held reasons in words, which the thread page
// shows. The org chart's cards (spec 2026-11-03) use only the name and the waiting line; never message text or an address.
const PRESENCE = { ready: "Ready", busy: "Busy", offline: "Offline", no_inbox: "No inbox yet" };
// RainCLI's own words for each hold code (presentation.py), with "their" for the name, which the card names. Any other
// code is written as RainCLI gives it.
const HOLD = { no_inbox: "waiting for them to pick an inbox", inbox_busy: "their inbox is busy", inbox_next_turn: "waiting for their inbox's next turn",
               inbox_offline: "their inbox is offline", update_needed: "their RainCLI needs an update", approval: "waiting for their approval",
               inbox_failed: "their inbox couldn't take it", agent_busy: "their agent is busy", agent_next_turn: "waiting for their agent's next turn",
               agent_offline: "their agent is offline" };
const holdWord = (r) => (Object.hasOwn(HOLD, r) ? HOLD[r] : r || "held");
const presence = (p) => (Object.hasOwn(PRESENCE, p) ? PRESENCE[p] : "Unknown");
// A card's title: a person with no display name on RainCLI is stored as the masked "[address]".
const shownName = (n) => (n === "[address]" ? "No name on RainCLI" : n);
// Our own inbox's holds, on the foot line: the connector's agent by name, and "our" where a person's card says "their".
const ownHoldWord = (code, agent) => {
  const a = agent || "our inbox", k = String(code).replace(/^(inbox|agent)_/, "");
  return { no_inbox: "waiting for us to pick an inbox", busy: `${a} is busy`, next_turn: `waiting for ${a}'s next turn`, offline: `${a} is offline`,
           failed: `${a} couldn't take it`, needed: "our RainCLI needs an update", approval: "waiting for our approval" }[code === "update_needed" ? "needed" : code === "no_inbox" ? code : k] || code;
};
// "Waiting on our reply since 4:15 PM, 2 h 5 min": the time of their first unanswered message, with its weekday when it was
// not today in ET, and how long it has waited in the ask page's own words (report.js: "1 h 42 min", "2 days" from 48 h).
// Past six days a weekday repeats, so the date stands in for the weekday and time: "since Oct 15, 19 days".
const WEEKDAY = fmt({ timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit" });
const waited = (iso, now = Date.now()) => {
  const m = Math.max(0, Math.floor((now - Date.parse(iso)) / 60000)), h = Math.floor(m / 60);
  return h >= 48 ? `${Math.floor(h / 24)} days` : h ? `${h} h ${m % 60} min` : `${Math.max(1, m)} min`;
};
function waitingLine(iso, now = Date.now()) {
  const d = new Date(iso), today = DAY.format(d) === DAY.format(new Date(now));
  if (now - d > 6 * 86400000) return `Waiting on our reply since ${DAY.format(d)}, ${waited(iso, now)}`;
  const p = Object.fromEntries(WEEKDAY.formatToParts(d).map((x) => [x.type, x.value]));
  const at = `${p.hour}:${p.minute} ${p.dayPeriod}`;
  return `Waiting on our reply since ${today ? "" : `${p.weekday} `}${at}, ${waited(iso, now)}`;
}

if (typeof module !== "undefined") {
  module.exports = { when, presence, waited, waitingLine, holdWord, shownName, ownHoldWord, label, word, role, count, office, seating, since, secondsIn, cardName, chart, WORDS, KINDS, ROLES, HEADS, LEADS };
}

// ---- The Teams page ----

// The Teams page only: an agent's own tab (/agent/<name>) and the home page load this file for its helpers.
if (typeof document !== "undefined" && document.getElementById("chart")) {
  const $ = (id) => document.getElementById(id);
  const root = $("chart");
  const MIN_COL = 190; // a team column is never narrower; past that the whole chart stacks, as on a phone
  // The team colour comes from this fixed map, never from an API value written into CSS.
  const TEAM = { chief: "var(--t-chief)", atlas: "var(--t-atlas)", forge: "var(--t-forge)", forecast: "var(--t-forecast)", orion: "var(--t-orion)" };
  const tone = (key) => (Object.hasOwn(TEAM, key) ? TEAM[key] : "var(--ink-3)");
  const STATE = (a) => (Object.hasOwn(WORDS, a.state) ? a.state : "unknown");
  // "." and ".." would resolve away from /agent/<name> however they are encoded: their cards are text only.
  const linkable = (name) => name !== "." && name !== "..";
  const data = { state: null };

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  // A card: the team's colour as its left edge, the team's name over a column's head, the name, the reason for a card
  // in the row, then the role and the state with its time. The state word and time keep to one line together.
  function card(a, now, kicker, reason) {
    const c = el(linkable(a.name) ? "a" : "div", "card");
    if (linkable(a.name)) c.href = `/agent/${encodeURIComponent(a.name)}`;
    c.dataset.agent = a.name;
    c.style.setProperty("--tc", tone(a.team));
    if (kicker) c.append(el("span", "c-kick", kicker));
    c.append(el("span", "c-name", cardName(a)));
    if (reason) c.append(el("span", "c-why", reason));
    const st = el("span", `c-st is-${STATE(a)}`), w = el("span", "c-w"), t = since(a, now);
    const said = t ? `${word(a)}, ${t}` : word(a);
    w.append(el("i", "dot"), said);
    st.append(...(role(a) ? [el("span", "c-role", role(a))] : []), w);
    c.append(st);
    return c;
  }

  // A RainCLI contact: the agent card with the name and, in clay, how long their last message has waited on us. A person with
  // nothing waiting is the name alone. It opens the read-only thread.
  function contact(x, now) {
    const c = el("a", "card outside");
    c.href = `/raincli/${encodeURIComponent(x.id)}`;
    c.append(el("span", "c-name", shownName(x.name)));
    if (x.waiting_since) c.append(el("span", "c-held", waitingLine(x.waiting_since, now)));
    else if (x.unread) c.append(el("span", "c-st", "Thread not read yet"));
    return c;
  }

  // The people who have written, as the children of raincli-inbox: those waiting on our reply first, oldest first, then
  // one quiet fold, "<n> with nothing waiting", of the rest, newest first (the API's order). A failed read keeps the
  // last-read cards under one quiet line; before any good read the line is alone. On a phone one line, "<k> people
  // outside" (with "<j> waiting on our reply" in clay), opens them in place; the open states are kept across redraws.
  function outside(now) {
    const r = data.state && data.state.raincli;
    if (!r || !(r.read || r.tried)) return null;
    const lost = !r.ok && (r.read ? el("p", "none", `RainCLI not reachable: as last read at ${tm(r.read_at)}`) : el("p", "none", "RainCLI not reachable"));
    if (!r.read) return lost;
    const people = r.contacts.filter((x) => x.wrote || x.unread);
    const waiting = people.filter((x) => x.waiting_since).sort((a, b) => Date.parse(a.waiting_since) - Date.parse(b.waiting_since));
    const unread = people.filter((x) => !x.waiting_since && x.unread), rest = people.filter((x) => !x.waiting_since && !x.unread);
    const list = el("ul", "tree");
    for (const x of waiting) { const li = el("li"); li.append(contact(x, now)); list.append(li); }
    for (const x of unread) { const li = el("li"); li.append(contact(x, now)); list.append(li); }
    if (rest.length) {
      const li = el("li"), f = el("details", "fold"), box = el("div", "fold-cards");
      f.append(el("summary", null, `${rest.length} with nothing waiting`));
      box.append(...rest.map((x) => contact(x, now)));
      f.open = data.restOpen === true;
      f.addEventListener("toggle", () => { data.restOpen = f.open; });
      f.append(box);
      li.append(f);
      list.append(li);
    }
    if (!phone.matches) { const d = el("div", "rain-cards"); d.append(...(lost ? [lost] : []), list); return d; }
    const d = el("details", "rain-cards");
    const sum = el("summary", null, `${people.length} ${people.length === 1 ? "person" : "people"} outside`);
    if (waiting.length) sum.append(el("span", "held", `${waiting.length} waiting on our reply`));
    d.open = data.rainOpen === true;
    d.addEventListener("toggle", () => { data.rainOpen = d.open; });
    d.append(sum, ...(lost ? [lost] : []), list);
    return d;
  }

  // "<n> finished": one closed <details> of cards, each still a link to its terminal.
  function folded(nodes, now) {
    const d = el("details", "fold"), box = el("div", "fold-cards");
    d.append(el("summary", null, `${nodes.length} finished`));
    box.append(...nodes.map((n) => card(n.a, now)));
    d.append(box);
    return d;
  }

  // Each parent's reports, on one spine down its left with a stub to each card, then its finished workers folded.
  function branch(nodes, now, done = []) {
    const ul = el("ul", "tree");
    for (const n of nodes) {
      const li = el("li");
      li.append(card(n.a, now));
      if (n.kids.length || n.done.length) li.append(branch(n.kids, now, n.done));
      if (n.a.name === "raincli-inbox") li.append(outside(now) || "");
      ul.append(li);
    }
    if (done.length) { const li = el("li"); li.append(folded(done, now)); ul.append(li); }
    return ul;
  }

  // A column: its one head's card with the team's name over it, and its tree; a team with several heads under the
  // chief (the chief's own standing agents) gets a quiet name card, and the heads hang off its spine.
  function column(c, now) {
    const col = el("div", "col");
    if (c.heads.length === 1) {
      col.append(card(c.heads[0].a, now, c.name));
      if (c.heads[0].kids.length || c.heads[0].done.length) col.append(branch(c.heads[0].kids, now, c.heads[0].done));
      // The RainCLI inbox heads its column: its contacts and groups hang under its card, as under a stacked branch.
      const o = c.heads[0].a.name === "raincli-inbox" ? outside(now) : null;
      // The wide cards are the inbox's children: their own tree goes straight under it, indented as any team's. The
      // fold and the unreachable line sit in one tree item.
      if (o && o.tagName === "DIV") col.append(o);
      else if (o) { const ul = el("ul", "tree"), li = el("li"); li.append(o); ul.append(li); col.append(ul); }
    } else {
      const g = el("div", "card group");
      g.style.setProperty("--tc", c.key ? tone(c.key) : "var(--ink-3)");
      g.append(el("span", "c-kick", c.name), el("span", "c-st", `${c.heads.length} report to the chief`));
      col.append(g, branch(c.heads, now));
    }
    // Workers of this team whose parent is not known: under a quiet line, with no line from anyone, since none is known.
    if (c.unknown.length || c.doneUnknown.length) {
      const box = el("div");
      box.style.cssText = "display: grid; gap: 10px; margin-top: 10px;";
      for (const n of c.unknown) box.append(card(n.a, now), ...(n.kids.length || n.done.length ? [branch(n.kids, now, n.done)] : []));
      if (c.doneUnknown.length) box.append(folded(c.doneUnknown, now));
      // One wrapper for the label and its cards, so a stacked chart can indent the group as the team's own (app.css).
      const group = el("div", "unknown");
      group.append(el("p", "quiet", "Parent not known"), box);
      col.append(group);
    }
    return col;
  }

  // Columns share the width, each at least MIN_COL; when they do not fit, or on a phone whatever the team count, the
  // chart is one indented column.
  const phone = matchMedia("(max-width: 640px)");
  function fit() {
    const cols = root.querySelectorAll(".teams > .col").length;
    root.classList.remove("is-stacked"); // measured at full width: a stacked chart is narrower
    root.classList.toggle("is-stacked", cols > 0 && (phone.matches || root.clientWidth / cols < MIN_COL));
  }

  function render() {
    const s = data.state;
    if (!s) return;
    // Before the first herdr snapshot nobody is known to be live or not.
    if (!s.as_of) {
      root.replaceChildren(el("p", "quiet", "Not read yet"));
      return;
    }
    const now = Date.now(), c = chart(s);
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.agent : null;
    const dylan = el("div", "card dylan");
    dylan.append(el("span", "c-name", "Dylan"), el("span", "c-st", "decides"));
    const parts = [el("div", "top")];
    parts[0].append(dylan);
    const mark = el("p", "legend"), key = (cls, text) => { const w = el("span", `c-st is-${cls}`); w.append(el("span", "c-w")); w.firstChild.append(el("i", "dot"), text); return w; };
    mark.append(key("working", "Working"), key("idle", "Idle"), key("blocked", "Blocked, for its parent to clear"), key("unknown", "Unknown"), el("span", null, "Tap a card to open its terminal"));
    parts.unshift(mark);
    if (c.chief) {
      const top = el("div", "top");
      parts.push(el("div", "down"));
      top.append(card(c.chief, now));
      parts.push(top);
    }
    if (c.cols.length) {
      const teams = el("div", "teams");
      teams.append(...c.cols.map((x) => column(x, now)));
      parts.push(teams);
    }
    if (c.out.length) {
      const sec = el("section", "unplaced"), row = el("div", "out");
      row.append(...c.out.map((x) => card(x.a, now, null, x.why)));
      sec.append(el("h2", null, "Not in the chart: herdr sees them, but Atlas cannot be sure who they report to"), row);
      parts.push(sec);
    }
    root.replaceChildren(...parts);
    fit();
    if (focused) {
      const back = [...root.querySelectorAll("a.card")].find((x) => x.dataset.agent === focused);
      if (back) back.focus({ preventScroll: true });
    }
    const plural = (n, one) => `${n} ${n === 1 ? one : `${one}s`}`;
    const shown = s.raincli && s.raincli.read ? s.raincli.contacts.filter((x) => x.wrote || x.unread) : null;
    const rain = shown && shown.length, owed = shown ? shown.filter((x) => x.waiting_since).length : 0;
    $("sum").replaceChildren(el("b", null, plural(c.agents, "agent")), ` in ${plural(c.teams, "team")}`,
      ...(shown === null ? [] : [`, and ${plural(rain, "person").replace("persons", "people")} outside on RainCLI${owed ? `, ${owed} waiting on our reply` : ""}`]),
      `. ${c.working} working`, ...(s.asks ? [", ", el("span", "you", `${s.asks} waiting on you`)] : []), ".");
  }
  addEventListener("resize", fit);
  phone.addEventListener("change", render);

  // ---- The bar and the footer ----

  // "12:49 PM", with the date in front when it is not today in ET.
  const tm = (iso) => {
    const d = new Date(iso);
    return `${DAY.format(d) === DAY.format(new Date()) ? "" : `${DAY.format(d)}, `}${ET.format(d).replace(/ /g, " ")}`;
  };
  // "Live, <time>" of the last herdr snapshot, or "Offline, last <time>" while Atlas does not answer or herdr is not
  // ok: the states on the chart are then that old.
  function renderBar() {
    const s = data.state, herdr = s ? s.sources.find((x) => x.source === "hc") : null;
    const off = read.failing || Boolean(herdr && herdr.state !== "ok"), t = s && s.as_of ? tm(s.as_of) : null;
    $("live").className = off ? "live off" : "live";
    $("live").replaceChildren(el("i"), off ? (t ? `Offline, last ${t}` : "Offline") : t ? `Live, ${t}` : "Reading");
    if (s) $("foot").textContent = `Atlas at ${s.commit}`;
    if (!s && !root.children.length) root.replaceChildren(el("p", "quiet", "Not read yet"));
  }

  // ---- Refresh: /api/state every 5 s, start to start, conditional on its last ETag ----

  // The body and ETag are accepted together. A read that fails or passes its deadline changes neither, and marks the
  // page failing until it answers again.
  const read = { failing: false };
  async function readState() {
    try {
      data.state = await POLL.get("/api/state", DEADLINE_MS);
      read.failing = false;
    } catch (e) {
      read.failing = true;
    }
    // Times in each state move on even when the state does not change, so the chart is drawn on every read.
    render();
    renderBar();
  }

  // No reads while the tab is hidden; the first read after it shows again is at once.
  let waiting = null;
  function every(next) {
    const t0 = Date.now();
    next().catch((e) => console.error(e)).finally(() => {
      waiting = setTimeout(() => { waiting = null; document.hidden ? (waiting = 0) : every(next); }, Math.max(0, POLL_MS - (Date.now() - t0)));
    });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && waiting === 0) { waiting = null; every(readState); } }); // null: a read is in flight, so a second show starts no second chain
  renderBar();
  every(readState);
}
