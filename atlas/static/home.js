// Atlas: the home page (spec 2026-10-31-home-briefing). In one glance: what changed since the last look, what needs
// Dylan, and who is working on what in the agents' own words; one tap to an ask, an agent's live screen or the thing
// shipped. Read-only: nothing here sends a key to a pane; the only write is the last look (POST /api/seen).
// It uses md.js for a team's words, term.js's renderer (parseScreen, styled, isRule) and app.js's helpers (label, word,
// since, rank); every value from the API reaches the page as text.
"use strict";

if (document.getElementById("home")) {
  const STATE_MS = 5000, BRIEF_MS = 5000, PEEK_MS = 2000, REVIEW_MS = 30000, USAGE_MS = 60000, NEAR = 80, READ_DEADLINE_MS = 4000, ASLEEP_S = 1800;
  const LOOK_KEY = "atlas.lastLook"; // the last look in this browser, used only when a request carries no login
  const $ = (id) => document.getElementById(id);
  const { el, line, links } = MD; // md.js: a team's words as nodes
  const params = new URLSearchParams(location.search);
  const teamsView = location.pathname === "/teams"; // the same page and code draw Teams: the cards, without the greeting
  if (teamsView) {
    document.title = "Teams - Atlas";
    document.querySelector('.tabs a[aria-current]')?.removeAttribute("aria-current");
    document.querySelector('.tabs a[href="/teams"]')?.setAttribute("aria-current", "page");
  }

  // ---- Words and times ----

  const T = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  const WD = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short" });
  const D8 = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
  const HOUR = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", hourCycle: "h23" });
  // "10:31 PM", with the weekday in front when it is not today in ET.
  const tm = (iso) => {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "";
    const t = T.format(d).replace(/ /g, " ");
    return D8.format(d) === D8.format(new Date()) ? t : `${WD.format(d)} ${t}`;
  };
  const ago = (iso) => {
    const s = (Date.now() - Date.parse(iso)) / 1000;
    return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
  const dur = (iso) => {
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    return s < 60 ? `${Math.max(1, Math.floor(s))} s` : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`;
  };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  // A "." or ".." segment would be resolved away by the browser and open another page, so such an ask gets no link.
  const askHref = ASKS.href;

  const isAsk = (w) => !!w && !/^\s*nothing\b/i.test(w);
  // ---- The mascot: Anthropic's Clawd grid, lit when working, faded when idle, fainter when asleep ----

  const GRID = ["..ooooooooooooo..", "..ooooooooooooo..", "..oooeoooooooeo..", "..oooeoooooooeo..", "ooooooooooooooooo",
                "ooooooooooooooooo", "..ooooooooooooo..", "..ooooooooooooo..", "..o.o.......o.o..", "..o.o.......o.o.."];
  const pathOf = (ch) => GRID.flatMap((r, y) => [...r].map((c, x) => (c === ch ? `M${x} ${y}h1v1h-1z` : ""))).join("");
  const BODY = pathOf("o"), EYES = pathOf("e");
  const hash = (s) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);
  const pose = (a) => (a.state === "working" || a.state === "blocked" ? a.state : a.state === "idle" && a.state_since && (Date.now() - Date.parse(a.state_since)) / 1000 >= ASLEEP_S ? "asleep" : "idle");
  // The greeting's own mascot (mascot.js): one canvas kept across redraws, so a poll never restarts it.
  const mascotCv = el("canvas", "mascot");
  mascotCv.setAttribute("aria-hidden", "true");
  const mascot = Mascot.make(mascotCv);
  const clawd = (a) => {
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", `clawd ${a ? pose(a) : "working"}`);
    svg.setAttribute("viewBox", "0 -1 17 11");
    svg.setAttribute("shape-rendering", "crispEdges");
    svg.setAttribute("aria-hidden", "true");
    svg.style.setProperty("--d", `${(hash(a ? a.name : "atlas") % 50) / 10}s`);
    for (const [cls, d] of [["b", BODY], ["e", EYES]]) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("class", cls);
      p.setAttribute("d", d);
      svg.append(p);
    }
    return svg;
  };

  // ---- Data: /api/state and /api/briefing, each every 5 s, start to start ----

  const data = { state: null, brief: null, review: null, usage: null };
  // Each endpoint's last read; good: the state's as_of at the last moment both reads had succeeded. A state read
  // whose herdr source is not ok counts as failed: its states are herdr's last snapshot, not live.
  const live = { state: null, brief: null, good: null };
  const mark = (key, ok) => {
    live[key] = ok;
    if (live.state && live.brief) live.good = data.state.as_of || new Date().toISOString();
  };
  const storedLook = () => { try { return localStorage.getItem(LOOK_KEY); } catch { return null; } };
  const storeLook = (iso) => { try { localStorage.setItem(LOOK_KEY, iso); } catch { /* storage blocked: the server's 24 h */ } };

  const get = (url) => POLL.get(url, READ_DEADLINE_MS); // poll.js: the ETag of the last answer goes back, a 304 reuses it
  const readState = () => get("/api/state").then((s) => {
    data.state = s;
    const herdr = (s.sources || []).find((x) => x.source === "hc");
    if (herdr && herdr.state !== "ok" && !live.good) live.good = herdr.last_ok_at; // failing since before this page opened
    mark("state", !herdr || herdr.state === "ok");
  }, () => mark("state", false));
  // The review queue's count, the same number as /review's heading; a failed read hides the line, never shows a stale one.
  const readReview = () => get("/api/review?count=1").then((r) => { data.review = r.waiting_for_review; }, () => { data.review = null; });
  // The fullest limit window at 80% or more, as one quiet line; a failed read hides it, never shows a stale one. The
  // server caches each account's read for 5 minutes, so this asks Anthropic no more often than /usage does.
  const readUsage = () => get("/api/usage").then((u) => {
    const live = u.accounts.filter((a) => a.state === "ok").flatMap((a) => a.windows.map((w) => ({ ...w, account: a.name })))
      .filter((w) => w.percent >= NEAR && Date.parse(w.resets_at) > Date.now()).sort((x, y) => y.percent - x.percent);
    const wname = (w) => w.label.replace(/^Weekly/, "weekly");
    // Every account at the limit, each by its fullest window, in one line.
    const full = [...new Map(live.filter((w) => w.percent >= 100).map((w) => [w.account, w])).values()];
    const at = (w) => `${tm(w.resets_at)}\u00a0ET`;
    if (full.length > 1) {
      const names = full.map((w) => w.account).join(" and "), one = new Set(full.map(wname)).size === 1;
      data.usage = !one ? full.map((w) => `${w.account} Claude at its ${wname(w)} limit until ${at(w)}`).join("; ")
        : new Set(full.map(at)).size === 1 ? `${names} Claude at their ${wname(full[0])} limits until ${at(full[0])}`
        : `${names} Claude at their ${wname(full[0])} limits, ${full.map((w) => `${w.account} until ${at(w)}`).join(", ")}`;
      return;
    }
    const hot = live[0];
    data.usage = !hot ? null : hot.percent >= 100 ? `${hot.account} Claude at its ${wname(hot)} limit until ${at(hot)}`
      : `${hot.account} Claude at ${Math.round(hot.percent)}% of its ${wname(hot)} window, resets ${at(hot)}`;
  }, () => { data.usage = null; });
  // Without a login the server counts from this browser's last look; with one, its own record wins.
  const readBrief = () => {
    const since = storedLook();
    return get(`/api/briefing${since ? `?since=${encodeURIComponent(since)}` : ""}`).then((b) => { data.brief = b; mark("brief", true); }, () => mark("brief", false));
  };

  // Mark all seen, and leaving the page: the last look is now, on the server for a login, else in this browser.
  function seen(leaving) {
    const login = data.brief && data.brief.login;
    if (!login) { storeLook(new Date().toISOString()); return Promise.resolve(); }
    if (leaving) { navigator.sendBeacon("/api/seen"); return Promise.resolve(); }
    return fetch("/api/seen", { method: "POST", signal: AbortSignal.timeout(READ_DEADLINE_MS) }).catch(() => {});
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") seen(true); });

  // ---- The model the page draws ----

  const agents = () => (data.state ? data.state.agents.filter((a) => a.gone === false) : []);
  const teamName = (key) => (data.state && data.state.teams && Object.hasOwn(data.state.teams, key) ? data.state.teams[key] : key || "No team");
  const byRank = (a, b) => rank(a) - rank(b) || (label(a) < label(b) ? -1 : label(a) > label(b) ? 1 : 0);
  // The teams in the briefing's order (teams that need Dylan first), else the structure file's, for At work's rows.
  const teamOrder = () => (data.brief ? data.brief.teams.map((t) => t.key) : Object.keys((data.state && data.state.teams) || {}));
  const working = () => {
    const order = teamOrder();
    return agents().filter((a) => a.state === "working")
      .sort((a, b) => (order.indexOf(a.team) + 1 || 99) - (order.indexOf(b.team) + 1 || 99) || byRank(a, b));
  };
  // The rows of Needs you, team by team in the briefing's order: each team's asks. A blocked agent is its parent's to
  // clear, so it is not here.
  const needs = () => {
    if (!data.brief) return [];
    return ASKS.open(data.brief); // an ask with an answer sent leaves at once; Answered today keeps it
  };
  // The team colour, from a fixed map by own-key lookup (the same five as app.js), never an API value in CSS.
  const TEAM = { chief: "var(--t-chief)", atlas: "var(--t-atlas)", forge: "var(--t-forge)", forecast: "var(--t-forecast)", orion: "var(--t-orion)" };
  const tone = (key) => (Object.hasOwn(TEAM, key) ? TEAM[key] : "var(--ink-3)");
  const fresh = (t) => t.new || [];
  const freshCount = () => (data.brief ? data.brief.teams.reduce((n, t) => n + fresh(t).length, 0) : 0);
  // What newly needs Dylan since the last look the greeting counts from (the same b.last_look): an ask written after
  // it. Never the count of asks waiting. ?clock=<ISO time> sets the mascot's clock.
  const newNeeds = () => {
    const look = Date.parse(data.brief.last_look), ids = [];
    if (isNaN(look)) return ids;
    for (const t of data.brief.teams) for (const k of t.asks || []) if (!k.answer && Date.parse(k.asked_at) > look) ids.push(`ask:${t.key}/${k.name}`);
    return ids;
  };
  const mascotState = (ids, w) => {
    const c = params.get("clock"), d = c ? new Date(c) : new Date(), h = +HOUR.format(isNaN(d) ? new Date() : d);
    return ids.length ? "needs" : !w && (h >= 23 || h < 6) ? "sleepy" : w ? "working" : h >= 6 && h < 12 ? "morning" : "rest";
  };
  const KIND = { working: "Working on", shipped: "Shipped", waiting: "Waiting on you" };

  // ---- Drawing: the whole page from the model, only when what it shows has changed ----

  const home = $("home");
  let drawn = "";
  let answeredOpen = false, answeredAll = false; // the Answered today list, open until the page reloads
  const pagesOpen = new Set(); // teams whose Pages list shows every page, until the page reloads
  let rising = null; // the team whose "<n> more" was just tapped: its new rows rise in once
  const expanded = new Set(); // teams whose card shows every new item, until the page reloads
  // A team's Pages: newest first, the first five, then "<n> more" in place. Each row opens the page in a new tab.
  function pagesList(t) {
    const box = el("div", "pages"), ul = el("ul"), all = pagesOpen.has(t.key);
    box.append(el("small", null, "Pages"), ul);
    for (const [n, p] of (all ? t.pages : t.pages.slice(0, 5)).entries()) {
      const li = el("li", rising === t.key && n >= 5 ? "pop" : null), a = el("a", null, p.title);
      a.href = `/a/${encodeURIComponent(t.key)}/${p.path.split("/").map(encodeURIComponent).join("/")}`;
      a.target = "_blank";
      a.rel = "noopener";
      li.append(a, el("span", "by", `${p.author || "author not named"} · ${tm(p.modified)} ET`));
      ul.append(li);
    }
    if (rising === t.key) rising = null;
    if (!all && t.pages.length > 5) {
      const more = el("button", "more", `${t.pages.length - 5} more`);
      more.type = "button";
      more.dataset.key = `morepages:${t.key}`;
      more.addEventListener("click", () => { pagesOpen.add(t.key); rising = t.key; drawn = ""; render(); });
      box.append(more);
    }
    return box;
  }
  function render() {
    const s = data.state, b = data.brief;
    const l = $("live");
    const off = live.state === false || live.brief === false;
    l.className = off ? "live off" : "live";
    l.replaceChildren(el("i"), off ? (live.good ? `Offline, last ${tm(live.good)}` : "Offline") : live.state && live.brief ? `Live, ${tm(live.good)}` : "Reading");
    if (!s || !b) return;
    // What the page shows, times included as they read: a redraw only when one of them changes keeps the mascots'
    // animations and the keyboard focus steady between polls.
    const N = needs(), W = working(), A = agents();
    const sig = JSON.stringify([b, A.map((a) => [a.name, a.state, pose(a), a.now, a.now && ago(a.now_at), a.pane, a.team]),
      b.teams.map((t) => (t.asks || []).map((k) => k.asked_at && dur(k.asked_at))), b.teams.map((t) => t.modified && tm(t.modified)), s.commit,
      data.review, data.usage, new Date().getHours()]);
    if (sig === drawn) return;
    drawn = sig;
    const focusKey = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;

    const hour = +HOUR.format(new Date());
    const hello = el("div", "hello");
    hello.append(mascotCv, el("h1", null, `Good ${hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening"}, Dylan`));
    const ids = newNeeds();
    const sub = el("p", "sub"), n = freshCount(), mark = el("button", null, "Mark all seen");
    mark.type = "button";
    mark.dataset.key = "mark";
    mark.addEventListener("click", () => seen(false).then(() => readBrief()).then(render));
    sub.append(`Since ${tm(b.last_look)}, `, el("b", null, `${plural(n, "thing", "things")} changed`), ". ",
      el("b", null, `${N.length} need you`), `, and ${plural(W.length, "agent is", "agents are")} working. `, mark);

    // Needs you: one section per team, headed by its dot, name and count; under it one row per thing that waits on
    // Dylan: one row per ask. A team with none has no section.
    const needList = el("div", "list");
    // At most three asks per team, then one "<n> more" row inside the section that opens in place (kept open across
    // polls); the greeting counts every row.
    const shownIn = {}, total = {}, count = {};
    for (const x of N) { count[x.team] = (count[x.team] || 0) + 1; total[x.team] = count[x.team]; }
    let shown = 0, head = Symbol("none"); // a sentinel: a row with no team still opens its own section
    const openMore = (x) => {
      expanded.add(`asks:${x.team}`);
      drawn = "";
      render();
      [...home.querySelectorAll("[data-key]")].find((y) => y.dataset.key === `need:ask:${x.name}`)?.focus({ preventScroll: true });
    };
    N.forEach((x) => {
      if (x.team !== head) {
        head = x.team;
        const h = el("div", "nh");
        h.style.setProperty("--tc", tone(x.team));
        h.append(el("i"), el("b", null, teamName(x.team)), el("span", null, plural(count[x.team], "ask", "asks")));
        needList.append(h);
      }
      shownIn[x.team] = (shownIn[x.team] || 0) + 1;
      if (shownIn[x.team] > 3 && !expanded.has(`asks:${x.team}`)) {
        if (shownIn[x.team] === 4) {
          const more = el("button", "more", `${total[x.team] - 3} more`);
          more.type = "button";
          more.dataset.key = `more-asks:${x.team}`;
          more.addEventListener("click", () => openMore(x));
          needList.append(more);
        }
        return;
      }
      const i = shown++;
      const r = el(x.href ? "a" : "div", "row need ask"), body = el("div");
      r.dataset.key = `need:ask:${x.name || x.team}`;
      const k = x.ask, q = el("div", "q");
      if (x.href) r.href = x.href;
      q.append(...(k.question ? line(k.question, true) : [el("span", "gap", `${k.name}: no ask block yet`)]));
      body.append(q);
      if (k.asked_at) body.append(el("div", "d", `Waiting ${dur(k.asked_at)}`));
      const go = MD.goesAhead(k.default);
      if (go && go.when) body.append(el("div", "d late", `Goes ahead ${go.when}`));
      r.append(el("span", "n", String(i + 1)), body, el("span", "chev"));
      needList.append(r);
    });
    if (!needList.children.length) needList.append(el("p", "quiet", "Nothing needs you right now."));
    // Answered today: what he sent since midnight ET and whether the lead has recorded it.
    const done = b.answered_today || [], notYet = done.filter((x) => x.state === "late").length;
    let doneBox = null;
    if (done.length) {
      doneBox = el("div", "answered");
      const tog = el("button", "answered-line"), t1 = `Answered today (${done.length}`;
      tog.type = "button";
      tog.dataset.key = "answered";
      tog.setAttribute("aria-expanded", String(answeredOpen));
      tog.append(t1, ...(notYet ? [", ", el("span", "late", `${notYet} not yet recorded`)] : []), ")");
      tog.addEventListener("click", () => { answeredOpen = !answeredOpen; drawn = ""; render(); [...home.querySelectorAll("[data-key]")].find((y) => y.dataset.key === "answered")?.focus({ preventScroll: true }); });
      doneBox.append(tog);
      if (answeredOpen) {
        const ul = el("ul", "answered-list");
        // Not yet recorded first, then the rest newest first (the server's order); five rows, then "<n> more".
        const rowsOf = [...done.filter((x) => x.state === "late"), ...done.filter((x) => x.state !== "late")];
        for (const x of answeredAll ? rowsOf : rowsOf.slice(0, 5)) {
          const li = el("li"), q = el(ASKS.href(x.team, x.name) ? "a" : "div", "q"), st = el("div", x.state === "late" ? "d late" : "d");
          const to = ASKS.href(x.team, x.name);
          if (to) q.href = to;
          q.append(...(x.question ? line(x.question, true) : [el("span", "gap", x.name)]));
          st.textContent = x.state === "recorded" ? `Recorded by ${x.recorded_by || x.lead || "the lead"} at ${tm(x.recorded_at)}`
            : x.state === "late" ? `Sent at ${tm(x.sent_at)}, not yet recorded` : `Sent at ${tm(x.sent_at)}`;
          li.append(q, st);
          ul.append(li);
        }
        doneBox.append(ul);
        if (!answeredAll && rowsOf.length > 5) {
          const more = el("button", "more", `${rowsOf.length - 5} more`);
          more.type = "button";
          more.dataset.key = "more-answered";
          more.addEventListener("click", () => { answeredAll = true; drawn = ""; render(); [...home.querySelectorAll("[data-key]")].find((y) => y.dataset.key === "more-answered")?.focus({ preventScroll: true }); });
          doneBox.append(more);
        }
      }
    }

    // At work right now: every agent herdr sees as working, in its own words for its latest step.
    const workList = el("div", "list");
    for (const a of W) {
      const r = el("button", "row work"), mid = el("div");
      r.type = "button";
      r.dataset.key = `work:${a.name}`;
      mid.style.minWidth = "0";
      mid.append(el("div", "nm", `${label(a)}, ${teamName(a.team)}`), el("div", "doing", a.now || "Working"));
      r.append(clawd(a), mid, el("span", "ago", a.now && a.now_at ? ago(a.now_at) : ""));
      r.addEventListener("click", () => openAgent(a.name));
      workList.append(r);
    }
    if (!W.length) workList.append(el("p", "quiet", "Nobody is working right now."));

    // Teams: one card per team, in the structure file's order.
    const cards = b.teams.map((t) => {
      const card = el("section", "team"), head = el("div", "t-head"), crew = A.filter((a) => a.team === t.key).sort(byRank);
      card.style.setProperty("--tc", `var(--t-${CSS.escape(t.key)}, var(--ink-3))`); // escaped: a key is never CSS
      head.append(el("span", "t-dot"), el("span", "t-name", t.name));
      // Needs you on the card is the same set as Needs you: an open ask, never the Waiting line's words.
      const open = (t.asks || []).some((k) => !k.answer);
      if (open) head.append(el("span", "t-flag", "Needs you"));
      head.append(el("span", "t-when", t.modified ? `Updated ${tm(t.modified)}` : ""));
      const now = el("p", t.working ? "t-now" : "t-now none");
      if (t.working) now.append(...line(t.working)); else now.textContent = "No status line yet";
      card.append(head, now);
      // Needs you shows this team's asks, not its Waiting line; the card keeps the line as written.
      if (open && isAsk(t.waiting)) {
        const w = el("p", "t-wait");
        w.append(el("span", "k", "Waiting on you: "), ...line(t.waiting));
        card.append(w);
      }
      const f = fresh(t);
      if (f.length) {
        const ul = el("ul", "new"), all = expanded.has(t.key);
        ul.tabIndex = -1;
        ul.dataset.key = `new:${t.key}`;
        for (const e of all ? f : f.slice(0, 3)) {
          const li = el("li"), txt = el("span");
          if (KIND[e.kind]) txt.append(el("span", "k", `${KIND[e.kind]}: `));
          // A null value: the team cleared that line from its STATUS at that time.
          txt.append(...(e.value == null ? [el("span", "k", "cleared from STATUS")] : line(e.value)));
          // A decision shows when it was written; any other change, when Atlas saw it.
          li.append(el("time", null, tm(e.kind === "decision" && e.written_at ? e.written_at : e.first_seen)), txt);
          ul.append(li);
        }
        card.append(ul);
        // More than three: the rest in place, in the API's order, on a tap; the list then takes the focus.
        if (!all && f.length > 3) {
          const more = el("button", "more", `${f.length - 3} more`);
          more.type = "button";
          more.dataset.key = `more:${t.key}`;
          more.addEventListener("click", () => {
            expanded.add(t.key);
            drawn = "";
            render();
            [...home.querySelectorAll("[data-key]")].find((x) => x.dataset.key === `new:${t.key}`)?.focus({ preventScroll: true });
          });
          card.append(more);
        }
      } else card.append(el("p", "quiet", "Nothing new since you looked."));
      // The team's changes since the last look, as one quiet link; none at 0.
      if (t.changes_since > 0) {
        const c = el("a", "since", `${plural(t.changes_since, "change", "changes")} since you last looked`);
        c.href = `/changes?team=${encodeURIComponent(t.key)}`;
        card.append(c);
      }
      if (t.pages.length) card.append(pagesList(t));
      if (crew.length) {
        const row = el("div", "crew"), c = (st) => crew.filter((a) => a.state === st).length;
        for (const a of crew) {
          const m = el("button", "mate");
          m.type = "button";
          m.dataset.key = `mate:${a.name}`;
          m.title = `${label(a)}, ${word(a)}`;
          m.setAttribute("aria-label", `${label(a)}, ${word(a)}`);
          m.append(clawd(a));
          m.addEventListener("click", () => openAgent(a.name));
          row.append(m);
        }
        const words = [...(c("working") ? [`${c("working")} working`] : []), `${crew.length - c("working") - c("blocked")} idle`];
        row.append(el("span", null, words.join(", ")));
        // A blocked agent is its parent's to clear: "<name>: Blocked for <parent>", in clay.
        for (const a of crew.filter((x) => x.state === "blocked")) row.append(el("span", "blk", `${label(a)}: ${word(a)}`));
        card.append(row);
      }
      if (t.shipped) {
        const url = links(t.shipped)[0], ship = el(url ? "a" : "div", t.picture ? "ship" : "ship noshot"), text = el("div");
        if (url) ship.href = url;
        text.append(el("small", null, "Last shipped"), ...line(t.shipped, true));
        ship.append(text);
        if (t.picture) {
          const img = el("img");
          img.alt = "";
          img.src = `/api/shipped-picture?team=${encodeURIComponent(t.key)}`;
          img.addEventListener("load", () => { if (img.naturalWidth / img.naturalHeight > 3) img.classList.add("strip"); });
          img.addEventListener("error", () => { img.remove(); ship.classList.add("noshot"); });
          ship.append(img);
        }
        card.append(ship);
      }
      return card;
    });

    const reviewLine = (k) => { const a = el("a", "reviewline", `${k} ${k === 1 ? "change waits" : "changes wait"} for your review`); a.href = "/review"; return a; };
    const usageLine = (t) => { const a = el("a", "usageline", t); a.href = "/usage"; return a; };
    const sec = (title, note) => { const d = el("div", "sec"); d.append(el("h2", null, title), el("span", null, note)); return d; };
    const foot = el("p", "foot", `Atlas at ${s.commit}, read ${tm(s.as_of)}`);
    if (teamsView) {
      const grid = el("div", "teamgrid");
      grid.append(...cards);
      home.replaceChildren(el("h1", "pt", "Teams"), el("p", "sub", plural(A.length, "agent", "agents")), grid, foot);
    } else {
      const left = el("div", "col"), right = el("div", "col");
      left.append(sec("Needs you", N.length ? "Tap one to answer" : ""), needList, ...(doneBox ? [doneBox] : []), ...(data.review > 0 ? [reviewLine(data.review)] : []));
      right.append(sec("At work right now", "In their own words"), workList);
      const cols = el("div", "cols");
      cols.append(left, right);
      home.replaceChildren(hello, sub, ...(data.usage ? [usageLine(data.usage)] : []), cols, foot);
    }
    if (!teamsView) mascot.update(mascotState(ids, W.length), ids); // after the canvas is in the page, so the z reads the page's ink
    if (focusKey) [...home.querySelectorAll("[data-key]")].find((x) => x.dataset.key === focusKey)?.focus({ preventScroll: true });
  }

  // ---- The sheet: an ask, or an agent's peek ----

  const scrim = $("scrim"), sheet = $("sheet");
  let peek = null, opener = null;
  function openSheet(label_, kids) {
    closeSheet(true);
    opener = document.activeElement;
    const x = el("button", "s-x");
    x.type = "button";
    x.setAttribute("aria-label", "Close");
    x.append(el("span", null, "Close"));
    x.addEventListener("click", () => closeSheet());
    sheet.setAttribute("aria-label", label_);
    sheet.replaceChildren(x, ...kids);
    sheet.scrollTop = 0;
    scrim.hidden = false;
    document.body.classList.add("is-sheet");
    home.inert = true;
    sheet.focus({ preventScroll: true }); // the sheet, not Close, so a tap shows no ring; Tab reaches Close
  }
  function closeSheet(quiet) {
    if (peek) { clearTimeout(peek.timer); peek = null; }
    if (scrim.hidden) return;
    scrim.hidden = true;
    sheet.replaceChildren();
    document.body.classList.remove("is-sheet");
    home.inert = false;
    if (!quiet && opener && opener.isConnected) opener.focus({ preventScroll: true });
  }
  scrim.addEventListener("click", (e) => { if (e.target === scrim) closeSheet(); });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && !scrim.hidden) closeSheet(); });

  // Whether this viewer is Dylan on one of his devices (type.js), for the ask sheet's last line.
  let typing = false;
  if (typeof SEND !== "undefined") SEND.typist().then((yes) => { typing = yes; });

  // An agent's peek: its state and time, its now line, its live screen re-read every 2 s, and its own tab.
  function openAgent(name) {
    const a = agents().find((y) => y.name === name);
    if (!a) return;
    const kick = el("p", "s-kicker", teamName(a.team)), title = el("h2", "s-title s-agent"), st = el("p", "s-state");
    kick.style.setProperty("--tc", `var(--t-${CSS.escape(a.team || "")}, var(--ink-3))`);
    title.append(clawd(a), el("span", null, label(a)));
    const nowBox = el("div", "s-block"), screen = el("div", "s-screen"), pre = el("pre");
    screen.setAttribute("aria-label", `${label(a)}'s screen`);
    screen.tabIndex = 0;
    pre.append(el("span", "note", "Reading its screen…"));
    screen.append(pre);
    const acts = el("div", "s-actions"), open = el("a", "s-btn primary", "Open live terminal");
    open.href = `/agent/${encodeURIComponent(a.name)}`;
    acts.append(open);
    openSheet(`${label(a)}`, [kick, title, st, nowBox, screen, acts]);
    peek = { name, st, nowBox, title, screen, pre, timer: null, text: null };
    paintPeek();
    readPeek();
  }
  function paintPeek() {
    if (!peek) return;
    const a = agents().find((y) => y.name === peek.name);
    if (!a) { peek.st.replaceChildren(el("b", null, "Not live")); return; }
    const t = since(a);
    peek.st.replaceChildren(el("b", a.state, word(a)), t ? `, ${t}` : "");
    peek.title.firstChild.replaceWith(clawd(a));
    const words = el("p");
    words.append(a.now || (a.runtime === "codex" ? "Working" : "No step read yet"), " ", el("span", "s-ago", a.now && a.now_at ? ago(a.now_at) : ""));
    peek.nowBox.replaceChildren(el("div", "s-label", "Now, in its own words"), words);
  }
  async function readPeek() {
    const p = peek;
    if (!p) return;
    const a = agents().find((y) => y.name === p.name);
    try {
      if (!a || !a.pane) throw new Error("not live");
      const j = await get(`/api/screens?pane=${encodeURIComponent(a.pane)}&format=ansi`).catch((e) => { throw e.status === 404 ? new Error("not live") : e; });
      if (p !== peek || j.text == null || j.text === p.text) return;
      // Following the bottom unless the reader scrolled up.
      const atEnd = p.screen.scrollHeight - p.screen.scrollTop - p.screen.clientHeight < 8;
      p.text = j.text;
      const rows = parseScreen(j.text);
      while (rows.length && !rows[rows.length - 1].plain.trim()) rows.pop();
      // One row per screen line; a line of box-drawing rules (term.js's isRule, as /agent/<name> marks it) stays one
      // rule across the peek, cut at its edge, instead of wrapping into a stack.
      p.pre.replaceChildren(...rows.map((row) => {
        const ln = el("div", isRule(row.plain) ? "ln is-rule" : "ln");
        ln.append(...row.runs.map((run) => styled(run.text, run.style)));
        return ln;
      }));
      if (atEnd || !p.shown) p.screen.scrollTop = p.screen.scrollHeight;
      p.shown = true;
    } catch (e) {
      if (p === peek && p.text == null) p.pre.replaceChildren(el("span", "note", e.message === "not live" ? "Not live: no screen to read." : "Its screen could not be read just now."));
    } finally {
      if (p === peek) {
        p.timer = document.hidden ? null : setTimeout(readPeek, PEEK_MS);
        p.paused = document.hidden;
      }
    }
  }

  // ---- Start: both reads every 5 s; a link with ?peek=<agent> or ?ask=<n> opens that sheet ----

  // Each poll runs start to start while the page is shown; a hidden page reads nothing, and showing it reads at once.
  const polls = [{ ms: STATE_MS, read: readState }, { ms: BRIEF_MS, read: readBrief }, { ms: REVIEW_MS, read: readReview }, { ms: USAGE_MS, read: readUsage }];
  function run(p) {
    clearTimeout(p.timer);
    p.timer = null;
    if (p.running) return;
    p.running = true;
    const t0 = Date.now();
    p.read().finally(() => {
      p.running = false;
      render();
      paintPeek();
      if (!document.hidden) p.timer = setTimeout(() => run(p), Math.max(0, p.ms - (Date.now() - t0)));
    });
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) for (const p of polls) { clearTimeout(p.timer); p.timer = null; }
    else {
      for (const p of polls) run(p);
      if (peek && peek.paused) { peek.paused = false; readPeek(); }
    }
  });
  let opened = false;
  readReview().then(render);
  readUsage().then(render);
  Promise.all([readState(), readBrief()]).then(() => {
    render();
    if (!opened && data.state && data.brief) {
      opened = true;
      if (params.get("peek")) openAgent(params.get("peek"));
      else if (params.get("ask")) { const x = needs()[+params.get("ask") - 1]; if (x && x.href) location.href = x.href; }
    }
    for (const p of polls) if (!document.hidden && !p.timer && !p.running) p.timer = setTimeout(() => run(p), p.ms);
  });
  render();
}
