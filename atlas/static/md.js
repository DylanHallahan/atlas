// Atlas: a team's words as written, as DOM nodes only (never innerHTML). Shared by the home page and the ask and
// change pages. Inline: `code`, **bold**, web links (a Markdown link by its text, a bare one shortened; a link to
// anything but http: or https:, such as a file path, is its text). Blocks: paragraphs, "- " bullets, ``` code and
// | tables |.
"use strict";

const MD = (() => {
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  // plain: for text inside a link (the shipped card), where a link may not nest: a Markdown link keeps its label as
  // text, and a bare one is dropped with the colon or spaces before it.
  const short = (u) => u.replace(/^https?:\/\//, "").replace(/^github\.com\/[^/]+\/([^/]+)\/pull\/(\d+).*/, "$1 PR $2").replace(/\/$/, "");
  const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  const TOKENS = /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s)<>]+)|\*\*([^*]+)\*\*/g;
  const inline = (s, plain) => {
    const out = [], re = new RegExp(TOKENS);
    s = String(s ?? "");
    let at = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > at) out.push(s.slice(at, m.index));
      if (m[1] != null) out.push(el("code", null, m[1]));
      else if (m[5] != null) { const b = el("b"); b.append(...inline(m[5], plain)); out.push(b); }
      // A Markdown link to anything but the web (a file path) is its label, as text.
      else if (m[2] != null && (plain || !/^https?:\/\//i.test(m[3]))) out.push(m[2]);
      else if (plain) {
        if (typeof out[out.length - 1] === "string") out[out.length - 1] = out[out.length - 1].replace(/:?\s*$/, "");
        const url = m[4].replace(/[.,;:]+$/, "");
        if (url.length < m[4].length) out.push(m[4].slice(url.length));
      } else {
        const url = (m[3] || m[4]).replace(/[.,;:]+$/, ""), a = el("a", null, m[2] || short(url));
        a.href = url;
        out.push(a);
        if (m[4] && url.length < m[4].length) out.push(m[4].slice(url.length));
      }
      at = re.lastIndex;
    }
    if (at < s.length) out.push(s.slice(at));
    if (plain && typeof out[out.length - 1] === "string") out[out.length - 1] = out[out.length - 1].replace(/[:,.\s]+$/, "");
    return out;
  };
  // One line as a team wrote it in STATUS: its first letter capitalised and a final full stop dropped.
  const line = (text, plain) => inline(cap(String(text || "").trim().replace(/\.$/, "")), plain);
  const links = (t) => [...String(t || "").matchAll(/https?:\/\/[^\s)<>\]]+/g)].map((m) => m[0].replace(/[.,;:]+$/, ""));

  // A table row's cells: split at each "|" not escaped as "\|", without the outer pipes, each trimmed.
  const cells = (r) => {
    const s = r.trim().replace(/^\|/, ""), out = [""];
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "\\" && s[i + 1] === "|") { out[out.length - 1] += "|"; i++; }
      else if (s[i] === "|") out.push("");
      else out[out.length - 1] += s[i];
    }
    if (out.length > 1 && !out[out.length - 1].trim()) out.pop(); // the closing pipe
    return out.map((c) => c.trim());
  };
  const ALIGN = /^:?-+:?$/;
  // A line that starts another block: a bullet, a code fence or a heading. It never starts or continues a table.
  const boundary = (r) => /^\s*([-*]\s|`{3}|#{1,6}\s)/.test(r);
  // A header row, then a delimiter row with as many cells ("---", ":--", "--:" or ":-:"), then the body rows up to a
  // blank line, a line with no pipe or the start of another block. A short row is padded and a long one cut to the header's width. Each table
  // sits in its own box, which scrolls sideways when it is wider than the page.
  const table = (rows, i) => {
    if (!rows[i].includes("|") || boundary(rows[i]) || i + 1 >= rows.length || !rows[i + 1].includes("-")) return null;
    const head = cells(rows[i]), delim = cells(rows[i + 1]);
    if (delim.length !== head.length || !delim.every((c) => ALIGN.test(c))) return null;
    const align = delim.map((c) => (c.endsWith(":") ? (c.startsWith(":") ? "c" : "r") : null));
    // A cell's value sits in one span, and an empty cell stays empty. A body cell carries its column's heading, as
    // text, for the card a row becomes on a phone (report.css).
    const row = (tag, vals, heads) => {
      const tr = el("tr");
      head.forEach((_, k) => {
        const td = el(tag, align[k]), v = vals[k] ?? "";
        if (heads) td.setAttribute("data-h", heads[k]);
        if (v) { const s = el("span"); s.append(...inline(v)); td.append(s); }
        tr.append(td);
      });
      return tr;
    };
    const t = el("table"), thead = el("thead"), tbody = el("tbody"), top = row("th", head), heads = [...top.cells].map((c) => c.textContent);
    thead.append(top);
    let j = i + 2;
    for (; j < rows.length && rows[j].trim() && rows[j].includes("|") && !boundary(rows[j]); j++) tbody.append(row("td", cells(rows[j]), heads));
    t.append(thead, tbody);
    const box = el("div", "table");
    box.append(t);
    // "scrolls" while part of the table lies past the box's right edge (CSS fades that edge): on scroll and resize.
    const edge = () => box.classList.toggle("scrolls", box.scrollLeft + box.clientWidth < box.scrollWidth - 1);
    box.addEventListener("scroll", edge, { passive: true });
    const fit = new ResizeObserver(edge);
    fit.observe(box);
    fit.observe(t);
    return { box, next: j - 1 };
  };

  // Several lines: a run of "- " or "* " lines is a list (an indented one continues its item), ``` fences a block of
  // code, and the other lines join into paragraphs at each blank line.
  const block = (text) => {
    const out = [], rows = String(text ?? "").replace(/\r/g, "").split("\n");
    let para = null, list = null, item = null, tb;
    const end = () => { para = null; list = null; item = null; };
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i], bullet = r.match(/^\s*[-*]\s+(.*)$/), fence = r.match(/^\s*(`{3,})/);
      if (fence) {
        // It closes only on a run of backticks at least as long as the one that opened it.
        const code = [], close = new RegExp(`^\\s*\`{${fence[1].length},}\\s*$`);
        while (++i < rows.length && !close.test(rows[i])) code.push(rows[i]);
        out.push(el("pre", null, code.join("\n")));
        end();
      } else if (!r.trim()) end();
      // Not inside a list item's indented lines: those stay with the item, as before tables were read.
      else if (!(item && /^\s/.test(r)) && (tb = table(rows, i))) {
        out.push(tb.box);
        i = tb.next;
        end();
      } else if (bullet) {
        if (!list) { list = el("ul"); out.push(list); para = null; }
        item = el("li");
        item.append(...inline(bullet[1]));
        list.append(item);
      } else if (item && /^\s/.test(r)) item.append(" ", ...inline(r.trim()));
      else {
        if (!para) { para = el("p"); out.push(para); list = null; item = null; } else para.append(" ");
        para.append(...inline(r.trim()));
      }
    }
    return out;
  };

  // A Default that goes ahead on its own: exactly "Goes ahead:" first (case as written, no markup), line wraps read as
  // spaces. Gives the words after it, its letter (when one leads) and its last comma clause as the time; else null.
  const goesAhead = (d) => {
    const flat = String(d || "").replace(/\s+/g, " ").trim();
    if (!flat.startsWith("Goes ahead:")) return null;
    const what = flat.slice(11).trim().replace(/\.$/, "");
    const m = what.match(/^([A-Za-z])\b/);
    return { what, letter: m ? m[1].toUpperCase() : "", when: what.includes(",") ? what.split(",").pop().trim() : "" };
  };

  // The Story and Code tabs of one change, on its own row under the bar: a tab shows only when the other page
  // exists, so a href is null for a page there is none of. Nothing shows unless both pages exist.
  const tabs = (on, story, code) => {
    if (!story || !code) return;
    const nav = el("nav", "ptabs");
    nav.setAttribute("aria-label", "This change");
    for (const [name, href] of [["Story", story], ["Code", code]]) {
      const a = el("a", name === on ? "on" : null, name);
      a.href = href;
      if (name === on) a.setAttribute("aria-current", "page");
      nav.append(a);
    }
    document.querySelector(".bar").after(nav);
  };

  // "Open on GitHub": the pull request when its number is digits, else the branch; built from a declared repo (a key of
  // repos, from /api/review) and that number or branch alone, each part encoded, never from free text. No link otherwise.
  const GH = "https://github.com/your-org/";
  const ghHref = (repos, repo, number, branch) => {
    if (typeof repo !== "string" || !repos || !Object.hasOwn(repos, repo)) return null;
    if (number !== null && number !== undefined && /^\d+$/.test(String(number))) return `${GH}${encodeURIComponent(repo)}/pull/${number}`;
    if (typeof branch !== "string" || !branch) return null;
    const parts = branch.split("/");
    if (parts.some((p) => p === "" || p === "." || p === "..")) return null;
    return `${GH}${encodeURIComponent(repo)}/tree/${parts.map(encodeURIComponent).join("/")}`;
  };
  const gh = (repos, repo, number, branch) => {
    const href = ghHref(repos, repo, number, branch);
    if (!href) return null;
    const a = el("a", "gh", /\/pull\/\d+$/.test(href) ? "Open on GitHub" : "Branch on GitHub");
    a.href = href; a.target = "_blank"; a.rel = "noopener";
    return a;
  };
  const declared = () => fetch("/api/review", { cache: "no-store", signal: AbortSignal.timeout(15000) }).then((r) => (r.ok ? r.json() : null)).then((q) => (q && q.repos) || {}, () => ({}));

  return { el, short, inline, line, links, block, goesAhead, tabs, gh, ghHref, declared };
})();
