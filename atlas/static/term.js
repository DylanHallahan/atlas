// Atlas: the terminal view (spec 2026-10-31-terminal-view). One view for the office's popup and for an agent's own
// tab at /agent/<name>: the pane's screen in its own colours, read on demand once a second, following or held, with
// find, copy, wrap and the previous or next agent; on a wide screen its type fitted to the pane's real width. History
// (spec 2026-10-31-history-and-fit): the agent's transcript as a conversation, from /api/history. On an agent's own tab one of
// them shows at a time at full height (spec 2026-11-03-message-an-agent). Nothing here sends a key, a paste or anything
// else to a pane except through type.js, for Dylan only; find and copy are local to the page. It uses the office's helpers from app.js (office, label, role, word,
// since, when), so it loads first and calls them only once both files have run.
"use strict";

// Just over the service's 1 s cache, so each read the view asks for is a new one.
const TERM_READ_MS = 1050, TERM_STATE_MS = 5000, TERM_HISTORY_MS = 5000;
// Fit: the type between these sizes, so the pane's columns span the screen: on a wide screen in the office's popup, from
// 900 px in an agent's own tab, now across its full width.
const TERM_FIT_MIN = 13, TERM_FIT_MAX = 18;
// Longer than the service's 5 s herdr timeout, so a stalled herdr shows as a failed read, not as Atlas not answering.
const TERM_DEADLINE_MS = 6000;

// ---- Renderer: herdr's ANSI screen to lines of styled runs (no DOM; runs under node for the worker's checks) ----

// The 16 basic colours for the dark terminal, as the prototype set them.
const ANSI_COLOURS = ["#1d1f21", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d6d9df",
                      "#5c6370", "#ff7b86", "#b5e890", "#ffd68a", "#82c4ff", "#de95f0", "#7fd3dd", "#ffffff"];
const byte = (v) => Number.isInteger(v) && v >= 0 && v < 256;
const rgb = (r, g, b) => (byte(r) && byte(g) && byte(b) ? `rgb(${r},${g},${b})` : null);
function ansi256(n) {
  if (!byte(n)) return null;
  if (n < 16) return ANSI_COLOURS[n];
  if (n >= 232) return rgb(8 + (n - 232) * 10, 8 + (n - 232) * 10, 8 + (n - 232) * 10);
  const v = (x) => (x ? 55 + x * 40 : 0);
  return rgb(v(Math.floor((n - 16) / 36)), v(Math.floor((n - 16) / 6) % 6), v((n - 16) % 6));
}

// One escape or control at a time: a CSI sequence (only SGR, ending in "m", is used); a whole control string (OSC,
// DCS, SOS, PM or APC) up to its terminator; any other ESC sequence; or a C0 control other than tab. Everything
// matched is dropped from the text, a control string's payload with it.
const ANSI_CONTROL = /\x1b\[([0-9;:?<=>]*)[ -\/]*([@-~])|\x1b[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[ -\/]*[0-~]?|[\x00-\x08\x0b-\x1f\x7f]/g;

// The style after one SGR: colours (16, 256 and truecolor), bold, dim, italic, underline, inverse, and their resets.
function sgr(style, params) {
  let st = { ...style };
  const a = params === "" ? [0] : params.split(";").map(Number);
  for (let i = 0; i < a.length; i++) {
    const n = a[i];
    if (n === 0) st = {};
    else if (n === 1) st.b = 1;
    else if (n === 2) st.d = 1;
    else if (n === 3) st.i = 1;
    else if (n === 4) st.u = 1;
    else if (n === 7) st.inv = 1;
    else if (n === 22) { delete st.b; delete st.d; }
    else if (n === 23) delete st.i;
    else if (n === 24) delete st.u;
    else if (n === 27) delete st.inv;
    else if (n >= 30 && n <= 37) st.fg = ANSI_COLOURS[n - 30];
    else if (n >= 90 && n <= 97) st.fg = ANSI_COLOURS[n - 82];
    else if (n === 39) delete st.fg;
    else if (n >= 40 && n <= 47) st.bg = ANSI_COLOURS[n - 40];
    else if (n >= 100 && n <= 107) st.bg = ANSI_COLOURS[n - 92];
    else if (n === 49) delete st.bg;
    else if ((n === 38 || n === 48) && (a[i + 1] === 2 || a[i + 1] === 5)) {
      const c = a[i + 1] === 2 ? rgb(a[i + 2], a[i + 3], a[i + 4]) : ansi256(a[i + 2]);
      if (c) st[n === 38 ? "fg" : "bg"] = c;
      i += a[i + 1] === 2 ? 4 : 2;
    }
  }
  return st;
}

// The screen as lines: each its runs of text with one style, its plain text, and a key (the style it starts in and
// its raw text), so a refresh keeps every line whose key is unchanged. The style carries across lines.
function parseScreen(text) {
  let st = {};
  return text.replace(/\n$/, "").split("\n").map((raw) => {
    const key = `${JSON.stringify(st)}\u0000${raw}`, runs = [];
    let at = 0, m;
    ANSI_CONTROL.lastIndex = 0;
    while ((m = ANSI_CONTROL.exec(raw))) {
      if (m.index > at) runs.push({ text: raw.slice(at, m.index), style: st });
      if (m[2] === "m") st = sgr(st, m[1]);
      at = ANSI_CONTROL.lastIndex;
    }
    if (at < raw.length) runs.push({ text: raw.slice(at), style: st });
    return { key, runs, plain: runs.map((r) => r.text).join("") };
  });
}

// The screen as plain text, as Copy gives it and as herdr's plain read does: every escape and control dropped, each
// line without trailing white space, and a newline at the end.
const plainScreen = (text) => `${text.replace(/\n$/, "").split("\n").map((l) => l.replace(ANSI_CONTROL, "").trimEnd()).join("\n")}\n`;

// Find: the matches of q in one line, ignoring case, left to right and not overlapping, as [start, end].
function matchesIn(plain, q) {
  const out = [];
  if (!q) return out;
  let hay = plain.toLowerCase(), needle = q.toLowerCase();
  if (hay.length !== plain.length || needle.length !== q.length) [hay, needle] = [plain, q]; // keep offsets true
  for (let i = hay.indexOf(needle); i >= 0; i = hay.indexOf(needle, i + needle.length)) out.push([i, i + needle.length]);
  return out;
}

// A line made only of box-drawing rules (eight or more) and spaces: kept to one line when Wrap is on.
const isRule = (plain) => /^[ ─-╿]*$/.test(plain) && /[─-╿]{8}/.test(plain);

// One run of text in its style, as a DOM node: plain text when it has none. Also used by the home page's peek.
function styled(t, st) {
  let fg = st.fg, bg = st.bg;
  if (st.inv) [fg, bg] = [bg || "var(--tv-bg)", fg || "var(--tv-fg)"];
  if (!fg && !bg && !st.b && !st.d && !st.i && !st.u) return document.createTextNode(t);
  const span = document.createElement("span");
  span.textContent = t;
  if (fg) span.style.color = fg;
  if (bg) span.style.background = bg;
  if (st.b) span.style.fontWeight = "700";
  if (st.d) span.style.opacity = "0.62";
  if (st.i) span.style.fontStyle = "italic";
  if (st.u) span.style.textDecoration = "underline";
  return span;
}

if (typeof module !== "undefined") {
  module.exports = { sgr, ansi256, parseScreen, plainScreen, matchesIn, isRule, ANSI_COLOURS };
}

// ---- The view ----

// The view inside root. modal: the office's popup (Close, Escape, Open in tab, focus kept inside); otherwise the
// agent's own tab. close() is called by Close and Escape; move(name) by Prev and Next.
function termView(root, { modal = false, close = null, move }) {
  const phone = matchMedia("(max-width: 809px)");
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const button = (text, cls = "tv-btn") => Object.assign(el("button", cls, text), { type: "button" });

  // The bar: who, then the tools.
  const chip = el("span", "tv-chip"), name = el("span", "tv-name"), who = el("div", "tv-who");
  who.append(chip, name);
  const team = el("span", "tv-meta"), stateWord = el("span"), stateTime = el("span"), state = el("span", "tv-meta");
  state.append(stateWord, stateTime);
  const pane = el("span", "tv-meta tv-pane"), runs = el("span", "tv-meta tv-runs");
  const input = Object.assign(el("input"), { type: "text", placeholder: "Find", autocomplete: "off", spellcheck: false });
  input.setAttribute("aria-label", "Find in this terminal");
  const found = el("span", "tv-count"), find = el("label", "tv-find");
  const up = button("Prev", "tv-btn tv-step"), down = button("Next", "tv-btn tv-step");
  up.setAttribute("aria-label", "Previous match");
  down.setAttribute("aria-label", "Next match");
  find.append(input, found, up, down);
  // Screen | History, on an agent's own tab only (the office popup has no History, 7:19 PM ET): one shows at a time, at
  // full height. History opens, or the Screen when the agent is blocked on a question; his pick holds for the visit.
  const toScreen = button("Screen"), toHistory = button("History"), views = el("div", "tv-views");
  views.setAttribute("role", "group");
  views.setAttribute("aria-label", "View");
  views.append(toScreen, toHistory);
  const copy = button("Copy", "tv-btn tv-wide tv-copy"), wrap = button("Wrap", "tv-btn tv-wrap"), prev = button("Prev"), next = button("Next");
  prev.setAttribute("aria-label", "Previous agent");
  next.setAttribute("aria-label", "Next agent");
  const tab = Object.assign(el("a", "tv-btn tv-wide", "Open in tab"), { target: "_blank", rel: "noopener" });
  const shut = button("Close");
  const tools = el("div", "tv-tools");
  tools.append(...(modal ? [] : [views]), find, copy, wrap, ...(modal ? [tab] : []), prev, next, ...(modal ? [shut] : []));
  const bar = el("header", "tv-bar");
  bar.append(who, team, runs, state, pane, tools);

  // The screen, the jump pill over its lower right, and the status line.
  const lines = el("div", "tv-lines"), empty = el("p", "tv-empty"), screen = el("div", "tv-screen");
  screen.tabIndex = 0;
  screen.setAttribute("aria-label", "Screen");
  screen.append(lines, empty);
  const jump = button(""), latest = el("div", "tv-latest"), body = el("div", "tv-body");
  latest.append(jump);
  const earlier = button("Earlier", "tv-btn tv-earlier"), items = el("div", "tv-items"), note = el("p", "tv-empty");
  const hist = el("div", "tv-history");
  hist.tabIndex = 0;
  hist.hidden = true;
  hist.setAttribute("aria-label", "History");
  // History's head: "Replies only" (hides the tool rows, remembered per browser), and the clay line for a question.
  const repliesBtn = button("Replies only", "tv-btn tv-replies"), hhead = el("div", "tv-hhead");
  const wait = el("div", "tv-wait"), waitText = el("span"), showScreen = button("Show the Screen");
  wait.hidden = true;
  wait.append(waitText, showScreen);
  hhead.append(repliesBtn, wait);
  hist.append(hhead, earlier, items, note);
  body.append(screen, hist, latest);
  const status = el("footer", "tv-status");
  // The one agent whose terminal is never shown (state.private): the bar stays, a line takes the place of the screen.
  const priv = el("p", "tv-empty tv-private");
  priv.append("This agent handles outside messages, so its terminal is not shown here. Its contacts are on the ");
  Object.assign(priv.appendChild(el("a")), { href: "/office", textContent: "org chart" });
  priv.append(".");
  root.classList.add("tv");
  root.replaceChildren(bar, priv, body, status);
  // The message box (type.js), for Dylan only; absent where type.js is not loaded.
  // Made when an agent that shows a terminal is first opened, never for the private one.
  let typer = null;
  // What the box says of its agent: role, team and whether it is working (a line sent then waits for its next step).
  const about = () => {
    const a = s ? s.agents.find((x) => x.name === agent) : null;
    return { role: a ? role(a) : "", team: a && s.teams && Object.hasOwn(s.teams, a.team) ? s.teams[a.team] : "", working: Boolean(a && a.gone === false && a.state === "working") };
  };

  let agent = null, s = null;              // the agent shown, by name, and the last /api/state
  let readPane = null, run = 0, timer = 0, abort = null;
  let text = null, readAt = null;          // the screen held, as herdr sent it, and when it was read
  let opened = null, answered = null;      // when the view opened this agent, and when Atlas last answered
  let atlasDown = false, readFailing = false, notLive = false, askedHeld = false;
  let rows = [];                           // the lines shown: { key, runs, plain, el, q, marks }
  let follow = true, seen = null;          // following the latest, or held (seen: the lines there when it held)
  let query = "", marks = [], current = null;
  let mode = "screen", cols = 0;           // the view shown, and the widest line the agent drew, in characters
  let picked = false, settled = false;     // his own pick of a view holds for the visit; settled: the opening view was chosen from the agent's state
  let replies = false;                     // Replies only: History without its tool rows
  const showsScreen = () => mode === "screen", showsHist = () => mode === "history";
  // History: its rows (as the screen's, one per item's text), the cursor for Earlier, and its own poll.
  let hRows = [], hEarlier = null, hSession = null, hNone = false, hReadAt = null, hFailing = false, hRun = 0, hTimer = 0;
  let hStick = true, hOn = false;          // History: at its bottom (so it follows), and being read
  // ---- Lines: only lines whose key changed are built; the rest stay in place ----

  function paintRow(row) {
    const ranges = matchesIn(row.plain, query), nodes = [];
    row.q = query;
    row.marks = [];
    let pos = 0, r = 0;
    for (const seg of row.runs) {
      for (let i = 0; i < seg.text.length;) {
        const m = ranges[r], at = pos + i;
        if (m && at >= m[0]) {
          // A match is one mark, in the find colours, even where it crosses runs of different styles.
          if (at === m[0]) {
            const mark = el("mark", null, row.plain.slice(m[0], m[1]));
            row.marks.push(mark);
            nodes.push(mark);
          }
          i = Math.min(seg.text.length, m[1] - pos);
          if (pos + i >= m[1]) r++;
          continue;
        }
        const end = m ? Math.min(seg.text.length, m[0] - pos) : seg.text.length;
        nodes.push(styled(seg.text.slice(i, end), seg.style));
        i = end;
      }
      pos += seg.text.length;
    }
    row.el.replaceChildren(...nodes);
  }

  function render() {
    // Held: the top line in view stays where it is, if it is still on the screen.
    const anchor = follow ? null : rows.find((r) => r.el.offsetTop + r.el.offsetHeight > screen.scrollTop);
    const anchorAt = anchor ? anchor.el.offsetTop - screen.scrollTop : 0;
    const pool = new Map();
    for (const r of rows) pool.set(r.key, [...(pool.get(r.key) || []), r]);
    rows = (text == null ? [] : parseScreen(text)).map((p) => {
      const kept = pool.get(p.key)?.shift();
      if (kept) return kept;
      const row = { ...p, el: el("div", isRule(p.plain) ? "tv-ln is-rule" : "tv-ln") };
      paintRow(row);
      return row;
    });
    const keep = new Set(rows.map((r) => r.el));
    for (const c of [...lines.children]) if (!keep.has(c)) c.remove();
    rows.forEach((r, i) => { if (lines.children[i] !== r.el) lines.insertBefore(r.el, lines.children[i] || null); });
    refind();
    // The pane's real width is the widest line the agent drew: herdr's layout can differ from the terminal's size.
    const widest = Math.max(0, ...rows.map((r) => [...r.plain].length));
    if (widest !== cols) {
      cols = widest;
      fit();
    }
    if (follow) toEnd();
    else if (anchor && anchor.el.isConnected) screen.scrollTop = anchor.el.offsetTop - anchorAt;
  }

  // ---- Find ----

  function refind() {
    // The screen's matches, then History's, in that fixed order.
    const shown = [...(showsScreen() ? rows : []), ...(showsHist() ? hRows.filter((r) => !(replies && r.tool)) : [])];
    for (const r of shown) if (r.q !== query) paintRow(r);
    marks = shown.flatMap((r) => r.marks);
    if (!marks.includes(current)) current = null;
    found.textContent = !query ? "" : marks.length ? `${marks.length} ${marks.length === 1 ? "match" : "matches"}` : "no matches";
    find.classList.toggle("has-query", Boolean(query));
  }

  function go(i) {
    if (current) current.classList.remove("is-current");
    current = marks.length ? marks[(i + marks.length) % marks.length] : null;
    if (!current) return;
    current.classList.add("is-current");
    current.scrollIntoView({ block: "center", inline: "nearest" });
  }

  input.addEventListener("input", () => {
    query = input.value;
    refind();
    go(marks.length - 1); // the match nearest the latest
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    go(marks.indexOf(current) + (e.shiftKey ? -1 : 1));
  });
  up.addEventListener("click", () => go(marks.indexOf(current) - 1));
  down.addEventListener("click", () => go(marks.indexOf(current) + 1));

  // ---- Following and held ----

  // Lines below the bottom of the view, wholly or in part.
  const below = () => {
    const bottom = screen.scrollTop + screen.clientHeight;
    let n = 0;
    for (let i = rows.length - 1; i >= 0 && rows[i].el.offsetTop + rows[i].el.offsetHeight > bottom + 1; i--) n++;
    return n;
  };
  // The view's own moves to the bottom. Its scroll event comes a frame later, perhaps after the layout has changed
  // (the font arriving), so an event at the place the view set is its own: it stays following and pins again.
  let pinnedAt = null;
  function toEnd() {
    screen.scrollTop = screen.scrollHeight;
    pinnedAt = screen.scrollTop;
  }
  screen.addEventListener("scroll", () => {
    if (follow && screen.scrollTop === pinnedAt) return toEnd();
    const atEnd = screen.scrollHeight - screen.scrollTop - screen.clientHeight < 4;
    if (atEnd !== follow) {
      follow = atEnd;
      seen = follow ? null : new Set(rows.map((r) => r.el));
    }
    paint();
  });
  jump.addEventListener("click", toEnd);
  // A change of size (the font arriving, Wrap, a resized window) keeps a following view at the bottom.
  const pin = new ResizeObserver(() => { if (follow) toEnd(); });
  pin.observe(lines);
  pin.observe(screen);

  // ---- The tools ----

  function setWrap(on) {
    root.classList.toggle("is-wrap", on);
    wrap.classList.toggle("is-on", on);
    wrap.setAttribute("aria-pressed", String(on));
    if (follow) toEnd();
  }
  setWrap(phone.matches);
  wrap.addEventListener("click", () => setWrap(!root.classList.contains("is-wrap")));

  copy.addEventListener("click", () => {
    const out = !showsScreen() ? hRows.map((r) => `${r.head}\n${r.plain}\n`).join("\n") : text == null ? null : plainScreen(text);
    if (!out) return;
    const say = (w) => { copy.textContent = w; setTimeout(() => { copy.textContent = "Copy"; }, 1500); };
    navigator.clipboard.writeText(out).then(() => say("Copied"), () => say("Copy failed"));
  });

  // Prev and Next: the live agents in the office's seating order, wrapping at the ends.
  function step(d) {
    if (!s) return;
    const names = office(s).carpets.flatMap((c) => c.agents.map((a) => a.name)), i = names.indexOf(agent);
    if (!names.length) return;
    move(names[i < 0 ? (d > 0 ? 0 : names.length - 1) : (i + d + names.length) % names.length]);
  }
  prev.addEventListener("click", () => step(-1));
  next.addEventListener("click", () => step(1));
  shut.addEventListener("click", () => close());

  // Keys: Escape closes the popup, Tab stays inside it, and [ and ] step unless a text field has focus.
  // None of them goes anywhere but this page.
  document.addEventListener("keydown", (e) => {
    if (agent == null) return;
    if (e.key === "Escape" && modal) {
      e.preventDefault();
      close();
    } else if (e.key === "Tab" && modal) {
      const stops = [...root.querySelectorAll("input, button, a[href], [tabindex='0']")].filter((x) => x.offsetParent !== null);
      const i = stops.indexOf(document.activeElement);
      e.preventDefault();
      stops[(i + (e.shiftKey ? stops.length - 1 : 1)) % stops.length].focus();
    } else if ((e.key === "[" || e.key === "]") && !(e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      step(e.key === "]" ? 1 : -1);
    }
  });

  // ---- Fit: the type grows so the pane's columns span the screen, within 13 to 18 px ----

  const wide = matchMedia(modal ? "(min-width: 1280px)" : "(min-width: 900px)");
  function fit() {
    if (!wide.matches || screen.hidden || !cols) {
      screen.style.fontSize = "";
      return;
    }
    // The widest line's natural width at 20 px, measured each time, so a late font and wide characters count too.
    screen.style.fontSize = "20px";
    Object.assign(lines.style, { width: "max-content", minWidth: "0" });
    const natural = lines.getBoundingClientRect().width;
    Object.assign(lines.style, { width: "", minWidth: "" });
    const cs = getComputedStyle(screen), pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    let px = Math.min(TERM_FIT_MAX, Math.max(TERM_FIT_MIN, natural ? Math.floor((20 * (screen.clientWidth - pad) / natural) * 10) / 10 : TERM_FIT_MIN));
    screen.style.fontSize = `${px}px`;
    // Rounding can leave a hair over: step down until nothing scrolls sideways. A pane too wide for 13 px keeps 13 px
    // and scrolls as before.
    while (px > TERM_FIT_MIN && screen.scrollWidth > screen.clientWidth) {
      px = Math.max(TERM_FIT_MIN, Math.round((px - 0.2) * 10) / 10);
      screen.style.fontSize = `${px}px`;
    }
    if (follow) toEnd();
  }
  wide.addEventListener("change", fit);
  new ResizeObserver(() => fit()).observe(root);
  document.fonts.ready.then(() => fit());

  // ---- History: the transcript as a conversation, every 5 s while it is shown, newest at the bottom ----

  const HM = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  const HD = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" });
  const hTime = (iso) => {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "";
    const t = HM.format(d).replace(/\u202f/g, " ");
    return HD.format(d) === HD.format(new Date()) ? t : `${HD.format(d)}, ${t}`;
  };
  const after = (a, b) => a[0] > b[0] || (a[0] === b[0] && a[1] > b[1]);
  const hAtEnd = () => hist.scrollHeight - hist.scrollTop - hist.clientHeight < 4;

  // One item: a message received (who sent it, and when), a reply, or a tool call on one line. Text only, as nodes.
  function hRow(it) {
    const box = el("div", `tv-item is-${it.kind}`), t = hTime(it.at);
    const head = it.kind === "in" ? `${it.who || "typed in the terminal"}, ${t}` : it.kind === "out" ? `reply, ${t}` : t;
    const txt = el("div", "tv-item-text");
    box.append(el("div", "tv-item-head", head), txt);
    if (it.mine) box.classList.add("is-mine");
    const row = { id: it.id, head, tool: it.kind === "tool", runs: [{ text: it.text, style: {} }], plain: it.text, el: txt, box, q: null, marks: [] };
    paintRow(row);
    return row;
  }

  function drawHistory(next, place) {
    const keepAt = place === "top" ? hist.scrollHeight - hist.scrollTop : null, wasEnd = hStick;
    hRows = next;
    items.replaceChildren(...hRows.map((r) => r.box));
    earlier.hidden = hEarlier == null;
    note.hidden = !hNone && hRows.length > 0;
    note.textContent = hNone ? "No history for this agent: Atlas reads Claude and Codex transcripts only." : "Not read yet";
    refind();
    if (keepAt != null) hist.scrollTop = hist.scrollHeight - keepAt;
    else if (place === "end" || wasEnd) { hStick = true; hToEnd(); }
  }
  // As the screen does: a scroll event at the place History itself set is its own (it may arrive after a reflow), so it
  // stays at the bottom and pins again; any other scroll is Dylan's.
  let hPinned = null;
  function hToEnd() {
    hist.scrollTop = hist.scrollHeight;
    hPinned = hist.scrollTop;
  }
  // History follows new items only while it is at the bottom, also when its width changes (the screen column fitting).
  hist.addEventListener("scroll", () => {
    if (hStick && hist.scrollTop === hPinned) return hToEnd();
    hStick = hAtEnd();
  });
  new ResizeObserver(() => { if (hStick && !hist.hidden) hToEnd(); }).observe(items);
  new ResizeObserver(() => { if (hStick && !hist.hidden) hToEnd(); }).observe(hist);

  async function getHistory(before) {
    const q = `/api/history?agent=${encodeURIComponent(agent)}${before != null ? `&before=${before}` : ""}`;
    if (before == null) return POLL.get(q, TERM_DEADLINE_MS); // the poll; an Earlier page is a one-off read
    const res = await fetch(q, { cache: "no-store", signal: AbortSignal.timeout(TERM_DEADLINE_MS) });
    if (!res.ok) throw new Error(`${q}: ${res.status}`);
    return res.json();
  }

  // The latest 200: new items join at the bottom; a new session, or a gap wider than one answer, starts over.
  async function readHistory(r, first) {
    try {
      const b = await getHistory(null);
      if (r !== hRun) return;
      hFailing = false;
      hReadAt = new Date().toISOString();
      hNone = Boolean(b.none);
      const last = hRows.length ? hRows[hRows.length - 1].id : null;
      const overlap = last && b.session === hSession && (b.items.length === 0 || !after(b.items[0].id, last) || b.earlier == null);
      if (first || !overlap) {
        hSession = b.session || null;
        hEarlier = b.earlier;
        drawHistory(b.items.map(hRow), "end");
      } else {
        const fresh = b.items.filter((it) => after(it.id, last));
        if (fresh.length) drawHistory([...hRows, ...fresh.map((it) => { const r = hRow(it); r.box.classList.add("is-new"); r.box.addEventListener("animationend", () => r.box.classList.remove("is-new"), { once: true }); return r; })]);
      }
    } catch (e) {
      if (r === hRun) hFailing = true;
    } finally {
      if (r === hRun) paint();
    }
  }

  function startHistory() {
    if (hidden()) return;
    const r = ++hRun;
    hOn = true;
    let first = true;
    const loop = async () => {
      const t0 = Date.now();
      await readHistory(r, first);
      first = false;
      if (r === hRun) hTimer = setTimeout(loop, Math.max(0, TERM_HISTORY_MS - (Date.now() - t0)));
    };
    loop();
  }
  function stopHistory() { hRun++; hOn = false; clearTimeout(hTimer); }

  // Earlier: the 200 before the oldest shown, above it, the place in view kept.
  earlier.addEventListener("click", async () => {
    const r = hRun, oldest = hRows.length ? hRows[0].id : null;
    earlier.disabled = true;
    try {
      const b = await getHistory(hEarlier);
      if (r !== hRun || b.session !== hSession) return;
      hEarlier = b.earlier;
      drawHistory([...b.items.filter((it) => !oldest || after(oldest, it.id)).map(hRow), ...hRows], "top");
    } catch (e) {
      console.error(e);
    } finally {
      earlier.disabled = false;
    }
  });

  // What shows: the screen or History, one at a time; History is read while it shows.
  function layout() {
    screen.hidden = !showsScreen();
    hist.hidden = !showsHist();
    input.setAttribute("aria-label", showsHist() ? "Find in this history" : "Find in this terminal");
    if (showsHist() && !hOn) {
      hRows = [];
      hEarlier = hSession = hReadAt = null;
      hNone = hFailing = false;
      hStick = true;
      drawHistory([], "end");
      startHistory();
    } else if (!showsHist() && hOn) stopHistory();
    refind();
  }
  function setMode(m) {
    mode = m;
    for (const [b, on] of [[toScreen, m === "screen"], [toHistory, m === "history"]]) {
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", String(on));
    }
    if (current) current.classList.remove("is-current");
    current = null;
    if (typer && m !== "screen") typer.leave();
    layout();
    fit();
    paint();
  }
  toScreen.addEventListener("click", () => { picked = true; if (mode !== "screen") setMode("screen"); });
  toHistory.addEventListener("click", () => { picked = true; if (mode !== "history") setMode("history"); });
  showScreen.addEventListener("click", () => { picked = true; setMode("screen"); });

  // Replies only: the tool rows go, his lines, other senders' messages and the replies stay. Remembered per browser,
  // wrapped so the page works without storage.
  const REPLIES_KEY = "atlas.history.replies-only";
  function setReplies(on, keep) {
    replies = on;
    root.classList.toggle("is-replies", on);
    repliesBtn.classList.toggle("is-on", on);
    repliesBtn.setAttribute("aria-pressed", String(on));
    if (keep) try { localStorage.setItem(REPLIES_KEY, on ? "1" : "0"); } catch { /* the page works without storage */ }
    refind();
    if (hStick && !hist.hidden) hToEnd();
  }
  repliesBtn.addEventListener("click", () => setReplies(!replies, true));
  let remembered = false;
  try { remembered = localStorage.getItem(REPLIES_KEY) === "1"; } catch { /* no storage */ }
  setReplies(remembered, false);

  // ---- Reading the pane: once a second, start to start, while the agent is live ----

  function stop() {
    run++;
    clearTimeout(timer);
    if (abort) abort.abort();
    abort = null;
  }

  function start() {
    const r = ++run, url = `/api/screens?pane=${encodeURIComponent(readPane)}&format=ansi`;
    const loop = async () => {
      const t0 = Date.now();
      await readOnce(url, r);
      if (r === run) timer = setTimeout(loop, Math.max(0, TERM_READ_MS - (Date.now() - t0)));
    };
    loop();
  }

  async function readOnce(url, r) {
    const ctl = (abort = new AbortController());
    try {
      const b = await POLL.get(url, TERM_DEADLINE_MS, ctl.signal).catch((e) => { if (e.status === 404) return null; throw e; });
      if (r !== run) return;
      atlasDown = false;
      answered = new Date().toISOString();
      // Not in herdr's latest snapshot, or the pane now holds another agent: shown as not live, the screen kept.
      notLive = !b || (b.herdr_name != null && b.herdr_name !== agent);
      if (notLive) return;
      readFailing = Boolean(b.error);
      if (b.text != null && b.text !== text) {
        text = b.text;
        readAt = b.read_at;
        render();
      } else if (b.text != null) readAt = b.read_at;
    } catch (e) {
      if (r === run) atlasDown = true;
    } finally {
      if (r === run) paint();
    }
  }

  // ---- The bar and the status line, from the state and the read ----

  const hidden = () => agent != null && agent === ((s && s.private) || document.body.dataset.private);
  function paint() {
    root.classList.toggle("is-private", hidden());
    const now = Date.now(), a = s ? s.agents.find((x) => x.name === agent) : null;
    if (typer) typer.allow(!hidden());
    const live = Boolean(a && a.gone === false && !notLive);
    const asked = live && a.state === "blocked";
    wait.hidden = !(asked && showsHist() && !modal);
    waitText.textContent = `${agent} is waiting on a question in its terminal`;
    if (typer) typer.update(asked);
    name.textContent = a ? label(a) : agent || "";
    root.dataset.team = (a && a.team) || "";
    root.setAttribute("aria-label", `Terminal of ${name.textContent}`);
    const teamName = a && s.teams && Object.hasOwn(s.teams, a.team) ? s.teams[a.team] : "No team";
    team.textContent = a ? (role(a) ? `${teamName}, ${role(a)}` : teamName) : "";
    runs.textContent = a && a.runs_on ? a.runs_on : "";
    stateWord.textContent = !s ? "" : live ? word(a) : "Not live";
    stateWord.className = !s ? "" : live ? `is-${a.state}` : "is-gone";
    stateTime.textContent = live && since(a, now) ? `, ${since(a, now)}` : "";
    pane.textContent = a && a.pane ? a.pane : "";
    tab.href = `/agent/${encodeURIComponent(agent || "")}`;

    empty.hidden = rows.length > 0;
    empty.textContent = s && !live ? "No screen held" : "Not read yet";

    const line = [];
    const item = (t, cls) => line.push(el("span", cls, t));
    if (!showsScreen()) {
      item("History", "is-on");
      if (hRows.length) item(`${hRows.length} ${hRows.length === 1 ? "item" : "items"}`);
      item("messages, replies and tool calls from the transcript; no tool results", "tv-wide");
      if (hFailing) item(`History has not been read since ${when(hReadAt || opened)}. It shows its last read.`, "is-warn");
      if (hReadAt) item(`read ${when(hReadAt, true)}`, "tv-right");
      status.replaceChildren(...line);
      latest.hidden = true;
      return;
    }
    if (s && !live) item("Not live", "is-warn");
    else if (follow) item("Following", "is-on");
    else {
      const n = below();
      item(`Held, ${n} ${n === 1 ? "line" : "lines"} above the latest`, "is-warn");
    }
    if (rows.length) item(`${rows.length} ${rows.length === 1 ? "line" : "lines"}`);
    item("scrollback: this screen (the agent draws on the alternate screen)", "tv-wide");
    item("screen only", "tv-narrow");
    // The office's failure notices, word for word; the screen keeps its last read.
    if (atlasDown) item(`Atlas has not answered since ${when(answered)}. The screen shows its last read.`, "is-warn");
    else if (live && readFailing) item(`Screens have not been read since ${when(readAt || opened)}. This screen shows its last read.`, "is-warn");
    if (readAt) item(`read ${when(readAt, true)}`, "tv-right");
    status.replaceChildren(...line);

    const fresh = seen ? rows.filter((r) => !seen.has(r.el) && r.plain.trim()).length : 0;
    latest.hidden = follow || !rows.length;
    jump.textContent = fresh ? `Jump to latest, ${fresh} new ${fresh === 1 ? "line" : "lines"}` : "Jump to latest";
  }

  // A new /api/state: the bar's words, and the pane to read, which follows the agent if herdr moves it.
  function update(state) {
    s = state;
    const a = s && s.agents.find((x) => x.name === agent && x.gone === false);
    // The first state that knows this agent picks the view it opens on, unless he has already picked: the Screen when it
    // is blocked on a question, History otherwise.
    if (!modal && !settled && a) {
      settled = true;
      if (!picked && a.state === "blocked" && mode !== "screen") setMode("screen");
    }
    const want = a && a.pane && !hidden() ? a.pane : null;
    if (want !== readPane) {
      stop();
      // A move to another pane starts from that pane's screen; leaving herdr keeps the last screen held.
      if (readPane && want) {
        text = readAt = null;
        render();
      }
      readPane = want;
      notLive = readFailing = false;
      if (want) start();
    }
    // Not live, with no screen of its own: the last screen Atlas holds for this agent, asked once, never a pane read.
    if (!want && s && !hidden() && text == null && !askedHeld) {
      askedHeld = true;
      readHeld();
    }
    paint();
  }

  async function readHeld() {
    const r = run;
    try {
      const res = await fetch(`/api/screens?agent=${encodeURIComponent(agent)}&format=ansi`, { cache: "no-store", signal: AbortSignal.timeout(TERM_DEADLINE_MS) });
      const b = res.ok ? await res.json() : null;
      if (r !== run || text != null || !b || b.text == null) return;
      text = b.text;
      readAt = b.read_at;
      render();
      paint();
    } catch (e) {
      console.error(e);
    }
  }

  function open(n, state) {
    stop();
    stopHistory();
    agent = n;
    if (!typer && !hidden() && typeof typeBox === "function") typer = typeBox({ root, status, screen, el, button, agent: () => agent, about,
      showScreen: () => { picked = true; if (mode !== "screen") setMode("screen"); } });
    if (typer) typer.reset();
    readPane = null;
    text = readAt = null;
    opened = answered = new Date().toISOString();
    atlasDown = readFailing = notLive = askedHeld = false;
    follow = true;
    seen = null;
    render();
    picked = settled = false;
    setMode(modal ? "screen" : "history");
    update(state);
  }

  return {
    open,
    state: update,
    close() { stop(); stopHistory(); agent = readPane = null; },
    focus() { screen.focus({ preventScroll: true }); },
  };
}

// ---- An agent's own tab: /agent/<name>, the name read from the address ----

if (typeof document !== "undefined" && document.body.classList.contains("tv-page")) {
  addEventListener("DOMContentLoaded", () => {
    const nameOf = () => {
      const raw = location.pathname.slice("/agent/".length);
      try { return decodeURIComponent(raw); } catch { return raw; }
    };
    let state = null;
    const show = () => {
      document.title = `${nameOf()} (Atlas)`;
      view.open(nameOf(), state);
    };
    const view = termView(document.getElementById("term"), {
      move: (n) => { history.pushState(null, "", `/agent/${encodeURIComponent(n)}`); show(); },
    });
    addEventListener("popstate", show);
    // The state, for the bar, the pane and the seating order: every 5 s, start to start; a failed read keeps the last.
    const poll = async () => {
      const t0 = Date.now();
      try {
        // Its own agent is included even when gone; the other agents are the live ones.
        state = await POLL.get(`/api/state?agent=${encodeURIComponent(nameOf())}`, TERM_DEADLINE_MS);
        view.state(state);
      } catch (e) {
        console.error(e);
      }
      setTimeout(poll, Math.max(0, TERM_STATE_MS - (Date.now() - t0)));
    };
    show();
    view.focus();
    poll();
  });
}
