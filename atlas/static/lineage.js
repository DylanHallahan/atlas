// Atlas: the data part of a Story page. For a change that touches a dbt project, /api/lineage gives the changed models
// one step up and one step down, drawn by Mermaid, and each model's column changes; at phone width
// the drawing is one card per changed model. For a repo without dbt, the author's **Data.** label, source to target, as
// written. Model labels are escaped; Mermaid sanitizes its SVG in strict mode.
"use strict";

const LINEAGE = (() => {
  const { el, inline } = MD;
  const NS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  };
  const KIND = { changed: "changed in this PR", new: "new in this PR", removed: "removed in this PR", model: "model, unchanged", source: "source table" };
  const HOT = new Set(["changed", "new", "removed"]);
  const FLAG = { gained: "read added by this PR", lost: "read removed by this PR" };
  const MAX_NODES = 60, BELOW = 560;
  // A name breaks only after an underscore or a dot.
  const name = (parent, s) => { for (const part of s.split(/(?<=[_.])/)) parent.append(part, el("wbr")); return parent; };
  const mono = (s, cls) => name(el("code", cls), s);

  // Every dictionary keyed by a model name is a Map: a model may be named constructor, __proto__ or toString.
  const nodesOf = (d) => new Map(Object.entries(d.nodes));
  const push = (m, k, v) => { const a = m.get(k); if (a) a.push(v); else m.set(k, [v]); };

  // The graph the diagram draws: unchanged inputs that feed exactly the same models share one card. Null when it has
  // too many models to draw, and then the page shows the cards.
  function graph(d) {
    const N0 = nodesOf(d), E0 = d.edges;
    if (N0.size > MAX_NODES) return null;
    const fed = new Set(E0.map((e) => e[1])), outList = new Map();
    for (const [a, b, f] of E0) push(outList, a, `${b}/${f}`);
    const groups = new Map(), groupOf = new Map();
    for (const [n, nd] of N0) if ((nd.kind === "source" || nd.kind === "model") && !fed.has(n)) push(groups, `${nd.kind}:${(outList.get(n) || []).sort().join("|")}`, n);
    for (const g of groups.values()) for (const n of g) groupOf.set(n, g);
    const alias = new Map(), N = new Map();
    for (const [n, nd] of N0) {
      const g = groupOf.get(n), id = g && g.length > 1 ? g.join(" + ") : n;
      alias.set(n, id);
      N.set(id, { ...nd, members: g && g.length > 1 ? g : [n] });
    }
    const seen = new Map();
    for (const [a, b, f] of E0) seen.set(`${alias.get(a)}\u0000${alias.get(b)}`, f);
    const E = [...seen].map(([k, f]) => [...k.split("\u0000"), f]);
    return { N, E };
  }

  // Encoded backslashes escape Markdown after Mermaid's lexer. HTML delimiters use its own escaping;
  // empty spans separate dollar pairs so literal model names never enter the math renderer.
  const escapeLabel = (s) => String(s).replace(/[!-/:-@[-`{-~\r\n]/g, (c) => {
    const entity = `#${c.charCodeAt(0)};`;
    return c === "$" ? `${entity}<span></span>` : (c > " " && !"&<>".includes(c) ? "#92;" : "") + entity;
  });
  function source(g) {
    const css = getComputedStyle(document.documentElement), token = (n) => css.getPropertyValue(`--${n}`).trim();
    const ids = new Map([...g.N.keys()].map((n, i) => [n, `n${i}`])), lines = ["flowchart TD"];
    for (const [n, nd] of g.N) lines.push(`  ${ids.get(n)}["${nd.members.map((m) => m.split(/(?<=[_.])/).map(escapeLabel).join("<wbr>")).join("<br>")}<br><small>${escapeLabel(nd.unreadable ? "could not be read" : KIND[nd.kind])}</small>"]:::${nd.kind}`);
    g.E.forEach(([a, b, flag], i) => {
      lines.push(`  ${ids.get(a)} ${flag === "lost" ? "-.->" : "-->"} ${ids.get(b)}`);
      if (flag) lines.push(`  linkStyle ${i} stroke:${token(flag === "gained" ? "green" : "clay")},stroke-width:2px`);
    });
    for (const [kind, stroke] of [["changed", "clay"], ["new", "green"], ["removed", "clay"], ["model", "line"], ["source", "line"]]) {
      lines.push(`  classDef ${kind} fill:${token(kind === "source" ? "soft" : "card")},stroke:${token(stroke)},stroke-width:${HOT.has(kind) ? 2 : 1}px,color:${token("ink")}${kind === "removed" ? ",stroke-dasharray:4 3" : ""}`);
    }
    return lines.join("\n");
  }

  let library, diagramId = 0;
  function loadMermaid() {
    if (!library) library = new Promise((resolve, reject) => {
      const script = el("script");
      script.src = "/static/mermaid-11.4.1.min.js";
      script.onload = () => resolve(window.mermaid);
      script.onerror = () => reject(new Error("Mermaid could not be loaded"));
      document.head.append(script);
    });
    return library;
  }

  // One card per changed model: what it reads and what reads it.
  function cards(d) {
    const box = el("div", "lin-cards"), nodes = nodesOf(d), reads = new Map(), readBy = new Map();
    for (const e of d.edges) { push(reads, e[1], e); push(readBy, e[0], e); }
    const hot = (n) => (nodes.has(n) && HOT.has(nodes.get(n).kind) ? nodes.get(n).kind : "");
    for (const n of [...nodes.keys()].sort()) {
      const nd = nodes.get(n);
      if (!HOT.has(nd.kind)) continue;
      const c = el("div", `pm is-${nd.kind}`), rs = reads.get(n) || [], bs = readBy.get(n) || [];
      c.append(mono(n, "n"), el("div", "k", nd.unreadable ? "could not be read" : KIND[nd.kind]), el("div", "lbl", "Reads"));
      const li = (x, extra) => { const l = el("li"); l.append(mono(x), ...(extra ? [` (${extra})`] : [])); return l; };
      const a = el("ul");
      a.append(...rs.map((e) => li(e[0], [hot(e[0]), FLAG[e[2]]].filter(Boolean).join(", "))));
      if (!rs.length) a.append(el("li", null, nd.unreadable ? "Could not be read" : nd.kind === "removed" ? "Removed" : "Nothing in this project"));
      const b = el("ul");
      b.append(...bs.map((e) => li(e[1], hot(e[1]))));
      if (!bs.length) b.append(el("li", null, "Nothing in this project"));
      c.append(a, el("div", "lbl", "Read by"), b);
      box.append(c);
    }
    return box;
  }

  function legend() {
    const l = el("div", "legend");
    for (const [cls, text] of [["changed", "changed in this PR"], ["new", "new in this PR"], ["removed", "removed in this PR"], ["source", "source table"]]) {
      const s = el("span");
      s.append(el("b", `is-${cls}`), text);
      l.append(s);
    }
    l.append(el("span", null, "From ref() and source() in the model SQL at the PR's head. Green line: a read this PR adds. Dotted line: a read it removes."));
    return l;
  }

  function schema(rows) {
    const out = [];
    for (const s of rows) {
      const c = el("div", "card sch");
      c.append(mono(s.model, "n"), el("span", `kindtag is-${s.kind}`, KIND[s.kind]));
      if (s.state === "unreadable") c.append(el("p", "gap", "Column list could not be read"));
      else if (s.state === "none") c.append(el("p", "gap", "No column list in the project"));
      else if (!s.added.length && !s.removed.length) c.append(el("p", "note", `No column changes: ${s.kept} kept, from the model's .yml at base and head.`));
      else {
        c.append(el("p", "note", `${s.added.length} added, ${s.removed.length} removed, ${s.kept} kept, from the model's .yml at base and head.`));
        const t = el("table"), head = el("tr");
        head.append(el("th", null, `Removed (${s.removed.length})`), el("th", null, `Added (${s.added.length})`));
        t.append(head);
        for (let i = 0; i < Math.max(s.added.length, s.removed.length); i++) {
          const r = el("tr"), x = el("td", "rem"), y = el("td", "add");
          if (s.removed[i]) x.append(mono(s.removed[i]));
          if (s.added[i]) y.append(mono(s.added[i]));
          r.append(x, y);
          t.append(r);
        }
        c.append(t);
      }
      out.push(c);
    }
    return out;
  }

  // A repo without dbt: each line of the author's Data label, "source -> target", as written: the source, a line down
  // to the target. A line without an arrow is shown as written.
  function data(text) {
    const box = el("div", "card flows");
    for (const ln of String(text).split("\n").map((l) => l.trim().replace(/^[-*]\s+/, "")).filter(Boolean)) {
      // One code span around the whole line (`a -> b`) becomes one around each side.
      const wrapped = /^`[^`]+`$/.test(ln), m = (wrapped ? ln.slice(1, -1) : ln).match(/^(.+?)\s*(?:->|→)\s*(.+)$/);
      if (m && wrapped) { m[1] = `\`${m[1]}\``; m[2] = `\`${m[2]}\``; }
      if (!m) { const p = el("p", "words"); p.append(...inline(ln)); box.append(p); continue; }
      const f = el("div", "flow"), a = el("div", "tbl"), z = el("div", "tbl");
      a.append(...inline(m[1]));
      z.append(...inline(m[2]));
      const arrow = sv("svg", { class: "down", width: "12", height: "26", viewBox: "0 0 12 26", "aria-hidden": "true" });
      arrow.append(sv("path", { d: "M6 0V24", class: "wire" }), sv("path", { d: "M2 19L6 25L10 19", class: "head" }));
      f.append(a, arrow, z);
      box.append(f);
    }
    return box;
  }

  // The nodes the Story page adds for dbt lineage and schema: one drawing or the cards, redrawn when the width changes.
  function section(d) {
    const kids = [el("h2", null, "What the data change touches: each changed model, one step up and one step down")];
    if (d.error) return [...kids, el("p", "gap", d.error)];
    const g = graph(d), wide = el("div", "card lin"), narrow = cards(d), box = el("div");
    const theme = matchMedia("(prefers-color-scheme: dark)");
    let renderedTheme, rendering = false;
    const paint = async () => {
      if (!box.isConnected) { observer.disconnect(); theme.removeEventListener("change", paint); return; }
      const isWide = box.clientWidth >= BELOW && g !== null && g.N.size > 0;
      if (!isWide) { wide.hidden = true; narrow.hidden = false; return; }
      if (rendering) return;
      if (renderedTheme === theme.matches) { wide.hidden = false; narrow.hidden = true; return; }
      rendering = true;
      const dark = theme.matches;
      try {
        const mermaid = await loadMermaid();
        // A phone reached while the library was loading keeps its cards and does not render.
        if (box.clientWidth < BELOW || !box.isConnected) return;
        const css = getComputedStyle(document.documentElement);
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base",
          flowchart: { htmlLabels: true, curve: "linear", nodeSpacing: 28, rankSpacing: 46 },
          themeVariables: { fontFamily: css.getPropertyValue("--sans").trim(), fontSize: "15px",
            lineColor: css.getPropertyValue("--ink-3").trim(), primaryTextColor: css.getPropertyValue("--ink").trim() } });
        wide.hidden = false;
        narrow.hidden = true;
        const { svg } = await mermaid.render(`lineage-${++diagramId}`, source(g), wide);
        wide.innerHTML = svg; // Mermaid's strict renderer sanitizes all labels and the SVG.
        renderedTheme = dark;
      } catch {
        wide.hidden = true;
        narrow.hidden = false;
        return;
      } finally { rendering = false; }
      paint();
    };
    wide.hidden = true;
    box.append(wide, narrow);
    const observer = new ResizeObserver(paint);
    observer.observe(box);
    queueMicrotask(paint);
    theme.addEventListener("change", paint);
    kids.push(box, legend());
    if (d.unreadable && d.unreadable.length) kids.push(el("p", "note", `${d.unreadable.length} models in the project could not be read, so a reader of a changed model may be missing: ${d.unreadable.slice(0, 8).join(", ")}${d.unreadable.length > 8 ? ", and more" : ""}.`));
    if (!narrow.children.length) narrow.append(el("p", "gap", "No model changed in this PR."));
    kids.push(el("h2", null, "Schema change"), ...schema(d.schema));
    return kids;
  }

  return { section, data, graph, source };
})();
