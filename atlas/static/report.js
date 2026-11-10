// Atlas: the ask page (/ask/<team>/<name>), the change page (/change/<team>/<name>) and the list of changes
// (/changes, ?team=<key>), after spec 2026-10-31-ask-and-change-pages. Each shows a team's own "For Dylan" block as
// written (for-dylan-blocks.md); a gap is named with who left it, never filled in. Read-only, and every API value
// reaches the page as text or nodes (md.js).
"use strict";

{
  const { el, inline, short, block } = MD;
  const doc = document.getElementById("doc");
  const crumb = document.getElementById("crumb");
  const [, kind, team, ...rest] = location.pathname.split("/").map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  const name = rest.join("/");
  const q = (o) => new URLSearchParams(o).toString();
  // A "." or ".." segment would be resolved away by the browser and open another page, so such a name gets no link.
  const dots = (...parts) => parts.some((p) => p === "." || p === "..");
  const path = (...parts) => (dots(...parts) ? null : `/${parts.map(encodeURIComponent).join("/")}`);
  const changeHref = (t, n) => path("change", t, ...n.split("/"));

  // ---- Times, in ET: "Sat 10-31, 5:12 PM ET", and how long since ----
  const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "2-digit", day: "2-digit", hour: "numeric", minute: "2-digit" });
  // A bare "YYYY-MM-DD" (a date from frontmatter or the file name, with no time written) shows as the date alone.
  const DATE = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "2-digit", day: "2-digit" });
  const day = (iso) => {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "";
    const bare = /^\d{4}-\d{2}-\d{2}$/.test(iso);
    const p = Object.fromEntries((bare ? DATE : ET).formatToParts(d).map((x) => [x.type, x.value]));
    return bare ? `${p.weekday} ${p.month}-${p.day}` : `${p.weekday} ${p.month}-${p.day}, ${p.hour}:${p.minute} ${p.dayPeriod} ET`;
  };
  const waited = (iso) => {
    const m = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 60000)), h = Math.floor(m / 60);
    return h >= 48 ? `${Math.floor(h / 24)} days` : h ? `${h} h ${m % 60} min` : `${Math.max(1, m)} min`;
  };

  async function get(url) {
    const res = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(6000) });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }
  // Team names from /api/state; a team's key stands in when that read fails.
  let names = {};
  const teamName = (key) => (Object.hasOwn(names, key) ? names[key] : key);
  let state = null; // the last /api/state, for team names and, on an ask page, whether the lead can take a reply
  const readNames = () => get("/api/state").then((s) => { state = s; names = (s && s.teams) || {}; }, () => {});

  // ---- Shared parts ----
  const h2 = (t) => el("h2", null, t);
  const gap = (t) => el("p", "gap", `Not written by ${teamName(t)}`);
  // A label's words as written, or its gap.
  const label = (r, key, cls) => {
    const v = r.labels[key], d = el("div", cls || "words");
    d.append(...(v ? block(v) : [gap(r.team)]));
    return d;
  };
  const kicker = (t, ...bits) => {
    const k = el("div", "kicker"), who = el("span", "who");
    k.style.setProperty("--tc", `var(--t-${CSS.escape(t)}, var(--ink-3))`); // escaped: a key is never CSS
    who.append(el("i"), ...bits.shift());
    k.append(who, ...bits);
    return k;
  };
  const codePath = (p) => { const c = el("code", null, p); return c; };
  const foot = (r, ...more) => {
    const f = el("p", "foot");
    f.append(...more, "From ", codePath(r.path), ".");
    return f;
  };

  // A screenshot: through /api/report-file only, and only an image inside the team's folders; else said plainly.
  const IMG = /\.(png|jpe?g|webp|gif)$/i;
  function shot(r, s, cap) {
    const f = el("figure", "shot"), c = el("figcaption");
    if (cap) c.append(el("b", null, `${cap}.`), " ");
    c.append(codePath(s.written));
    const missing = (why) => { f.replaceChildren(el("p", "gap", why), c); };
    if (!s.inside || !s.path) missing(`This screenshot lies outside ${teamName(r.team)}'s folders, so Atlas does not open it.`);
    // A report named as a path (Look at) is a quiet path, as the report's own is at the foot.
    else if (/\.md$/i.test(s.path)) { const p = el("p", "path"); p.append(codePath(s.written)); return p; }
    else if (!IMG.test(s.path)) missing("This is not an image, so it is not shown.");
    else {
      // In a frame no taller than about 70% of the window, cut from the top; a tap opens the whole image.
      const img = el("img"), frame = el("a", "frame");
      frame.href = img.src = `/api/report-file?${q({ team: r.team, path: s.path })}`;
      img.alt = cap ? `${cap}: ${s.written}` : s.written;
      img.addEventListener("error", () => missing("This screenshot does not open."));
      // The fade and the caption's note say so only while the image is cut: set on load, and again whenever the frame
      // or the image changes size (a rotation, a resized window).
      const note = el("span", null, " Cut to fit; tap it to see the whole screen.");
      const mark = () => {
        const cut = img.offsetHeight > frame.clientHeight + 1;
        frame.classList.toggle("cut", cut);
        if (cut) c.append(note); else note.remove();
      };
      img.addEventListener("load", mark);
      const fit = new ResizeObserver(mark);
      fit.observe(frame);
      fit.observe(img);
      frame.view = () => ({ src: frame.href, alt: img.alt, caption: `${cap ? `${cap}. ` : ""}${s.written.split("/").pop()}` }); // in the viewer: the label and the file's name
      // A plain click opens the viewer; a modified click or a middle click keeps the link, the raw file in a new tab.
      frame.addEventListener("click", (e) => {
        if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        openViewer(frame);
      });
      frame.append(img);
      f.append(frame, c);
    }
    return f;
  }
  // ---- Screenshots at a glance (F5): a strip of thumbnails first in Context, one click to enlarge ----
  // A shot Atlas shows as an image: inside the team's folders, an image type. Anything else keeps shot()'s plain note.
  const showable = (s) => Boolean(s.inside && s.path && IMG.test(s.path));
  const plain = (t) => t.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[*`]/g, "").replace(/\s+/g, " ").trim();
  // The label's text without the backticked paths of the shots in `list`, and each such shot's caption: the words of
  // the line it stood on (its bullet), the marker and a trailing colon off. A line left with no words is dropped.
  function lift(text, list) {
    const own = new Map(list.map((s) => [s.written, s])), caps = new Map(), kept = [];
    for (const ln of String(text || "").split("\n")) {
      const hit = [];
      const rest = ln.replace(/`([^`\s]+)`/g, (m, w) => (own.has(w) ? (hit.push(w), "") : m));
      if (!hit.length) { kept.push(ln); continue; }
      const words = plain(rest.replace(/^\s*(?:[-*]|\d+\.)\s+/, "")).replace(/[\s:,;-]+$/, "");
      for (const w of hit) caps.set(w, words);
      if (words) kept.push(rest.replace(/[ \t]+$/, "").replace(/[ \t]*[:,;]$/, ""));
    }
    return { text: kept.join("\n").replace(/\n{3,}/g, "\n\n").trim(), caps };
  }
  // A screenshot sent with an answer is opened by its upload id, never by its path.
  const fileUrl = (r, s) => (s.upload ? `/api/upload?${q({ id: s.upload })}` : `/api/report-file?${q({ team: r.team, path: s.path })}`);
  // The viewer: one view over the page for every screenshot. It steps through the page's viewable images in document
  // order (collected at open), zooms around the pointer or the fingers (one translate and scale on the image), and pans.
  // Nothing touches the URL or the scroll position. Opened by a thumbnail or by a change screen's frame.
  const VIEW = ".thumb, .shot .frame";
  const viewables = () => [...document.querySelectorAll(VIEW)].filter((n) => n.querySelector("img")).map((n) => ({ node: n, ...n.view() }));
  function openViewer(from) {
    const all = viewables();
    enlarge(all, Math.max(0, all.findIndex((x) => x.node === from)), from);
  }
  const MAX = 4, STEP = 1.5;
  function enlarge(items, start, from) {
    if (document.querySelector(".lightbox")) return; // one view at a time
    let at = start, nw = 0, nh = 0, W = 0, H = 0, s = 1, tx = 0, ty = 0, fit = 1, ease = 0, tap = null;
    const back = el("div", "lightbox"), bar = el("div", "lb-bar"), stage = el("div", "lb-stage"), img = el("img"), cap = el("p", "lb-cap");
    const count = el("span", "lb-count"), prev = el("button", null, "‹"), next = el("button", null, "›"), shut = el("button", null, "Close");
    for (const [b, label] of [[prev, "Previous image"], [next, "Next image"], [shut, "Close"]]) { b.type = "button"; b.setAttribute("aria-label", label); }
    back.setAttribute("role", "dialog");
    back.setAttribute("aria-modal", "true");
    back.setAttribute("aria-label", "Screenshot");
    back.tabIndex = -1; // focus moves into the view, so nothing behind it takes keys
    const many = items.length > 1;
    bar.append(...(many ? [count, prev, next] : []), shut);
    stage.append(img);
    back.append(bar, stage, cap);
    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    // Keep the image on the stage: an axis that fits stays centred, a larger one stops with its edge on the stage's.
    const settle = () => {
      const w = nw * s, h = nh * s;
      tx = w <= W ? (W - w) / 2 : clamp(tx, W - w, 0);
      ty = h <= H ? (H - h) / 2 : clamp(ty, H - h, 0);
    };
    const paint = (eased) => {
      clearTimeout(ease);
      img.classList.toggle("easing", Boolean(eased));
      if (eased) ease = setTimeout(() => img.classList.remove("easing"), 300);
      img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
      stage.classList.toggle("zoomed", s > fit + 0.001);
    };
    const lay = (keepFit) => {
      const r = stage.getBoundingClientRect();
      W = r.width; H = r.height;
      if (!nw || !W) return;
      img.style.visibility = "";
      const was = Math.abs(s - fit) < 0.001;
      fit = Math.min(1, (W - 32) / nw, (H - 32) / nh);
      s = keepFit || was ? fit : clamp(s, fit, MAX);
      settle();
      paint(false);
    };
    // Zoom to scale z, keeping the stage point (px, py) where it is.
    const zoomAt = (z, px, py, eased) => {
      z = clamp(z, fit, MAX);
      tx = px - (px - tx) * z / s;
      ty = py - (py - ty) * z / s;
      s = z;
      settle();
      paint(eased);
    };
    const local = (e) => { const r = stage.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const onImage = (e) => { const r = img.getBoundingClientRect(); return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom; };
    const show = (i) => {
      at = i; nw = nh = 0;
      img.style.transform = "none";
      img.style.visibility = "hidden";
      img.onload = () => { nw = img.naturalWidth; nh = img.naturalHeight; img.style.width = `${nw}px`; img.style.height = `${nh}px`; lay(true); };
      img.src = items[i].src; img.alt = items[i].alt;
      if (img.complete && img.naturalWidth) img.onload();
      cap.textContent = items[i].caption; cap.hidden = !items[i].caption;
      count.textContent = `${i + 1} of ${items.length}`;
      prev.disabled = i === 0; next.disabled = i === items.length - 1;
    };
    const step = (d) => { const i = at + d; if (items[i]) show(i); };
    const close = () => {
      document.removeEventListener("keydown", keys, true);
      watch.disconnect();
      back.remove();
      from.focus({ preventScroll: true });
    };
    const watch = new ResizeObserver(() => lay(false));
    const keys = (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return; // Alt+Left is the browser's Back
      const onButton = e.target.closest?.(".lightbox button");
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
      else if (e.key === "+" || e.key === "=") zoomAt(s * STEP, W / 2, H / 2, true);
      else if (e.key === "-" || e.key === "_") zoomAt(s / STEP, W / 2, H / 2, true);
      else if (e.key === "0") { s = fit; settle(); paint(true); }
      else if (e.key === "Tab") { // Tab stays in the view, among its buttons
        const b = [...bar.querySelectorAll("button:not(:disabled)")], i = b.indexOf(document.activeElement) + (e.shiftKey ? -1 : 1);
        b[(i + b.length) % b.length].focus();
      }
      else if ((e.key === " " || e.key === "Enter") && onButton) return; // a focused button works as a button
      else if (!["Enter", " ", "PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(e.key)) return; // the page behind does not scroll
      e.preventDefault();
      e.stopImmediatePropagation(); // the arrows step the pictures here, not the asks behind
    };
    // The wheel and a trackpad pinch (which arrives as a wheel with ctrl) zoom around the pointer, following the hand.
    stage.addEventListener("wheel", (e) => {
      const [px, py] = local(e), d = e.deltaY * (e.deltaMode === 1 ? 16 : 1);
      zoomAt(s * Math.exp(-d * (e.ctrlKey ? 0.01 : 0.002)), px, py, false);
    }, { passive: false });
    back.addEventListener("wheel", (e) => e.preventDefault(), { passive: false });
    // Pointers: one drags a zoomed image, two pinch it. A tap on the dark area closes; two quick taps on the image zoom.
    const down = new Map();
    let moved = 0, started = null, pinch = 0;
    stage.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      stage.setPointerCapture(e.pointerId);
      down.set(e.pointerId, [e.clientX, e.clientY]);
      if (down.size === 1) { moved = 0; started = onImage(e); }
      else started = false;
      if (down.size === 2) { const [a, b] = [...down.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); }
      e.preventDefault();
    });
    stage.addEventListener("pointermove", (e) => {
      const p = down.get(e.pointerId);
      if (!p) return;
      const dx = e.clientX - p[0], dy = e.clientY - p[1];
      moved += Math.abs(dx) + Math.abs(dy);
      if (down.size === 2) {
        const [a, b] = [...down.entries()].map(([id, v]) => (id === e.pointerId ? [e.clientX, e.clientY] : v));
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]), r = stage.getBoundingClientRect();
        const mx = (a[0] + b[0]) / 2 - r.left, my = (a[1] + b[1]) / 2 - r.top;
        if (pinch) zoomAt(s * d / pinch, mx, my, false);
        tx += dx / 2; ty += dy / 2; // the fingers' midpoint moves by half of each finger's move
        settle(); paint(false);
        pinch = d;
      } else if (s > fit + 0.001) { tx += dx; ty += dy; settle(); paint(false); }
      p[0] = e.clientX; p[1] = e.clientY;
    });
    const release = (e) => {
      if (!down.delete(e.pointerId)) return;
      if (down.size === 1) moved = 99; // a pinch that ends with one finger down is no tap
      if (down.size || e.type === "pointercancel") { tap = null; return; }
      if (moved >= 6) { tap = null; return; }
      if (!started && !onImage(e)) { close(); return; } // a press and a release both on the dark area
      if (!started || !onImage(e)) return;
      const now = performance.now();
      if (tap && now - tap[0] < 350 && Math.hypot(e.clientX - tap[1], e.clientY - tap[2]) < 30) {
        tap = null;
        const [px, py] = local(e);
        if (s > fit + 0.001) { s = fit; settle(); paint(true); }
        else zoomAt(fit < 1 ? 1 : 2, px, py, true);
      } else tap = [now, e.clientX, e.clientY];
    };
    stage.addEventListener("pointerup", release);
    stage.addEventListener("pointercancel", release);
    prev.addEventListener("click", () => step(-1));
    next.addEventListener("click", () => step(1));
    shut.addEventListener("click", close);
    document.addEventListener("keydown", keys, true);
    document.body.append(back);
    watch.observe(stage);
    show(start);
    back.focus({ preventScroll: true });
  }
  // The strip: each thumbnail a button (so the keyboard opens it too), its caption under it cut at two lines. A picture
  // that does not open drops out of the strip and shows as shot() shows it, with its path.
  function strip(r, list, caps, fallback, failed) {
    const row = el("div", "strip");
    for (const s of list) {
      const caption = caps.get(s.written) || "", src = fileUrl(r, s), alt = caption || s.written;
      const b = el("button", "thumb"), img = el("img"), c = el("span", "tcap", caption);
      b.type = "button";
      b.title = caption;
      img.src = src;
      img.alt = alt;
      img.addEventListener("error", () => {
        b.remove();
        const f = el("figure", "shot"), note = el("figcaption");
        note.append(codePath(s.written));
        f.append(el("p", "gap", "This screenshot does not open."), note);
        fallback.append(f);
        fallback.hidden = false;
        if (!row.children.length) row.remove();
        failed(s);
      });
      b.append(img, c);
      b.view = () => ({ src, alt, caption });
      b.addEventListener("click", () => openViewer(b));
      row.append(b);
    }
    return row;
  }
  // The screenshots sent with his answer, in the same strip and the same viewer as an ask's own.
  const answerShots = (r, list) => strip(r, list.map((x) => ({ upload: x.id, path: x.path, written: x.path })), new Map(), el("div"), () => {});
  const bare = (t) => String(t || "").replace(/^(report|ask)\s*:\s*/i, ""); // the rest as written, case and all
  const page = (title, kids) => {
    document.title = `${title} - Atlas`;
    doc.replaceChildren(...kids);
  };
  const fail = (msg, p) => { const kids = [el("h1", null, msg)]; if (p) kids.push(el("p", "foot", p)); page(msg, kids); };

  // ---- Ask navigation: one snapshot of Needs you when this page opens ----
  const sendTime = (iso) => `${new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "numeric", minute: "2-digit",
  }).format(new Date(iso))} ET`;
  function stepper(order, id) {
    const index = order.findIndex((x) => x.id === id), nav = el("nav", "ask-stepper");
    nav.setAttribute("aria-label", "Step through asks");
    const previous = el("button", null, "‹ Previous"), next = el("button", null, "Next ›");
    const move = (i) => {
      if (!order[i]?.href) return;
      try { if (i < index) sessionStorage.setItem("ask-dir", "back"); } catch {} // the next page reads it to slide the other way
      location.assign(order[i].href);
    };
    previous.type = next.type = "button";
    previous.disabled = index <= 0 || !order[index - 1]?.href;
    next.disabled = !order[index + 1]?.href;
    previous.addEventListener("click", () => move(index - 1));
    next.addEventListener("click", () => move(index + 1));
    // An ask outside the set (answered, or no longer asked) shows no position: Next goes to the first open ask.
    nav.append(previous, ...(index >= 0 ? [el("span", null, `${index + 1} of ${order.length}`)] : []), next);
    nav.hidden = index < 0 && !order.length;
    document.addEventListener("keydown", (e) => {
      if (e.defaultPrevented || e.isComposing || e.repeat || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey ||
          e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.closest(".answer .options")) return;
      const button = e.key === "ArrowLeft" ? previous : e.key === "ArrowRight" ? next : null;
      if (button && !button.disabled) { e.preventDefault(); button.click(); }
    });
    return nav;
  }
  async function sentNotice() {
    const url = new URL(location.href);
    if (!url.searchParams.has("sent")) return null;
    const sent = url.searchParams.get("sent");
    url.searchParams.delete("sent");
    history.replaceState(history.state, "", url);
    if (!sent) return null;
    // Only the id crosses the page change. The choice, question, lead and time come from the server.
    const split = sent.indexOf("/"), t = sent.slice(0, split), n = sent.slice(split + 1);
    const href = split > 0 && ASKS.href(t, n);
    if (!href) return null;
    try {
      const r = await get(`/api/ask?${q({ team: t, name: n })}`), a = r?.answer;
      if (!a?.sent_at || !a.pick) return null;
      const line = el("p", "ask-sent"), open = el("a", null, "Open it");
      const question = Array.from(r.labels.Question || bare(r.title)).slice(0, 60).join("");
      open.href = href;
      line.append(`Sent ${a.pick === "other" ? "Something else" : a.pick} on "${question}" to ${a.lead || r.lead} at ${sendTime(a.sent_at)}. `, open);
      return line;
    } catch { return null; } // a failed read never invents confirmation
  }
  function lastAnswer(box, answer, lead) {
    const home = el("a", null, "Home"); home.href = "/";
    const detail = el("p", "answer-done-detail", `Sent to ${answer.lead || lead} at ${sendTime(answer.sent_at)}`);
    box.replaceChildren(el("p", null, "That was the last one. Nothing else waits on your answer."), detail, home);
    box.classList.add("answer-done");
  }

  // ---- The ask page ----
  async function ask() {
    crumb.textContent = "Needs you";
    const [r, brief, notice] = await Promise.all([get(`/api/ask?${q({ team, name })}`), get("/api/briefing"), sentNotice()]);
    if (!r) return fail("No such ask", `Atlas has no report ${name} for ${team}.`);
    // The stepper steps through and counts what Needs you lists: the asks with no answer.
    const order = ASKS.order(brief).filter((k) => !k.ask.answer), tn = teamName(r.team), L = r.labels || {}, kids = [stepper(order, r.ask_id)];
    let showComplete = null;
    // The answer box and the Reply box are for Dylan only (type.js), and only when the lead can be named and reached.
    const typist = typeof SEND !== "undefined" && (await SEND.typist());
    const mayAnswer = Boolean(typist && r.lead && r.has_ask && r.ask_id && path("agent", r.lead));
    const wait = el("span", "wait");
    const tick = () => { wait.textContent = r.asked_at ? `waiting ${waited(r.asked_at)}` : ""; };
    const asked = el("span");
    // Asked as a time when it parses, else as written (then no waited time), else its gap.
    asked.append(...(r.asked_at ? [`asked ${day(r.asked_at)}`] : L.Asked ? ["asked ", ...inline(L.Asked)] : ["asked: ", el("span", "gap", `Not written by ${tn}`)]));
    const k = kicker(r.team, [r.linked ? `${tn} needs you` : tn], ...(r.has_ask ? [asked] : []));
    if (r.linked && r.asked_at) { k.append(wait); tick(); setInterval(tick, 30000); }
    kids.push(k);
    if (!r.linked && !r.answer) {
      const n = el("div", "notice");
      n.append(el("b", null, "No longer asked."), ` ${tn}'s STATUS no longer links this ask, so it can't be answered here.`);
      kids.push(n);
    }
    if (!r.has_ask) {
      const p = el("p", "lede");
      p.append(`${tn} linked this report, but it has no ask block yet.`);
      kids.push(el("h1", null, bare(r.title)), p);
      if (r.status_line) {
        const s = el("div", "card words");
        s.append(el("div", "label", `${tn}'s STATUS line, as written`), ...block(r.status_line));
        kids.push(s);
      }
    } else {
      const h = el("h1");
      h.append(...(L.Question ? inline(L.Question) : [el("span", "gap", `Not written by ${tn}`)]));
      const goesLine = el("p", "goes");
      goesLine.hidden = true; // filled below once the Default is read: directly under the question
      if (notice) kids.push(notice);
      kids.push(h, goesLine);

      // Context: the strip of screenshots first (Look at's, then Evidence's), then the Background, then what to look at
      // (the words, a button for the first web link, each screenshot Atlas cannot show as a thumbnail).
      // A part the team did not write is left out, not marked.
      const ctx = [], lookShots = (r.shots || []).filter(showable), evShots = (r.evidence_shots || []).filter(showable);
      const lookText = L["Look at"] || "";
      const markable = !r.answer && Boolean(lookText.trim()) && !/https:\/\//.test(lookText) && !/^[ \t]*(?:[-*][ \t]+)?(?:No|Nothing)\b/m.test(lookText);
      const lookLift = lift(L["Look at"], lookShots), evLift = lift(L.Evidence, evShots);
      const caps = new Map([...evLift.caps, ...lookLift.caps]);
      const rest = el("div", "card look");
      const shown = [...lookShots, ...evShots].filter((s, i, a) => a.findIndex((x) => x.path === s.path) === i); // one per file
      const lookMark = el("p", "mark", "Look at: no link given");
      const failed = (s) => { if (lookShots.some((x) => x.path === s.path)) lookMark.hidden = !markable; };
      if (shown.length) ctx.push(strip(r, shown, caps, rest, failed));
      if (L.Background) ctx.push(label(r, "Background"));
      if (L.Data) ctx.push(el("div", "label data", "Data"), label(r, "Data"));
      const lookWords = lookLift.text, others = (r.shots || []).filter((s) => !showable(s));
      if (lookWords || others.length) {
        if (lookWords) rest.append(label({ ...r, labels: { ...L, "Look at": lookWords } }, "Look at"));
        // The button is for a one-line Look at; with bullets, each bullet already opens its own link.
        const url = /^\s*[-*]\s/m.test(lookWords) ? null : MD.links(lookWords)[0];
        if (url) { const a = el("a", "btn", `Open ${short(url)}`); a.href = url; rest.append(a); }
        for (const s of others) rest.append(shot(r, s));
      }
      rest.hidden = !rest.children.length; // shown again if a thumbnail fails to open and adds its note
      if (lookShots.length || evShots.length || rest.children.length) ctx.push(rest);
      // Two clay marks, on an open ask only: no Background, and a Look at with no https link (bare ids or a box path), unless a line says No or Nothing.
      // A Look at that holds only screenshots Atlas shows as images has no mark; any other path still does.
      if (!r.answer) {
        const onlyShots = lookShots.length && !others.length;
        if (!L.Background) ctx.push(el("p", "mark", "Background: not written"));
        lookMark.hidden = onlyShots || !markable; // shown again if one of Look at's thumbnails does not open
        ctx.push(lookMark);
      }
      if (ctx.length) kids.push(h2("Context"), ...ctx);

      // The options: each "- **A. Name.** words" bullet, tagged by the letter written in Recommended and Default.
      const keys = [], opts = [], cards = {};
      let tail = null, gap = false, inTail = false;
      // Line by line: a "- **A. Name.** words" line starts an option, and the lines after it are its words.
      for (const ln of String(L.Options || "").split("\n")) {
        const m = ln.match(/^[ \t]*[-*][ \t]+\*\*([A-Z])\.[ \t]*([^*]*?)\.?\*\*[ \t]*(.*)$/);
        if (m) { keys.push(m[1]); opts.push({ key: m[1], title: m[2], text: m[3] }); inTail = false; gap = false; }
        else if (opts.length) {
          // After a blank line, an unindented line that is not a bullet is not the option's words: it shows under the cards, until the next option bullet.
          if (inTail || (!ln.trim() ? (gap = true, false) : gap && !/^[ \t]|^[-*][ \t]/.test(ln))) inTail = true, tail = (tail === null ? "" : `${tail}\n`) + ln;
          else opts[opts.length - 1].text += `\n${ln}`;
        }
      }
      // Only a letter leading the label tags an option; "None, because A and B ..." tags nothing.
      const pick = (t) => { const m = String(t || "").trim().match(/^\**([A-Z])\b/); return m && keys.includes(m[1]) ? m[1] : undefined; };
      const rec = pick(L.Recommended);
      // Only a Default that starts exactly "Goes ahead:" says anything; "Holds." and older wording show nothing.
      const ga = MD.goesAhead(L.Default), def = ga && keys.includes(ga.letter) ? ga.letter : undefined;
      const answerable = mayAnswer && r.linked && opts.length, box = el("div", "options");
      if (ga) { goesLine.textContent = `Goes ahead without you: ${ga.what}`; goesLine.hidden = false; }
      if (opts.length) {
        for (const o of opts) {
          const d = el(answerable ? "label" : "div", "opt"), t = el("div", "t"), w = el("div", "words");
          cards[o.key] = d;
          t.append(...inline(o.title));
          if (o.key === rec) t.append(el("span", "tag rec", "Recommended"));
          if (o.key === def) t.append(el("span", "tag", "Goes ahead without you"));
          // The undo tag is the option's final sentence, and only when that sentence is the tag: taken off the words and
          // shown under them. A sentence starts after a full stop and any whitespace, or after a line break; one scan
          // from the end finds where, so nothing rescans the text.
          let text = o.text.trim(), undo = null, cut = 0;
          for (let i = text.length - 2; i >= 0; i--) if (text[i] === "\n" || (text[i] === "." && /\s/.test(text[i + 1]))) { cut = i + 1; break; }
          const last = text.slice(cut).trim();
          if (last === "Easy to undo." || (last.startsWith("Hard to undo:") && last.endsWith("."))) { undo = last; text = text.slice(0, cut).trim(); }
          if (text) w.append(...block(text));
          d.append(el("span", "k", o.key), t, w);
          if (undo) {
            const hard = undo.startsWith("Hard");
            d.append(el("div", "undo-row"));
            d.lastChild.append(el("span", hard ? "undo hard" : "undo", hard ? undo.replace(/\.$/, "") : "Easy to undo"));
          }
          box.append(d);
        }
      }
      // The option cards are the answer for Dylan: radios, then a note, the line shown and Send (type.js).
      if (!opts.length) kids.push(h2("Your options"), label(r, "Options", "card words"));
      else if (!answerable) kids.push(h2("Your options"), box);
      else {
        const ab = answerBox({ el, lead: r.lead, team: tn, question: L.Question || bare(r.title), askId: r.ask_id, opts, rec, def, state,
          answer: r.answer, cards, box, get: () => POLL.get(`/api/ask?${q({ team, name })}`, 6000).catch((e) => { if (e.status === 404) return null; throw e; }), thumbs: (list) => answerShots(r, list),
          onSent: async () => {
            const [current, fresh] = await Promise.all([get(`/api/ask?${q({ team, name })}`), get("/api/briefing")]);
            if (!current?.answer?.sent_at) throw new Error("Answer state unavailable");
            const live = new Map(ASKS.order(fresh).map((x) => [x.id, x]));
            const index = order.findIndex((x) => x.id === r.ask_id);
            // Search the opening order, after this ask and then from its start. New arrivals wait for a page load.
            const following = [...order.slice(index + 1), ...order.slice(0, index + 1)];
            const next = following.find((x) => x.id !== r.ask_id && x.href && live.has(x.id) && !live.get(x.id).ask.answer);
            if (next) { location.assign(`${next.href}?${q({ sent: r.ask_id })}`); return; }
            const url = new URL(location.href); url.searchParams.set("completed", "1"); url.searchParams.delete("sent");
            history.replaceState(null, "", url);
            lastAnswer(ab.node, current.answer, r.lead);
          } });
        if (new URLSearchParams(location.search).get("completed") === "1" && r.answer?.sent_at &&
            !order.some((x) => x.id !== r.ask_id && !x.ask.answer)) showComplete = () => lastAnswer(ab.node, r.answer, r.lead);
        kids.push(h2("Your answer"), ab.node);
      }
      // Anyone sees the screenshots sent with the answer; Dylan's own box shows them under the answer's state.
      if (!answerable && r.answer) {
        const a = r.answer, c = a.letter || a.pick, who = a.recorded_by || a.lead || r.lead, card = el("div", "card words");
        const you = `You answered ${c === "other" ? "Something else" : c || ""}`.trim();
        // Today's times read "at 3:33 PM ET"; another day's read as the kicker writes it, with no "at".
        const ETD = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" });
        const when = (iso) => ETD.format(new Date(iso)) === ETD.format(new Date()) ? ` at ${sendTime(iso)}` : ` ${day(iso)}`;
        card.append(el("p", null, a.state === "recorded"
          ? `${you}${a.sent_at ? when(a.sent_at) : ""}. ${who} recorded it${when(a.recorded_at)}.`
          : `${you}${when(a.sent_at)}. ${who} has not recorded it yet.`));
        kids.push(h2("Your answer"), card);
        if (a.shots?.length) kids.push(answerShots(r, a.shots));
      }
      if (tail !== null && tail.trim()) { const d = el("div", "words tail"); d.append(...block(tail.trim())); box.append(d); }
      kids.push(h2("Recommendation"), label(r, "Recommended", "rec-box words"), ...(evLift.text || !L.Evidence ? [h2("The evidence"), label({ ...r, labels: { ...L, Evidence: evLift.text } }, "Evidence", "card words")] : []));
      // Asks written before the undo tags keep their old section, in its old place; a new ask has none and no gap.
      if (L["Cost of being wrong"]) kids.push(h2("The cost of being wrong"), label(r, "Cost of being wrong", "card words"));
    }

    // The lead's terminal.
    const term = el("div", "card words");
    if (r.lead) {
      const href = path("agent", r.lead), a = el(href ? "a" : "span", "btn ghost", `Open ${r.lead}'s terminal`);
      if (href) a.href = href;
      term.append(el("p", null, `Questions go to ${r.lead}, who owns this ask. Its terminal shows what it is doing.`), a);
    } else term.append(el("p", "gap", `${tn} has no lead named in the structure file.`));
    // For Dylan, a reply box in its place (type.js); everyone else keeps the terminal link.
    if (mayAnswer) {
      // One card for questions: its words, then the Reply box (which carries the link to the terminal).
      term.replaceChildren(el("p", null, `Questions go to ${r.lead}, who owns this ask. Write to it in the Reply box, or open its terminal to type there or see what it is doing.`),
        replyBox({ el, lead: r.lead, question: L.Question || bare(r.title), state, bare: true }));
      kids.push(h2("Ask before you decide"), term, foot(r));
    } else kids.push(h2("Ask before you decide"), term, foot(r));
    page(L.Question || bare(r.title), kids);
    if (showComplete) showComplete();
  }

  // ---- The data part of a change page ----
  async function dataPart(slot, r) {
    const label = r.labels.Data;
    const own = () => { if (label) slot.replaceChildren(h2("Data"), LINEAGE.data(label)); };
    if (!r.review) return own();
    try {
      const d = await get(`/api/lineage?${q({ repo: r.review.repo, n: r.review.n })}`);
      if (!d) return own();
      if (d.dbt === false) return own();
      slot.replaceChildren(...(d.dbt === null ? [h2("Data"), el("p", "gap", d.error || "Lineage could not be read just now")] : LINEAGE.section(d)));
    } catch {
      slot.replaceChildren(h2("Data"), el("p", "gap", "Lineage could not be read just now"));
    }
  }

  // ---- The change page ----
  async function change() {
    crumb.textContent = "Changes";
    const r = await get(`/api/change?${q({ team, name })}`);
    if (!r) return fail("No such change", `Atlas has no report ${name} for ${team}.`);
    const tn = teamName(r.team), kids = [];
    // Story and Code, joined: the Code page exists when the report names a change that is open on this box.
    MD.tabs("Story", path("change", r.team, ...r.name.split("/")), r.review && path("pr", r.review.repo, String(r.review.n)));
    const when = r.written_at && r.written_src !== "none" ? el("span", null, `written ${day(r.written_at)}`) : el("span", "gap", "No written time in the report");
    const all = el("a", null, `All ${tn} changes`);
    all.href = `/changes?${q({ team: r.team })}`;
    const ghl = r.review && MD.gh(await MD.declared(), r.review.repo, r.review.number, r.review.branch);
    kids.push(kicker(r.team, [tn], when, all, ...(ghl ? [ghl] : [])), el("h1", null, bare(r.title)));
    if (!r.has_change) {
      const slot = el("div", "data");
      kids.push(el("p", "lede", "No For Dylan section."), slot, foot(r));
      page(bare(r.title), kids);
      return dataPart(slot, r); // the change this report links still has its lineage
    }
    // The screens: a before and after pair, stacked full width under the lede; the others last, in the order written.
    const shots = r.shots || [];
    const before = shots.find((s) => /before/i.test(s.written)), after = shots.find((s) => /after/i.test(s.written));
    const pair = before && after && before !== after ? [before, after] : [];
    kids.push(label(r, "What changed for a user", "lede words"));
    if (pair.length) {
      const box = el("div", "shots");
      box.append(shot(r, before, "Before"), shot(r, after, "After"));
      kids.push(h2("Before and after"), box);
    }
    kids.push(h2("Why"), label(r, "Why", "card words"),
      h2("How it was checked"), label(r, "How it was checked", "card words"));
    const not = el("div", "not");
    not.append(el("div", "label", "Not checked"), ...(r.labels["Not checked"] ? block(r.labels["Not checked"]) : [gap(r.team)]));
    kids.push(not, h2("The code, by what it does"), label(r, "The code", "card words code-list"));

    // The data: dbt lineage and schema from the change's git objects, read when this page opens (not before the page
    // shows); for a repo without dbt, the author's Data label as written, else nothing.
    const slot = el("div", "data");
    kids.push(slot);
    dataPart(slot, r);

    const others = shots.filter((s) => !pair.includes(s));
    if (!shots.length) kids.push(h2("Screens"), label(r, "Screens", "card words"));
    else if (others.length) {
      const box = el("div", "shots");
      box.append(...others.map((s) => shot(r, s)));
      kids.push(h2(pair.length ? "Other screens" : "Screens"), box);
    }
    const more = [];
    if (r.pr && /^https:\/\/github\.com\//.test(r.pr)) {
      const a = el("a", null, short(r.pr));
      a.href = r.pr;
      more.push("The pull request: ", a, ". ");
    }
    kids.push(foot(r, ...more));
    page(bare(r.title), kids);
  }

  // ---- The list of changes ----
  async function changes() {
    crumb.textContent = "Changes";
    const only = new URLSearchParams(location.search).get("team");
    const list = await get(`/api/changes${only ? `?${q({ team: only })}` : ""}`);
    const items = (list && (list.changes || list.items || list)) || [];
    const kids = [el("h1", null, only ? `Changes in ${teamName(only)}` : "Changes")];
    const sub = el("p", "lede", "Every report written or changed since Sat 10-31, 1:32 PM ET, newest first.");
    if (only) { const a = el("a", null, "All teams"); a.href = "/changes"; sub.append(" ", a); }
    kids.push(sub);
    const box = el("div", "card rows");
    for (const c of Array.isArray(items) ? items : []) {
      const href = changeHref(c.team, c.name), a = el(href ? "a" : "div", "change-row"), top = el("div", "top"), who = el("span", "who"), what = el("div", "what");
      if (href) a.href = href;
      a.style.setProperty("--tc", `var(--t-${CSS.escape(c.team)}, var(--ink-3))`);
      who.append(el("i"), teamName(c.team));
      top.append(el("time", null, c.written_at ? day(c.written_at) : "No written time"), who);
      if (!c.has_change) what.append(el("span", "gap", "No For Dylan section"), ` in ${bare(c.title)}`);
      else if (c.lede) what.append(...inline(c.lede, true));
      else what.append(el("span", "gap", `Not written by ${teamName(c.team)}`));
      a.append(top, what);
      box.append(a);
    }
    if (!box.children.length) box.append(el("p", "quiet", only ? `No change from ${teamName(only)} since then.` : "No change since then."));
    kids.push(box);
    page(kids[0].textContent, kids);
  }

  const run = { ask, change, changes }[kind];
  // The first load tries once more after a second before it says so: a read that stalls once usually answers the next time.
  const load = () => readNames().then(() => (run ? run() : fail("Not found")));
  load().catch(() => new Promise((r) => setTimeout(r, 1000)).then(load)).catch(() => fail("Atlas could not read this just now", "Reload to try again."));
}
