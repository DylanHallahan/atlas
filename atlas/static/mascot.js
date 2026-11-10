// The greeting's mascot (spec 2026-10-31-mascot-directions, "The build"): Anthropic's Clawd on a 21x14 canvas, whole
// pixels only. One of five states, chosen by home.js: needs (a flag on the raised arm), sleepy, working (Lookout),
// morning (a mug) and rest. Under reduced motion or a hidden tab nothing runs. Text and canvas only.
"use strict";

const Mascot = (() => {
  const C = { b: "#d77757", e: "#141414", P: "#5f6b7a", R: "#94a0b0", w: "#be8e5c", f: "#ef4444" };
  const BASE = { y: 0, e: 0, ed: 0, lk: 0, l: 0, a: 0, pl: 0, fl: 0, mu: 0, md: 0, st: 0, z: 0 };
  // Anthropic's pose 0 has no right arm at rest; its raised arm is a staircase (x, y, w, h from the figure's corner),
  // clipped here at one column past the 17-wide figure. The left arm lies at rows 4 and 5 (3: rows 5 and 6).
  const ARM = [4, 2, 3, 5], A5 = [[15, 3, 2, 1], [15, 2, 3, 1], [16, 1, 2, 1], [17, 0, 1, 1]];
  const REST = {
    needs: { a: 5, pl: 4 }, sleepy: { e: 2, z: 1 }, working: {}, morning: { mu: 1 }, rest: {},
  };
  const BLINK = [[{ e: 1 }, 70], [{ e: 2 }, 110], [{ e: 1 }, 70]];
  const RIPPLE = [[{ fl: 1 }, 160], [{ fl: 2 }, 160], [{ fl: 1 }, 160], [{ fl: 2 }, 160], [{ fl: 1 }, 200]];
  const RISE = [[{ a: 5, pl: 0 }, 180], [{ a: 5, pl: 1 }, 200], [{ a: 5, pl: 2 }, 220], [{ a: 5, pl: 3, fl: 1 }, 240], [{ a: 5, pl: 4, fl: 2 }, 240]];
  const LOWER = [[{ a: 5, pl: 3, fl: 1 }, 150], [{ a: 5, pl: 2 }, 150], [{ a: 5, pl: 1 }, 150], [{ a: 5 }, 150]];
  const jit = (n) => Math.round(n * (0.88 + Math.random() * 0.24)), rnd = (a, b) => a + Math.random() * (b - a);
  const pick = (a, b) => (Math.random() < 0.5 ? a : b);

  // The next move in a state and the pause after it (ms): at rest one blink or sip every 3 to 6 s.
  const clip = (s) => {
    if (s === "needs") return { f: RIPPLE, gap: [3500, 5600] };
    if (s === "sleepy") return { f: [[{ z: 2 }, 650], [{ z: 3 }, 650], [{ z: 2 }, 650]], gap: [2400, 4200] };
    if (s === "working") {
      const g = pick(-1, 1);
      return Math.random() < 0.7 ? { f: [[{ lk: g }, 900], [{ lk: 0 }, 0]], gap: [2600, 4800] }
        : { f: [[{ lk: -1 }, 700], [{ lk: 1, y: 1 }, 700], [{ lk: 0 }, 0]], gap: [3500, 6000] };
    }
    if (s === "morning" && Math.random() < 0.6) {
      return { f: [[{ st: 1 }, 500], [{ st: 2 }, 500], [{ md: -1, e: 2 }, 1000], [{ md: -1, e: 1, st: 3 }, 300]], gap: [3200, 5600] };
    }
    return { f: BLINK, gap: [3000, 6000] };
  };

  function draw(cv, p) {
    const c = cv.getContext("2d"), x = 2, y = 4 + p.y;
    const F = (a, b, w, h, k) => { c.fillStyle = C[k || "b"]; c.fillRect(x + a, y + b, w, h); };
    c.clearRect(0, 0, 21, 14);
    F(2, 0, 13, 8);
    F(0, ARM[p.l], 2, 2);
    if (p.a) for (const [a, b, w, h] of A5) F(a, b, w, h);
    for (const n of [2, 4, 12, 14]) F(n, 8, 1, p.y > 0 ? 2 - p.y : 2);
    if (p.e < 2) for (const n of [5, 13]) F(n + p.lk, (p.e ? 3 : 2) + p.ed, 1, p.e ? 1 : 2, "e");
    if (p.pl) { // the flag: a pole in the raised hand, the cloth on its top and to its left
      F(17, -p.pl, 1, p.pl, "w");
      if (p.pl > 2) {
        F(15, -p.pl, 2, p.fl === 1 ? 1 : 2, "f");
        if (p.fl) F(p.fl === 1 ? 15 : 16, 1 - p.pl, 1, 1, "f");
      }
    }
    if (p.mu) { // the mug: a 3 by 3 body, a ring handle at the left, steam rising from it
      const d = p.md;
      F(0, 1 + d, 3, 3, "P");
      F(-1, 1 + d, 1, 1, "P"); F(-2, 1 + d, 1, 3, "P"); F(-1, 3 + d, 1, 1, "P");
      if (p.st) F(p.st === 2 ? 0 : 1, d + (p.st === 1 ? 0 : p.st === 2 ? -1 : -2), 1, 1, "R");
    }
    if (p.z) { // the "z", in the page's third ink
      c.fillStyle = getComputedStyle(cv).color;
      const zx = x + 14 + p.z, zy = y - p.z * 2;
      c.fillRect(zx, zy, 2, 1); c.fillRect(zx + 1, zy + 1, 1, 1); c.fillRect(zx, zy + 2, 2, 1);
    }
  }

  // One mascot on one canvas. update(state, ids): the state home.js chose and the ids of what newly needs Dylan.
  // Nothing happens when neither changed, so a poll never restarts a move; a new id raises the flag once.
  function make(cv) {
    cv.width = 21;
    cv.height = 14;
    const rm = matchMedia("(prefers-reduced-motion: reduce)"), seen = new Set();
    let st = null, t = 0, gen = 0, on = !document.hidden;
    const rest = () => ({ ...BASE, ...REST[st] }), show = (o) => draw(cv, { ...rest(), ...o });
    const live = () => on && !rm.matches;
    const stop = () => { gen++; clearTimeout(t); };
    const play = (f, done) => {
      const g = gen;
      let i = 0;
      (function step() {
        if (g !== gen) return;
        if (i >= f.length) { show({}); if (done) done(); return; }
        const [o, ms] = f[i++];
        show(o);
        t = setTimeout(step, jit(ms));
      })();
    };
    const next = () => {
      if (!live()) return;
      const c = clip(st);
      play(c.f, () => { t = setTimeout(next, rnd(...c.gap)); });
    };
    const settle = () => { show({}); if (live()) t = setTimeout(next, rnd(3000, 5000)); };
    const m = {
      update(state, ids) {
        const arrival = ids.some((i) => !seen.has(i));
        ids.forEach((i) => seen.add(i));
        if (state === st && !arrival) return;
        const was = st;
        st = state;
        stop();
        if (!on) return;
        if (rm.matches) return show({});
        if (arrival && was === "needs") play(RIPPLE, settle);
        else if (arrival) play(RISE, settle);
        else if (was === "needs") play(LOWER, settle);
        else settle();
      },
    };
    const sync = () => { on = !document.hidden; stop(); if (on && st) { show({}); if (live()) t = setTimeout(next, rnd(3000, 5000)); } };
    document.addEventListener("visibilitychange", sync);
    rm.addEventListener("change", sync);
    return m;
  }
  return { make };
})();
