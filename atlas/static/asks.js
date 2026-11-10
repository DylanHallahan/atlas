// Ask rows in Needs you's order. Home and the ask page share this one ordering function.
"use strict";
const ASKS = (() => {
  // Dot segments would be resolved away by the browser, so they never get a link.
  const href = (team, name) => {
    const parts = [team, ...name.split("/")];
    return parts.some((p) => p === "." || p === "..") ? null : `/ask/${parts.map(encodeURIComponent).join("/")}`;
  };
  const order = (brief) => (brief.teams || []).flatMap((t) => (t.asks || []).map((ask) => ({
    kind: "ask", team: t.key, name: ask.name, ask, href: href(t.key, ask.name), id: `${t.key}/${ask.name}`,
  })));
  // What waits on Dylan as an ask: every linked ask whose answer has not been sent. Home's Needs you and Stand-up share it.
  const open = (brief) => order(brief).filter((k) => !k.ask.answer);
  return { href, order, open };
})();
