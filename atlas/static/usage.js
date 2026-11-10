// Atlas: /usage. Each Claude account's limit windows from /api/usage (atlas/usage.py): percent used, when it resets and
// when it was read. Every value reaches the page as text; nothing here uses innerHTML. The server caches each read for
// 5 minutes, so polling every 30 s asks Anthropic at most once per account per cache window.
"use strict";

if (document.getElementById("usage")) {
  const { el } = MD;
  const POLL_MS = 30000, NEAR = 80, FULL = 100;
  const T = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  const WD = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short" });
  const D8 = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
  // "1:00 PM ET", with the weekday in front when it is not today in ET.
  const et = (iso) => {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "";
    return `${D8.format(d) === D8.format(new Date()) ? "" : WD.format(d) + " "}${T.format(d)} ET`;
  };
  const pct = (n) => `${Math.round(n)}%`;

  // "4 hours into its week": a fact worked out from the weekly window's reset, never a forecast.
  function intoWeek(w) {
    const h = Math.floor((Date.now() - (Date.parse(w.resets_at) - 7 * 864e5)) / 36e5);
    if (!(h >= 0)) return "";
    const n = h < 24 ? h : Math.floor(h / 24), unit = h < 24 ? "hour" : "day";
    return h < 1 ? `${pct(w.percent)} used, under an hour into its week` : `${pct(w.percent)} used, ${n} ${unit}${n === 1 ? "" : "s"} into its week`;
  }

  function windowRow(w, old) {
    const hot = w.percent >= NEAR, row = el("div", `win${hot ? " hot" : ""}${old ? " old" : ""}`);
    const top = el("div", "win-top"), track = el("div", "bar-track"), fill = el("div", "bar-fill");
    top.append(el("span", "win-name", w.label), el("span", "win-pct", pct(w.percent)));
    fill.style.width = `${Math.min(100, Math.max(0, w.percent))}%`;
    track.append(fill);
    const note = el("div", "win-note");
    // A window already past its reset is not current: the percent shown was true before it.
    const over = Date.parse(w.resets_at) <= Date.now();
    if (!old && !over && w.percent >= FULL) note.append(el("span", "win-flag", `at the limit until ${et(w.resets_at)}`));
    else {
      if (!old && !over && hot) note.append(el("span", "win-flag", "near the limit"), " · ");
      note.append(over ? `reset ${et(w.resets_at)}` : `resets ${et(w.resets_at)}`);
    }
    row.append(top, track, note);
    if (w.key === "seven_day" && !old && !over && w.percent < FULL && intoWeek(w)) row.append(el("div", "win-note", intoWeek(w)));
    return row;
  }

  function card(a) {
    const c = el("section", "acct");
    c.append(el("h2", null, `${a.name} Claude`));
    if (a.state === "ok") {
      for (const w of a.windows) c.append(windowRow(w, false));
      c.append(el("p", "read", `read ${et(a.read_at)}`));
      // A window the server found past its reset: greyed and dated, never current.
      if (a.last) {
        for (const w of a.last.windows) c.append(windowRow(w, true));
        c.append(el("p", "read", `last known, read ${et(a.last.read_at)}`));
      }
    } else {
      c.append(el("p", "unk", `unknown: ${a.reason}`));
      // The last figures Atlas read, greyed and dated, never as current.
      if (a.last) {
        for (const w of a.last.windows) c.append(windowRow(w, true));
        c.append(el("p", "read", `last known, read ${et(a.last.read_at)}`));
      }
      c.append(el("p", "read", `checked ${et(a.read_at)}`));
    }
    return c;
  }

  // "Live agents by account and model": one row per account and model, most agents first, then the workers' counts.
  function agentsSection(a) {
    const sec = el("section", "sect");
    sec.append(el("h2", "sect-head", "Live agents by account and model"));
    for (const r of a.rows) {
      const row = el("div", "arow"), names = el("span", "anames");
      names.append(...r.names.flatMap((n, i) => [...(i ? [" "] : []), Object.assign(el("a", null, n), { href: `/agent/${encodeURIComponent(n)}` })]));
      row.append(el("span", "alabel", r.label), el("span", "acount", String(r.names.length)), names);
      sec.append(row);
    }
    for (const n of a.empty) sec.append(el("p", "quiet", `${n}: no live agents`));
    if (a.workers) {
      const w = Object.entries(a.workers).sort((x, y) => y[1] - x[1]).map(([m, n]) => `${n} on ${m}`);
      if (!Object.hasOwn(a.workers, "Opus 5.5")) w.push("none on Opus 5.5");
      sec.append(el("p", "quiet", `Workers: ${w.join(", ")}.`));
    }
    return sec;
  }

  // "Worktrees": linked worktrees per repo; the untouched count is quiet clay when above 0.
  const INVENTORY = "https://your-host.your-tailnet.ts.net/change/devops/2026-10-31-repo-inventory";
  function worktreesSection(w) {
    const rows = w.rows;
    const sec = el("section", "sect");
    sec.append(el("h2", "sect-head", "Worktrees"));
    for (const r of rows) {
      const row = el("div", "wrow"), line = el("span", "wline");
      line.append(el("span", r.untouched ? "stale" : null, `${r.untouched} untouched for 2 days or more`),
        ` · oldest ${r.oldest_days == null ? "unknown" : r.oldest_days < 1 ? "under a day" : `${Math.floor(r.oldest_days)} days`}`);
      row.append(el("span", "alabel", r.repo), el("span", "acount", String(r.count)), line);
      sec.append(row);
    }
    if (!rows.length) sec.append(el("p", "quiet", "No linked worktrees."));
    if (w.failed.length) sec.append(el("p", "quiet", `Not read just now: ${w.failed.join(", ")}`));
    const inv = el("p", "quiet", "DevOps's inventory: ");
    inv.append(Object.assign(el("a", null, "repo inventory"), { href: INVENTORY }));
    sec.append(inv);
    return sec;
  }

  const live = document.getElementById("live");
  let last = null;
  async function draw() {
    try {
      const d = await POLL.get("/api/usage", 15000);
      last = d.as_of;
      document.getElementById("cards").replaceChildren(...d.accounts.map(card));
      document.getElementById("fleet").replaceChildren(agentsSection(d.agents), worktreesSection(d.worktrees));
      const wins = d.accounts.flatMap((a) => (a.state === "ok" ? a.windows.map((w) => ({ ...w, account: a.name })) : []));
      const full = wins.filter((w) => w.percent >= FULL), near = wins.filter((w) => w.percent >= NEAR && w.percent < FULL);
      const kind = (w) => w.label.replace(/^Weekly/, "weekly");
      const kinds = new Set(full.map(kind)), who = [...new Set(full.map((w) => w.account))], many = who.length > 1;
      // What holds, the limit first.
      document.getElementById("sum").textContent = full.length
        ? (kinds.size === 1 ? `${who.join(" and ")} ${many ? "are at their" : "is at its"} ${[...kinds][0]} limit.`
          : `${full.map((w) => `${w.account} is at its ${kind(w)} limit`).join("; ")}.`)
        : near.length ? (near.length === 1 ? "A window is near its limit." : `${near.length} windows are near their limit.`)
        : "Percent of each limit window used, as Anthropic reports it.";
      live.className = "live";
      live.replaceChildren(el("i"), `Live, ${et(last)}`);
    } catch {
      live.className = "live off";
      live.replaceChildren(el("i"), last ? `Offline, last ${et(last)}` : "Offline");
    }
  }
  draw();
  setInterval(draw, POLL_MS);
}
