// Atlas: the review queue (/review) and a change's code (/pr/<repo>/<n>), after spec 2026-10-31-code-review and the
// prototype mocks/review-pr41.html. The diff is git's, read by /api/pr-diff from this box's copies; nothing here
// fetches or calls GitHub. Every API value reaches the page as text or nodes (md.js), never innerHTML, and syntax
// colour comes from a fixed set of class names, never from the text.
"use strict";

{
  const { el, inline, block } = MD;
  const root = document.getElementById("root");
  const [, kind, repoName, ...rest] = location.pathname.split("/").map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  const q = (o) => new URLSearchParams(o).toString();
  const dots = (...parts) => parts.some((p) => p === "." || p === "..");
  const path = (...parts) => (dots(...parts) ? null : `/${parts.map(encodeURIComponent).join("/")}`);
  const bare = (t) => String(t || "").replace(/^(report|ask)\s*:\s*/i, "");
  // A change's heading: its report's title, else the subject of the branch's first commit after the base; when the title
  // is only a worker's or branch's name ("Report: grata-build"), which does not say what changed, that subject too. The
  // worker's name goes in the small line above.
  const heading = (x) => { const t = bare(x.title), s = x.first || x.subject; return /\s/.test(t) || !s ? t || s || x.branch : s; };
  const worker = (x) => { const m = /^From `([^`]+)`/.exec((x.story && x.story.author) || ""); return m ? m[1] : x.author; };

  const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "2-digit", day: "2-digit", hour: "numeric", minute: "2-digit" });
  const day = (iso) => {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "";
    const p = Object.fromEntries(ET.formatToParts(d).map((x) => [x.type, x.value]));
    return `${p.weekday} ${p.month}-${p.day}, ${p.hour}:${p.minute} ${p.dayPeriod} ET`;
  };
  async function get(url) {
    const res = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(15000) });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }
  let names = {};
  const teamName = (key) => (Object.hasOwn(names, key) ? names[key] : key);
  const readNames = () => get("/api/state").then((s) => { names = (s && s.teams) || {}; }, () => {});
  const teamLabel = (x) => (x.teams ? x.teams.map(teamName).join(", ") : teamName(x.team));
  // One dot per declaring team, each in its own tint, for a repo two teams share; a single-team node keeps its one tinted dot.
  const teamDots = (node, x) => { if (x.teams) for (const t of x.teams) node.append(tint(el("i"), t)); else node.append(el("i")); return node; };
  const tint = (node, team) => { node.style.setProperty("--tc", `var(--t-${CSS.escape(String(team))}, var(--ink-3))`); return node; };
  const say = (msg, sub) => { const d = el("div", "q"); d.append(el("h1", null, msg)); if (sub) d.append(el("p", "sub", sub)); root.replaceChildren(d); };

  // ---- A small highlighter: comments, strings, numbers, keywords and a call's name, by file type ----
  const EXT = { js: "js", mjs: "js", cjs: "js", ts: "js", tsx: "js", jsx: "js", java: "js", go: "js", rs: "js", c: "js", h: "js", cs: "js",
    css: "css", scss: "css", py: "py", sh: "sh", bash: "sh", toml: "sh", yaml: "sh", yml: "sh", sql: "sql", html: "html", svg: "html", xml: "html" };
  const COM = { js: String.raw`\/\/.*$|\/\*.*?\*\/`, css: String.raw`\/\*.*?\*\/`, py: "#.*$", sh: "#.*$", sql: String.raw`--.*$|\/\*.*?\*\/`, html: "<!--.*?-->" };
  const KW = { js: "const|let|var|function|return|if|else|for|while|of|in|new|class|import|from|export|async|await|try|catch|finally|throw|null|true|false|this|typeof|break|continue|switch|case|default|interface|type|extends|static",
    py: "def|class|return|if|elif|else|for|while|in|not|and|or|import|from|as|with|try|except|finally|raise|None|True|False|lambda|yield|pass|break|continue|is",
    sql: "select|from|where|and|or|not|null|create|table|insert|into|values|update|set|delete|alter|add|drop|index|primary|key|references|default|join|on|order|by|group|limit|text|integer" };
  const RULES = {};
  const rules = (lang) => RULES[lang] ||= {
    tok: new RegExp(`(${COM[lang]})|("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'${lang === "js" ? "|`(?:[^`\\\\]|\\\\.)*`" : ""})|(\\b\\d+(?:\\.\\d+)?\\b${lang === "css" ? "|#[0-9a-fA-F]{3,8}\\b" : ""})`, "gm"),
    kw: KW[lang] ? new RegExp(`\\b(${KW[lang]})\\b|\\b([A-Za-z_]\\w*)(?=\\()`, lang === "sql" ? "gi" : "g") : null };
  const span = (cls, text) => el("span", cls, text);
  function words(text, r) {
    if (!r.kw || !text) return text ? [text] : [];
    const out = []; let at = 0, m; const re = new RegExp(r.kw);
    while ((m = re.exec(text))) {
      if (m.index > at) out.push(text.slice(at, m.index));
      out.push(span(m[1] ? "k" : "f", m[0])); at = re.lastIndex;
    }
    if (at < text.length) out.push(text.slice(at));
    return out;
  }
  function hl(src, file) {
    const lang = EXT[(file.match(/\.([A-Za-z0-9]+)$/) || [])[1]?.toLowerCase()];
    if (!lang || src.length > 1000) return [src];
    const r = rules(lang), out = []; let at = 0, m; const re = new RegExp(r.tok);
    while ((m = re.exec(src))) {
      if (m[0] === "") { re.lastIndex++; continue; }
      out.push(...words(src.slice(at, m.index), r), span(m[1] ? "c" : m[2] ? "s" : "n", m[0]));
      at = m.index + m[0].length;
    }
    out.push(...words(src.slice(at), r));
    return out;
  }

  // ---- The code page ----
  async function code() {
    const n = rest.join("/");
    const [pr, queue] = await Promise.all([get(`/api/pr?${q({ repo: repoName, n })}`), get("/api/review").catch(() => null)]);
    if (!pr) return say("No such change", `Atlas has no open change ${n} in ${repoName}.`);
    if (pr.not_on_box || pr.landed) return say(pr.landed ? "Already in the base" : "Not on this box yet", pr.message);
    const sp = pr.story && pr.story.name ? { team: pr.story.team, name: pr.story.name } : null;
    const story = sp ? await get(`/api/change?${q(sp)}`).catch(() => null) : null;
    const storyHref = sp && path("change", sp.team, ...sp.name.split("/"));
    MD.tabs("Code", storyHref, path("pr", pr.repo, String(pr.n)));
    const typist = typeof SEND !== "undefined" && (await SEND.typist());
    const list = await get(`/api/pr-diff?${q({ repo: pr.repo, n: pr.n })}`);
    if (!list || list.error || !list.files) return say(pr.subject || "Change", (list && list.error) || pr.error || "Atlas could not read this change's diff.");
    const files = list.files;
    const tot = files.reduce((a, f) => [a[0] + (f.add || 0), a[1] + (f.del || 0)], [0, 0]);
    const cx = pr.codex, finds = (cx && cx.findings) || [];
    const at = new Map(finds.filter((f) => !f.unchanged_line).map((f) => [`new:${f.line}:${f.path}`, f]));
    const mine = [...(queue ? [...queue.waits, ...queue.new, ...queue.rest] : [])].find((x) => x.repo === pr.repo && String(x.n) === String(pr.n));
    let reviewed = Boolean(mine && mine.reviewed);
    const title = bare(pr.subject || (pr.story && pr.story.title) || pr.branch);
    document.title = `${title} - Atlas`;

    // Drafts: kept in this browser only, by change and head; every read and write is guarded.
    const KEY = `atlas.review.${pr.repo}.${pr.n}.${String(pr.head).slice(0, 12)}`;
    const drafts = new Map();
    const KEYRE = /^(old|new):(\d+):([^]+)$/;
    try { for (const [k, v] of Object.entries(JSON.parse(localStorage.getItem(KEY) || "{}"))) if (typeof v === "string" && KEYRE.test(k)) drafts.set(k, v); } catch { /* none kept */ }
    const keep = () => { try { if (drafts.size) localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(drafts))); else localStorage.removeItem(KEY); } catch { /* not kept */ } };
    const items = () => [...drafts].map(([k, v]) => [k, v.replace(/\s*\n\s*/g, " ").trim()]).filter(([, v]) => v);
    // A line's key is "new:<line>:<path>" for a line of the new file and "old:<line>:<path>" for a deleted one: the side and
    // the line come first and only they are parsed, so a path may hold any character, a colon included.
    const split = (k) => { const m = KEYRE.exec(k); return { path: m[3], line: Number(m[2]), old: m[1] === "old" }; };
    const OLD = "old side, a deleted line: ";
    const said = ([k, v]) => { const x = split(k); return `${x.path}:${x.line}: ${x.old ? OLD : ""}${v}`; };
    const placeOf = (k) => { const x = split(k); return x.old ? `${x.path}, old line ${x.line}` : `${x.path}:${x.line}`; };
    const keyOf = (side, line, file) => `${side}:${line}:${file}`;
    const draftLine = () => items().length ? `Dylan, review of ${pr.repo} PR ${pr.n}: ${items().map(said).join("; ")}` : "";

    // Left: the author's "The code" list groups the files; the rest go under "Other files".
    const groups = [];
    for (const m of String((story && story.labels && story.labels["The code"]) || "").matchAll(/^[ \t]*[-*] (.+?): ((?:`[^`]+`(?:, )?)+)$/gm)) groups.push([m[1].trim(), [...m[2].matchAll(/`([^`]+)`/g)].map((x) => x[1])]);
    const where = new Map();
    for (const [g, fs] of groups) for (const p of fs) if (!where.has(p)) where.set(p, g);
    const order = [];
    for (const f of files) { const g = where.get(f.path) || (groups.length ? "Other files" : "Files"); if (!order.includes(g)) order.push(g); }
    order.sort((a, b) => (a === "Other files") - (b === "Other files"));

    // ---- The head ----
    const records = (pr.records_all || [pr.records]).map((x) => `${x}'s`).join(" or ");
    const head = el("div", "head"), kick = el("div", "kick"), tm = tint(el("span", "tm"), pr.team);
    teamDots(tm, pr).append(teamLabel(pr));
    const ids = el("span", null, pr.number ? `${pr.repo} PR ${pr.number}` : `${pr.repo}, no PR number known`);
    const br = el("span"); br.append(el("code", null, pr.branch));
    const bd = el("span", null, `by ${pr.author || "unknown"}, owned by ${pr.owners ? pr.owners.map((x) => x.name).join(" and ") : pr.owner.name || "no one"}`);
    const when = el("span"); when.append(el("code", null, `${pr.base_ref} ${String(pr.base || "").slice(0, 7)}`), ` (${day(pr.base_time)}) to `, el("code", null, String(pr.head).slice(0, 7)), `, read from this box's clone, last fetched ${day(pr.as_of_fetch) || "never"}`);
    kick.append(tm, ids, br, bd, when);
    const ghl = MD.gh(queue && queue.repos, pr.repo, pr.number, pr.branch);
    if (ghl) kick.append(ghl);
    const pills = el("div", "pills");
    const pAdd = el("span", "pill add", `+${tot[0]}`), pDel = el("span", "pill del", `−${tot[1]}`), pFiles = el("span", "pill", `${files.length} ${files.length === 1 ? "file" : "files"}`);
    pills.append(pAdd, pDel, pFiles);
    pills.append(el("span", cx && cx.read ? "pill c" : "pill", cx ? `${cx.verdict ? `Review: no blocker, ${cx.verdict.should_fix} should-fix` : !cx.read ? "Review found" : `Review found: ${finds.length ? `${finds.length} ${finds.length === 1 ? "finding" : "findings"}` : "no findings listed"}`}${cx.current || !cx.read ? "" : ", of an earlier head"}` : `No review found in ${records} records`));
    const mark = el(typist ? "button" : "span", "pill");
    const paintMark = () => { mark.textContent = typist ? (reviewed ? "Reviewed by you" : "Mark reviewed") : (reviewed ? "Reviewed by you" : "Not reviewed by you"); mark.classList.toggle("on", typist && reviewed); };
    paintMark();
    const markMsg = el("span", "mark-msg");
    markMsg.setAttribute("role", "status");
    if (typist) {
      mark.type = "button";
      mark.addEventListener("click", async () => {
        const want = !reviewed;
        const why = await SEND.post("/api/review-mark", { repo: pr.repo, n: pr.n, head: pr.head, reviewed: want });
        markMsg.textContent = why || "";
        if (why === null) { reviewed = want; paintMark(); }
      });
    }
    pills.append(mark, markMsg);
    head.append(kick, el("h1", null, title), pills);

    // ---- The diff, file by file, each read when it comes into view ----
    const mode = { v: "unified" }, st = new Map(), full = new Map(), open = new Set();
    const PHONE = matchMedia("(max-width: 700px)");
    const main = el("main", "main"), tools = el("div", "tools");
    const bU = el("button", "btn on", "Unified"), bS = el("button", "btn split-btn", "Split");
    bU.type = bS.type = "button";
    tools.append(bU, bS, el("span", "pill", "Tap a line number to comment"));
    main.append(tools);
    const sections = files.map((f, i) => {
      const s = el("section", "file"), h = el("div", "fh"), body = el("div", "filebody");
      s.id = `f${i}`;
      const p = el("span", "p", f.path);
      h.append(p);
      if (f.old) { const o = el("span", "p"); o.append("from ", el("code", null, f.old)); h.append(o); }
      h.append(el("span", `st ${f.status}`, f.status));
      if (f.sensitive) h.append(el("span", "st warn", "Sensitive file: shown only as git has it, check it holds no secret"));
      const ct = el("span", "ct"); ct.append(el("span", "a", `+${f.add}`), " ", el("span", "d", `−${f.del}`));
      h.append(ct);
      s.append(h, body);
      main.append(s);
      return { f, i, s, body };
    });
    const cell = (cls, ...kids) => { const t = el("td", cls); t.append(...kids); return t; };
    const rowEl = (cls, ...tds) => { const r = el("tr", cls); r.append(...tds); return r; };
    function notes(sec, key, span) {
      const out = [], f = at.get(key);
      if (f) { const d = el("div", "finding"); d.append(el("b", null, `Codex ${f.id}. `), `${f.title}.`); out.push(rowEl("note", Object.assign(cell(null, d), { colSpan: span }))); }
      if (drafts.has(key)) {
        const box = el("div", "cbox"), ta = el("textarea"), rm = el("button", "btn", "Remove"), row = el("div", "row");
        ta.value = drafts.get(key);
        ta.dataset.k = key;
        ta.setAttribute("aria-label", `Your comment on ${placeOf(key)}`);
        rm.type = "button"; rm.dataset.del = key;
        row.append(rm);
        box.append(el("div", "who", `Your comment on ${placeOf(key)}, not sent yet`), ta, row);
        out.push(rowEl("note", Object.assign(cell(null, box), { colSpan: span })));
      }
      return out;
    }
    function lineRows(sec, t, o, n, text) {
      const cls = t === "+" ? "add" : t === "-" ? "del" : "", key = n ? keyOf("new", n, sec.f.path) : o ? keyOf("old", o, sec.f.path) : null, code = hl(text, sec.f.path);
      const ln = cell("ln", String(n ?? o ?? ""));
      if (key) ln.dataset.c = key;
      return [rowEl(cls, ln, cell("sgn", t === " " ? "" : t), cell("code", ...code)), ...(key ? notes(sec, key, 3) : [])];
    }
    function splitRows(sec, lines) {
      const out = [];
      const one = (L, R) => {
        const mk = (x, right) => x ? [cell("ln", String((right ? x[2] : x[1]) ?? "")), cell("code", ...hl(x[3], sec.f.path))] : [cell("ln e"), cell("code e")];
        const l = mk(L, false), r = mk(R, true);
        // Only a deleted line's old number is a target on the left; an unchanged line is commented on its right.
        if (L && L[0] === "-" && L[1]) l[0].dataset.c = keyOf("old", L[1], sec.f.path);
        if (R && R[2]) r[0].dataset.c = keyOf("new", R[2], sec.f.path);
        r[1].classList.add("l");
        out.push(rowEl(R && R[0] === "+" ? "add" : L && L[0] === "-" ? "del" : "", ...l, ...r));
        if (L && L[0] === "-" && L[1]) out.push(...notes(sec, keyOf("old", L[1], sec.f.path), 4));
        if (R && R[2]) out.push(...notes(sec, keyOf("new", R[2], sec.f.path), 4));
      };
      for (let k = 0; k < lines.length;) {
        const l = lines[k];
        if (l[0] === " ") { one(l, l); k++; continue; }
        const dels = [], adds = [];
        while (k < lines.length && lines[k][0] === "-") dels.push(lines[k++]);
        while (k < lines.length && lines[k][0] === "+") adds.push(lines[k++]);
        for (let j = 0; j < Math.max(dels.length, adds.length); j++) one(dels[j], adds[j]);
      }
      return out;
    }
    const gapRow = (span, label, key, whole) => {
      const b = el("button", "gapbtn", label);
      b.type = "button"; b.dataset.gap = key;
      return rowEl("gap", Object.assign(cell(null, b), { colSpan: span }));
    };
    // Unchanged lines git shows after the last change: fewer than three means the file ends there.
    const trailing = (d) => { const ls = (d.hunks.at(-1) || { lines: [] }).lines; let k = 0; while (k < ls.length && ls[ls.length - 1 - k][0] === " ") k++; return k; };
    function paint(sec) {
      const { f, body } = sec, d = st.get(f.path), span = mode.v === "split" ? 4 : 3, spl = mode.v === "split";
      const plain = (t) => body.replaceChildren(el("p", "plain", t));
      if (f.binary) return plain("A binary file: its size changed, there are no lines to show.");
      const collapsed = f.status === "deleted" && f.del > 60 && !open.has(`${sec.i}:all`);
      if (collapsed) {
        const b = el("button", "gapbtn", `Show ${f.del} deleted lines`);
        b.type = "button"; b.dataset.gap = `${sec.i}:all`;
        return body.replaceChildren(b);
      }
      if (!d) return plain("Reading this file's lines…");
      if (d.error) return plain(d.error);
      const table = el("table", `diff ${mode.v}`), cg = el("colgroup"), num = PHONE.matches ? 34 : 46, w = (px) => { const c = el("col"); if (px) c.style.width = `${px}px`; return c; };
      cg.append(...(spl ? [w(num), w(), w(num), w()] : [w(num), w(16), w()]));
      table.append(cg);
      const tb = el("tbody"); table.append(tb);
      const lines = (...ls) => (spl ? splitRows(sec, ls.flat()) : ls.flat().flatMap(([t, o, n, x]) => lineRows(sec, t, o, n, x)));
      // The unchanged lines a..b, each with both its numbers, from the ranges the API returned; a run it did not return is
      // said so in a row, never shown as blank lines.
      const gapLines = (a, b) => {
        const rd = full.get(f.path), out = []; let miss = 0;
        const flush = () => { if (miss) { out.push(`${miss} unchanged ${miss === 1 ? "line is" : "lines are"} ${rd.capped ? "past where Atlas's read of this file was cut, so they cannot be read here" : "too far from any change for Atlas to read here"}`); miss = 0; } };
        for (let n = a; n <= b; n++) { const x = rd.lines.get(n); if (x) { flush(); out.push([" ", x[0], n, x[1]]); } else miss++; }
        flush();
        return out;
      };
      const shown = (ls) => ls.flatMap((x) => (typeof x === "string" ? [rowEl("gap", Object.assign(cell(null, el("p", "plain", x)), { colSpan: span }))] : lines(x)));
      const run = (ls) => { const out = []; let cur = []; for (const x of ls) { if (typeof x === "string") { if (cur.length) out.push(cur); cur = []; out.push(x); } else cur.push(x); } if (cur.length) out.push(cur); return out; };
      let prev = 1;
      d.hunks.forEach((h, k) => {
        if (f.status === "modified") {
          const key = `${sec.i}:${k}`, to = h.n - 1;
          if (to >= prev) {
            if (open.has(key) && full.has(f.path)) tb.append(...shown(run(gapLines(prev, to))));
            else tb.append(gapRow(span, `Show ${to - prev + 1} unchanged ${to - prev + 1 === 1 ? "line" : "lines"}`, key));
          }
        }
        tb.append(rowEl("hunk", Object.assign(cell(null, h.ctx || `line ${h.n}`), { colSpan: span })));
        tb.append(...lines(h.lines));
        const last = h.lines.filter((l) => l[2]).at(-1);
        prev = last ? last[2] + 1 : h.n;
      });
      if (d.capped) tb.append(...shown(["The rest of this file's diff cannot be read here: Atlas's read of it was cut at its size limit"]));
      if (f.status === "modified" && !d.next && !d.capped) {
        const key = `${sec.i}:end`;
        const rd = full.get(f.path), more = rd && (rd.max >= prev || !rd.eof);
        if (open.has(key) && rd) {
          if (rd.max >= prev) tb.append(...shown(run(gapLines(prev, rd.max))));
          if (!rd.eof) tb.append(...shown([rd.capped ? "The rest of this file cannot be read here: Atlas's read of it was cut at its size limit" : "More unchanged lines follow, too far from any change for Atlas to read here"]));
        } else if (rd ? more : trailing(d) >= 3) tb.append(gapRow(span, "Show the unchanged lines below", key));
      }
      body.replaceChildren(table);
      if (d.next) {
        const b = el("button", "gapbtn", `Show the next lines (${d.shown} of ${d.total})`);
        b.type = "button"; b.dataset.more = f.path;
        body.append(b);
      }
    }
    // One page of a file's hunks, joined to the ones before it: a hunk the page cut carries on in the next, with the
    // same start.
    // A read cut at the reader's size limit (output_capped) ends in a row that may be partial and says nothing of what
    // follows: its last row is dropped, and the rest is said to be unreadable, never taken for the end of the file.
    const uncut = (hunks) => {
      const out = hunks.slice(), l = out.at(-1);
      if (l) { out[out.length - 1] = { ...l, lines: l.lines.slice(0, -1) }; if (!out.at(-1).lines.length) out.pop(); }
      return out;
    };
    const joined = (prior, got, offset) => {
      const hunks = prior.hunks.slice();
      for (const h of got.hunks || []) {
        const l = hunks.at(-1);
        if (offset && l && l.o === h.o && l.n === h.n && h === got.hunks[0]) hunks[hunks.length - 1] = { ...l, lines: l.lines.concat(h.lines) }; else hunks.push(h);
      }
      return hunks;
    };
    const readPage = async (f, params, offset) => {
      const r = await get(`/api/pr-diff?${q({ repo: pr.repo, n: pr.n, path: f.path, lines: 5000, ...params, ...(offset ? { offset } : {}) })}`);
      const got = r && r.files && r.files.find((x) => x.path === f.path);
      if (!got) throw new Error("none");
      return { got, next: r.next || null };
    };
    async function load(sec, offset) {
      const f = sec.f;
      if (f.binary || (!offset && st.has(f.path)) || (f.status === "deleted" && f.del > 60 && !open.has(`${sec.i}:all`))) return;
      if (!offset) st.set(f.path, null);
      try {
        const { got, next } = await readPage(f, {}, offset);
        const prior = st.get(f.path) || { hunks: [], shown: 0 };
        const shown = prior.shown + (got.hunks || []).reduce((a, h) => a + h.lines.length, 0);
        const cut = Boolean(got.output_capped) && !next, hunks = joined(prior, got, offset);
        st.set(f.path, { hunks: cut ? uncut(hunks) : hunks, shown, total: got.total_lines, next, capped: cut });
      } catch { st.set(f.path, { error: "Atlas could not read this file's lines. Reload to try again.", hunks: [] }); }
      paint(sec);
      // Three lines after the last change may be the end of the file or not: read the file once to know.
      const d = st.get(f.path);
      if (f.status === "modified" && d && !d.error && !d.next && !d.capped && trailing(d) === 3 && !full.has(f.path)) expand(sec);
    }
    // The whole file around its changes, as far as the API reads it: every page of the diff with 5000 lines of context,
    // kept as the lines it returned, each with both numbers. eof is true only when git shows the file's end.
    async function expand(sec) {
      const f = sec.f;
      if (!full.has(f.path)) {
        try {
          let acc = { hunks: [] }, offset = 0, capped = false;
          for (let k = 0; k < 200; k++) {
            const { got, next } = await readPage(f, { context: 5000 }, offset);
            acc = { hunks: joined(acc, got, offset) };
            if (got.output_capped) capped = true;
            if (!next || next.path !== f.path) break;
            offset = next.offset;
          }
          if (capped) acc = { hunks: uncut(acc.hunks) };
          const lines = new Map(); let max = 0;
          for (const h of acc.hunks) for (const l of h.lines) if (l[2]) { lines.set(l[2], [l[1], l[3]]); max = Math.max(max, l[2]); }
          full.set(f.path, { lines, max, capped, eof: !capped && trailing(acc) < 5000 });
        } catch { return; }
      }
      paint(sec);
    }
    const seen = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { seen.unobserve(e.target); load(sections[+e.target.id.slice(1)]); } }), { rootMargin: "800px 0px" });
    sections.forEach((s) => { paint(s); seen.observe(s.s); });
    const repaintAll = () => sections.forEach((s) => paint(s));

    // ---- Left: the files by what they do, and the queue ----
    const tree = el("nav", "tree");
    tree.setAttribute("aria-label", "Files");
    tree.append(el("h3", null, "Files, by what they do"));
    for (const g of order) {
      const box = el("div", "grp"), gt = el("div", "gt");
      gt.append(...(g === "Other files" || g === "Files" ? [g] : inline(g, true)));
      box.append(gt);
      for (const s of sections) {
        if ((where.get(s.f.path) || (groups.length ? "Other files" : "Files")) !== g) continue;
        const a = el("a", "fi"), nm = el("span", "nm"), nmb = el("bdi", null, s.f.path), c = finds.filter((x) => x.path === s.f.path).length;
        a.href = `#f${s.i}`;
        nm.append(nmb);
        a.append(nm, el("span", "a", `+${s.f.add}`), el("span", "d", `−${s.f.del}`));
        if (c) a.append(el("span", "x", String(c)));
        box.append(a);
      }
      tree.append(box);
    }
    if (queue) {
      const shown = [...queue.waits, ...queue.new];
      if (shown.length) {
        tree.append(el("h3", null, "Review queue"));
        const ul = el("ul", "queue");
        for (const x of shown.slice(0, 12)) {
          const li = el("li", String(x.n) === String(pr.n) && x.repo === pr.repo ? "here" : null), a = el("a"), m = el("span"), t = el("span", "t", `${x.repo} ${x.number ? `#${x.number}` : x.branch}`);
          const href = path("pr", x.repo, String(x.n));
          if (href) a.href = href;
          tint(a, x.team);
          m.append(t, " ", el("span", "m", `${heading(x).slice(0, 60)} +${x.totals.add} −${x.totals.del}`));
          a.append(el("i"), m, el("span", x.reviewed ? "r" : "u", x.reviewed ? "Reviewed" : "Not yet"));
          li.append(a);
          ul.append(li);
        }
        tree.append(ul);
      }
    }

    // ---- Right: drafts and Send, one line of story, Codex ----
    const side = el("aside", "side"), send = el("div", "send"), pre = el("pre"), why = el("p", "why");
    // A change in a repo two teams declare has one owner and one button per declaring team; the server picks the agent from `to`.
    const owners = pr.owners || [pr.owner || {}], shared = Boolean(pr.owners);
    // A reason both buttons share (not the typist) shows once, under both; a reason for one owner shows under its own button.
    const offAll = !typist ? "Only you can send, from your own signed-in browser." : "";
    const offOwn = (o) => !o.name ? "No one owns this change." : !o.live ? `${o.name} is not running, so there is nobody to send to.`
      : o.state === "blocked" ? `${o.name} is waiting on a question in its terminal. Answer it there first.` : "";
    const offOf = (o) => offAll || offOwn(o);
    const gos = owners.map(() => { const b = el("button", "btn go"); b.type = "button"; return b; });
    const whys = owners.map(() => el("p", "why"));
    let sending = false;
    const paintSend = () => {
      const k = items().length;
      pre.textContent = draftLine() || "No comments yet.";
      owners.forEach((o, i) => {
        gos[i].textContent = `Send ${k} ${k === 1 ? "comment" : "comments"} to ${o.name || "its owner"}`;
        gos[i].disabled = !k || Boolean(offOf(o)) || sending;
        if (shared) { whys[i].textContent = offOwn(o); whys[i].hidden = !whys[i].textContent; }
      });
      if (offAll || (!shared && offOf(owners[0]))) why.textContent = shared ? offAll : offOf(owners[0]);
      why.hidden = !why.textContent;
    };
    send.append(el("b", null, shared ? "Your comments go to the agent you send to" : `Your comments go to ${owners[0].name || "its owner"}'s terminal as one line`), pre, ...(shared ? gos.flatMap((g, i) => [g, whys[i]]) : gos), why);
    owners.forEach((o, i) => gos[i].addEventListener("click", async () => {
      const sent = items();
      if (!sent.length || offOf(o) || sending) return;
      sending = true; paintSend();
      let reason;
      try {
        reason = await SEND.send({ kind: "review", repo: pr.repo, n: pr.n, ...(shared ? { to: o.team } : {}), comments: sent.map(([k, v]) => { const x = split(k); return { path: x.path, line: x.line, comment: (x.old ? OLD : "") + v }; }) });
      } finally { sending = false; }
      if (reason) { why.textContent = reason; why.hidden = false; paintSend(); return; }
      // Only a draft still as it was sent goes; one edited while the request was out stays, as written since.
      const now = new Map(items());
      for (const [k, v] of sent) if (now.get(k) === v) drafts.delete(k);
      keep(); repaintAll(); paintSend();
      why.textContent = `Sent to ${o.name}.`; why.hidden = false;
    }));
    side.append(send);
    const one = el("div", "blk"), lede = (story && story.labels && story.labels["What changed for a user"]) || "";
    one.append(el("b", null, "What changed for a user"));
    if (lede) { const first = block(lede)[0]; if (first) { first.classList.add("clamp"); one.append(first); } } else one.append(el("p", "gap", sp ? "Not written by the team" : "No report names this change."));
    if (storyHref) { const a = el("a", "more", "Read the story"); a.href = storyHref; one.append(a); }
    side.append(one);
    side.append(el("h3", null, cx ? `Review${cx.at ? `, at ${String(cx.at).slice(0, 7)}` : ""}${cx.current || !cx.read ? "" : " (an earlier head)"}` : "Review"));
    if (cx && cx.summary) { const sm = el("p", "blk clamp"); sm.append(...inline(cx.summary, true)); side.append(sm); }
    if (cx) { const a = el("a", "more", "Read the review"); a.href = path("pr", pr.repo, String(pr.n), "review"); side.append(a); }
    if (!finds.length) side.append(el("p", "gap", !cx ? `No review found in ${records} records.` : cx.read ? "No findings." : "Its findings are not in a format Atlas reads, so they are not counted here."));
    for (const f of finds) {
      const c = el("div", f.fixed ? "cf fixed" : "cf"), loc = el("div", "loc", `${f.path}:${f.line}`);
      c.append(el("b", null, f.id), ` ${f.title}`, loc);
      if (f.unchanged_line) loc.append(" ", el("span", "un", "unchanged line"));
      if (f.fixed) loc.append(" ", el("span", null, "fixed"));
      c.dataset.jump = keyOf("new", f.line, f.path);
      c.dataset.file = f.path;
      side.append(c);
    }
    if (cx && cx.unparsed) side.append(el("p", "gap", `${cx.unparsed} more in the review could not be read here.`));

    const grid = el("div", "grid");
    grid.append(tree, main, side);
    root.replaceChildren(head, grid);
    paintSend();

    // ---- Events ----
    const setMode = (m) => { mode.v = m; bU.classList.toggle("on", m === "unified"); bS.classList.toggle("on", m === "split"); repaintAll(); };
    bU.addEventListener("click", () => setMode("unified"));
    bS.addEventListener("click", () => setMode("split"));
    const secOf = (p) => sections.find((s) => s.f.path === p);
    grid.addEventListener("click", async (e) => {
      const t = e.target.closest("[data-gap],[data-c],[data-del],[data-jump],[data-more]");
      if (!t) return;
      if (t.dataset.more) return load(secOf(t.dataset.more), st.get(t.dataset.more).shown);
      if (t.dataset.gap) {
        const key = t.dataset.gap, [idx, part] = key.split(":"), sec = sections[+idx];
        open.add(key);
        if (part === "all") { paint(sec); return load(sec); }
        return expand(sec);
      }
      if (t.dataset.c) {
        if (!drafts.has(t.dataset.c)) drafts.set(t.dataset.c, "");
        const sec = secOf(split(t.dataset.c).path); paint(sec); paintSend();
        return sec.body.querySelector(`textarea[data-k="${CSS.escape(t.dataset.c)}"]`)?.focus();
      }
      if (t.dataset.del) {
        const k = t.dataset.del; drafts.delete(k); keep(); paint(secOf(split(k).path)); return paintSend();
      }
      if (t.dataset.jump) {
        const sec = secOf(t.dataset.file);
        if (!sec) return;
        await load(sec);
        (sec.body.querySelector(`[data-c="${CSS.escape(t.dataset.jump)}"]`) || sec.s).scrollIntoView({ block: "center" });
      }
    });
    grid.addEventListener("input", (e) => {
      if (!e.target.dataset.k) return;
      drafts.set(e.target.dataset.k, e.target.value); keep(); paintSend();
    });
    PHONE.addEventListener("change", repaintAll);
  }

  // ---- The review queue ----
  async function queue() {
    const [r] = await Promise.all([get("/api/review"), readNames()]);
    if (!r) return say("Atlas could not read the queue just now", "Reload to try again.");
    document.title = "Review - Atlas";
    const inv = await get("/api/change?team=devops&name=2026-10-31-repo-inventory").then((x) => x && path("change", "devops", "2026-10-31-repo-inventory"), () => null);
    const page = el("div", "q");
    page.append(el("h1", null, "Review"), el("p", "sub", `${r.waiting_for_review} ${r.waiting_for_review === 1 ? "change waits" : "changes wait"} for your review.`));
    const rowOf = (x, why) => {
      const href = path("pr", x.repo, String(x.n)), a = el("div", "qr"), g = MD.gh(r.repos, x.repo, x.number, x.branch), who = tint(el("span", "who"), x.team), cnt = el("span", "cnt");
      teamDots(who, x).append(teamLabel(x), ` · ${x.repo} ${x.number ? `#${x.number}` : x.branch}`, ...(worker(x) ? [` · ${worker(x)}`] : []), ...(g ? [" · ", g] : []));
      const t1 = el(href ? "a" : "span", "t", heading(x));
      if (href) t1.href = href;
      a.append(who, t1);
      if (why) a.append(el("span", "w", why));
      const t = el("span"); t.append(el("span", "a", `+${x.totals.add}`), " ", el("span", "d", `−${x.totals.del}`));
      cnt.append(t);
      if (x.codex) { const k = Object.values(x.codex.counts || {}).reduce((a, b) => a + b, 0); cnt.append(el("span", "cx", x.codex.read ? (k ? `Review: ${k} ${k === 1 ? "finding" : "findings"}` : "Review: no findings") : "Review: findings not read")); }
      cnt.append(el("span", x.reviewed ? "r" : null, x.reviewed ? "Reviewed" : "Not yet"));
      a.append(cnt);
      return a;
    };
    const section = (title, list, empty, why) => {
      const h = el("h2", null, title), box = el("div", "rows");
      h.append(el("span", null, String(list.length)));
      if (!list.length) box.append(el("p", "quiet pad", empty));
      for (const x of list) box.append(rowOf(x, why && why(x)));
      page.append(h, box);
    };
    section("Waits on you", r.waits, "Nothing waits on you.", (x) => `Waits on you: ${x.waits[0].text}`);
    section("New since your last look", r.new, "Nothing new since your last look.");
    // Everything else, behind a toggle: grouped by repo, counted as branches (a squash merge makes a branch look open).
    const tg = el("button", "toggle", `Unmerged branches on this box (${r.rest.length})`), more = el("div");
    tg.type = "button"; tg.setAttribute("aria-expanded", "false"); more.hidden = true;
    const byRepo = {};
    for (const x of r.rest) (byRepo[x.repo] ||= []).push(x);
    more.append(el("p", "sub", "Branches not merged into their repo's base. A squash merge never makes a branch an ancestor, so some of these are merged on GitHub."));
    for (const name of Object.keys(byRepo).sort()) {
      const g = r.repos[name] || {}, d = el("details", "repo"), sm = el("summary");
      sm.append(el("b", null, `${name} (${byRepo[name].length})`));
      if (g.stale7) sm.append(el("span", "qc", `${g.stale7} untouched for 7 days or more`));
      if (g.unnamed) sm.append(el("span", "qc", `${g.unnamed} named by no report or STATUS`));
      if (inv) { const a = el("a", "qn", "Devops inventory"); a.href = inv; sm.append(a); }
      d.append(sm);
      d.addEventListener("toggle", () => {
        if (!d.open || d.querySelector(".rows")) return;
        const box = el("div", "rows");
        for (const x of byRepo[name]) box.append(rowOf(x));
        d.append(box);
      });
      more.append(d);
    }
    tg.addEventListener("click", () => { more.hidden = !more.hidden; tg.setAttribute("aria-expanded", String(!more.hidden)); });
    page.append(tg, more);
    root.replaceChildren(page);
  }

  // /pr/<repo>/<n>/review: the change's review file, read-only, as the rendered text.
  async function doc() {
    const d = await get(`/api/pr-review?${q({ repo: repoName, n: rest[0] })}`);
    if (!d) return say("No review file for this change", "Atlas reads only the one review file a team keeps for it.");
    document.title = `${d.title} - Atlas`;
    const page = el("div", "page");
    const back = el("a", "more", `Back to ${repoName} ${d.n}'s code`);
    back.href = path("pr", repoName, rest[0]);
    page.append(el("h1", null, d.title), el("p", "sub", d.path));
    // block() reads paragraphs, lists, code and tables; a heading line ends the run before it and is drawn here.
    let run = [], fence = false;
    const flush = () => { if (run.length) page.append(...block(run.join("\n"))); run = []; };
    for (const l of d.text.replace(/^# .*\n/, "").split("\n")) {
      if (/^\s*`{3}/.test(l)) fence = !fence;
      const h = !fence && /^(#{2,6}) +(.+)$/.exec(l);
      if (h) { flush(); page.append(el(h[1].length === 2 ? "h2" : "h3", null, h[2])); } else run.push(l);
    }
    flush();
    page.append(back);
    root.replaceChildren(page);
  }

  const doc_ = kind === "pr" && rest.length === 2 && rest[1] === "review";
  const run = doc_ ? doc : { pr: code, review: queue }[kind === "review" ? "review" : "pr"];
  // The first load tries once more after a second before it says so: a read that stalls once usually answers the next time.
  const load = () => (kind === "review" || kind === "pr" ? readNames().then(run) : Promise.reject());
  load().catch(() => new Promise((r) => setTimeout(r, 1000)).then(load)).catch(() => say("Atlas could not read this just now", "Reload to try again."));
}
