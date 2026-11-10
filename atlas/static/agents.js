// Atlas: the agent grid at /agents (spec 2026-11-10-agent-grid). Every live agent is one tile: its name, team, status and the bottom
// of its terminal screen, as Atlas holds it. The tiles are sized to fit one window. View only: no input, no send, no POST; a tile
// is a link to /agent/<name>. Reads /api/state and /api/screens?text=1 together every 5 s; the words and the order come from app.js.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const grid = $("grid");
  const POLL_MS = 5000, DEADLINE_MS = 4000; // as the org chart: a read not answered within DEADLINE_MS counts as failed
  const GAP = 8, PAD = 16, MIN_W = 150, MIN_H = 90, SHAPE = 1.5; // tiles near 3:2, never under 150 by 90 px
  // The team colour comes from this fixed map, never from an API value written into CSS; a team with no colour keeps the line colour.
  const TEAM = { chief: "var(--t-chief)", atlas: "var(--t-atlas)", forge: "var(--t-forge)", forecast: "var(--t-forecast)", orion: "var(--t-orion)" };
  const STATE = (a) => (Object.hasOwn(WORDS, a.state) ? a.state : "unknown");
  // "." and ".." would resolve away from /agent/<name> however they are encoded: their tiles are text only.
  const linkable = (name) => name !== "." && name !== "..";
  const data = { state: null, screens: new Map(), failing: false };
  const tiles = new Map();
  let order = "", count = 0;

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  // The bottom of a screen as a tile shows it. Claude Code's input box goes: the last rule of "─" that sits directly above a
  // "❯" prompt line, and everything under it (the permissions footer too). Trailing blank lines go; any other screen keeps the rest.
  const RULE = /^\s*─{8,}/, PROMPT = /^\s*❯/;
  function bottom(text) {
    let lines = text.split("\n").map((l) => l.trimEnd());
    const p = lines.findLastIndex((l, i) => PROMPT.test(l) && i > 0 && RULE.test(lines[i - 1]));
    if (p > 0) lines = lines.slice(0, p - 1);
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    return lines.join("\n");
  }

  function make() {
    const t = { a: el("a", "tile"), name: el("span", "t-name"), st: el("span", "c-st"), word: el("span", "c-w"), team: el("span", "t-team"),
                scr: el("div", "t-scr"), pre: el("pre"), shown: null };
    t.st.append(el("i", "dot"), t.word);
    t.pre.setAttribute("aria-hidden", "true"); // the link is named by its label; the screen is for the eye
    t.scr.append(t.pre);
    const top = el("span", "t-top");
    top.append(t.name, t.st);
    t.a.append(top, t.team, t.scr);
    return t;
  }

  function fill(t, a, s) {
    const st = STATE(a), team = Object.hasOwn(s.teams || {}, a.team) ? s.teams[a.team] : "No team";
    t.a.className = `tile is-${st}`;
    t.st.className = `c-st is-${st}`;
    t.word.textContent = WORDS[st];
    t.name.textContent = t.a.title = a.name;
    t.team.textContent = team;
    t.a.setAttribute("aria-label", `${a.name}, ${team}, ${WORDS[st]}`);
    if (Object.hasOwn(TEAM, a.team)) t.a.style.setProperty("--tc", TEAM[a.team]); else t.a.style.removeProperty("--tc");
    if (linkable(a.name)) t.a.href = `/agent/${encodeURIComponent(a.name)}`; else t.a.removeAttribute("href");
    let words = null, shown = "";
    if (a.name === s.private) words = "Screen not shown";
    else shown = bottom(data.screens.get(a.name) || "");
    if (!words && !shown) words = "No screen text yet.";
    const key = words ? `n:${words}` : `t:${shown}`;
    if (key === t.shown) return;
    t.shown = key;
    t.scr.classList.toggle("is-note", Boolean(words));
    t.pre.textContent = words || shown;
  }

  // The column count that makes the tiles largest while all of them fit the window (a tile is judged by the biggest 3:2 box it
  // holds). If none keeps a tile at least 150 by 90, the tiles stop shrinking and the page scrolls: as many columns as are 150 wide.
  function fit(n) {
    const W = grid.clientWidth, H = innerHeight - (grid.getBoundingClientRect().top + scrollY) - PAD;
    let best = null;
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c), w = (W - (c - 1) * GAP) / c, h = (H - (r - 1) * GAP) / r;
      const size = Math.min(w / SHAPE, h);
      if (w >= MIN_W && h >= MIN_H && (!best || size > best.size)) best = { c, r, size };
    }
    const c = best ? best.c : Math.max(1, Math.floor((W + GAP) / (MIN_W + GAP)));
    grid.style.gridTemplateColumns = `repeat(${c}, minmax(0, 1fr))`;
    grid.style.gridTemplateRows = best ? `repeat(${best.r}, minmax(0, 1fr))` : "";
    grid.style.height = best ? `${H}px` : "";
    grid.style.gridAutoRows = best ? "" : `${Math.max(MIN_H, ((W - (c - 1) * GAP) / c) / SHAPE)}px`;
  }

  // The live agents in the office's order: teams in the structure file's order, then by kind, then by name.
  function draw() {
    const s = data.state;
    if (!s) return;
    const keys = Object.keys(s.teams || {});
    const place = (a) => (keys.includes(a.team) ? keys.indexOf(a.team) : keys.length);
    const live = s.agents.filter((a) => a.gone === false).sort((a, b) => place(a) - place(b) || rank(a) - rank(b) || byLabel(a, b));
    for (const a of live) {
      if (!tiles.has(a.name)) tiles.set(a.name, make());
      fill(tiles.get(a.name), a, s);
    }
    const names = new Set(live.map((a) => a.name));
    for (const k of [...tiles.keys()]) if (!names.has(k)) tiles.delete(k);
    const now = JSON.stringify(live.map((a) => a.name));
    if (now !== order) { // the nodes move only when the order does, so a poll never costs the keyboard its place
      order = now;
      grid.replaceChildren(...(live.length ? live.map((a) => tiles.get(a.name).a) : [el("p", "quiet", "No live agents.")]));
    }
    const n = (st) => live.filter((a) => STATE(a) === st).length;
    $("sum").replaceChildren(el("span", "", `${live.length} ${live.length === 1 ? "agent" : "agents"}, ${n("working")} working, ${n("blocked")} blocked`),
      ...(s.as_of ? [el("span", "", `As of ${ET.format(new Date(s.as_of)).replace(/ /g, " ")} ET`)] : []));
    count = live.length;
    fit(count);
  }

  // "Live, <time>" of the last hc snapshot, or "Offline, last <time>" while Atlas does not answer or hc or the screens read is not ok:
  // the tiles then keep the text they last had.
  function bar() {
    const s = data.state, source = (k) => (s ? (s.sources || []).find((x) => x.source === k) : null);
    const off = data.failing || [source("hc"), source("screens")].some((x) => x && x.state !== "ok");
    const t = s && s.as_of ? ET.format(new Date(s.as_of)).replace(/ /g, " ") : null;
    $("live").className = off ? "live off" : "live";
    $("live").replaceChildren(el("i"), off ? (t ? `Offline, last ${t}` : "Offline") : t ? `Live, ${t}` : "Reading");
  }

  // One state read and one screens read per poll, together. A read that fails changes nothing it holds and marks the page failing.
  async function read() {
    const [st, sc] = await Promise.allSettled([POLL.get("/api/state", DEADLINE_MS), POLL.get("/api/screens?text=1", DEADLINE_MS)]);
    data.failing = st.status !== "fulfilled" || sc.status !== "fulfilled";
    if (st.status === "fulfilled") data.state = st.value;
    if (sc.status === "fulfilled") data.screens = new Map(sc.value.screens.map((x) => [x.herdr_name, x.text]));
    draw();
    bar();
  }

  // No reads while the tab is hidden; the first read after it shows again is at once.
  let waiting = null;
  function every() {
    const t0 = Date.now();
    read().catch((e) => console.error(e)).finally(() => {
      waiting = setTimeout(() => { waiting = null; document.hidden ? (waiting = 0) : every(); }, Math.max(0, POLL_MS - (Date.now() - t0)));
    });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && waiting === 0) { waiting = null; every(); } });
  addEventListener("resize", () => fit(count));
  bar();
  every();
})();
