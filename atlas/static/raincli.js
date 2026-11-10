// Atlas: a RainCLI thread at /raincli/<conversation id> (spec 2026-11-02-raincli-org-chart), read only. The page asks
// /api/raincli-thread, which reads RainCLI live; the text is shown and kept nowhere. There is no control on this page that
// sends, replies, acknowledges, approves or archives. Every value reaches the page as text, never as markup.
"use strict";

{
  const root = document.getElementById("root");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "2-digit", day: "2-digit", hour: "numeric", minute: "2-digit" });
  const at = (iso) => {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "time not known";
    const p = Object.fromEntries(ET.formatToParts(d).map((x) => [x.type, x.value]));
    return `${p.weekday} ${p.month}-${p.day} ${p.hour}:${p.minute} ${p.dayPeriod} ET`;
  };
  const STATE = { received: "received", submitted: "submitted", replied: "replied", rejected: "rejected", stored: "stored",
                  submission_uncertain: "may not have arrived" };
  const why = holdWord; // app.js: RainCLI's own words for a hold code
  const size = (n) => (typeof n !== "number" ? "" : n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
  const say = (msg) => root.replaceChildren(back(), el("p", "quiet", msg));
  const back = () => { const a = el("a", "back", "Org chart"); a.href = "/office"; return a; };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function chip(m) {
    if (m.state === "held") return el("span", "chip held", `held: ${why(m.reason)}`);
    return Object.hasOwn(STATE, m.state) ? el("span", "chip", STATE[m.state]) : null;
  }

  function show(d) {
    const todayKey = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" });
    const today = todayKey.format(new Date());
    const n = d.messages.length, t = d.messages.filter((m) => m.at && todayKey.format(new Date(m.at)) === today).length;
    const head = el("div"), meta = el("p", "meta"), dot = el("span", `dotw is-${Object.hasOwn(PRESENCE, d.presence) ? d.presence : "unknown"}`);
    dot.append(el("i"), presence(d.presence));
    meta.append(el("span", null, "outside, RainCLI"), dot, el("span", null, `${plural(n, "message", "messages")}, ${t} today`));
    head.append(el("h1", null, shownName(d.name)), meta, el("p", "ro", "Read only: nothing here sends, replies or acknowledges."));
    const ours = Object.values(d.held || {}).reduce((a, b) => a + b, 0);
    if (ours) head.append(el("p", "line", `${ours} of ours held: ${Object.keys(d.held).map(why).join(", ")}`));
    const list = el("ul", "msgs");
    for (const m of d.messages) {
      const li = el("li", m.ours ? "msg us" : "msg"), h = el("div", "mh");
      h.append(el("b", null, shownName(m.who)), el("span", null, at(m.at)));
      if (m.reply) h.append(el("span", null, "a reply"));
      const c = chip(m);
      if (c) h.append(c);
      li.append(h);
      if (m.text) li.append(el("p", "mt", m.text));
      if (m.files.length) li.append(el("p", "files", `Attached: ${m.files.map((f) => `${f.name} (${size(f.size)})`).join(", ")}`));
      list.append(li);
    }
    const i = d.inbox, f = el("p", "foot-line");
    if (i) {
      const k = Object.values(i.held || {}).reduce((a, b) => a + b, 0);
      f.textContent = `Inbox connection: ${i.mode || "mode not known"} mode, messages go to ${i.herdr_agent || "no agent"}, escalations go to ${i.escalation_target || "no one"}, ${k} held${k ? `: ${Object.keys(i.held).map((c) => ownHoldWord(c, i.herdr_agent)).join(", ")}` : ""}.`;
      if (k) f.classList.add("held");
    }
    root.replaceChildren(back(), head, list, ...(i ? [f] : []));
    document.title = `${shownName(d.name)}, RainCLI thread`;
  }

  async function load() {
    const id = decodeURIComponent(location.pathname.split("/")[2] || "");
    try {
      const res = await fetch(`/api/raincli-thread?id=${encodeURIComponent(id)}`, { cache: "no-store", signal: AbortSignal.timeout(150000) });
      if (res.status === 404) return say("This conversation is not on the chart.");
      if (!res.ok) throw new Error(String(res.status));
      const d = await res.json();
      if (d.timed_out) return say("RainCLI took too long: the thread could not be read in time");
      if (d.unreachable) return say("RainCLI not reachable: the thread cannot be read right now");
      show(d);
    } catch (e) {
      say("RainCLI not reachable: the thread cannot be read right now");
    }
  }
  load();
}
