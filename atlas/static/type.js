// Atlas: typing into an agent's terminal, and replying to an ask, for Dylan only (spec 2026-10-31-typing). Nothing
// here shows unless /api/whoami says typist; the server checks every send anyway (POST /api/send). Every value
// reaches the page as text; nothing here opens a dialog.
"use strict";

const SEND = (() => {
  const HEADER = "X-Atlas-Send-Token";
  let me = null; // the /api/whoami answer, kept once it comes; a failed or timed-out read is {} and is not kept
  const who = () => me || (me = fetch("/api/whoami", { cache: "no-store", signal: AbortSignal.timeout(6000) })
    .then((r) => (r.ok ? r.json().catch(() => ({})) : {}), () => ({})).then((w) => { if (!("typist" in w)) me = null; return w; }));
  const typist = () => who().then((w) => Boolean(w.typist && w.send_token));
  // Why a send failed, in his words.
  const REASONS = {
    "not-typist": "Not sent: this browser is not signed in as you on one of your devices.",
    "not-serve": "Not sent: this request did not come through the tailnet.",
    "cross-site": "Not sent: the request did not come from this page.",
    gone: "Not sent: the agent is not running.",
    blocked: "Not sent: the agent is waiting on a question in its terminal. Answer it with the keys.",
    "bad-body": "Not sent: Atlas could not read what was sent.",
    "too-long": "Not sent: that is too long.",
  };
  const RESTART = "Not sent: Atlas restarted. Reload the page to send.";
  // One POST with the send token. Resolves to {ok} when sent, {why} (the sentence that says why not), {already} (a 409:
  // the key was taken before) or {lost} when no answer came. When Atlas has restarted the token is refused: the
  // whoami answer is read again and the same body goes once more, never twice.
  async function attempt(path, body, over, retried) {
    const w = await who();
    try {
      const res = await fetch(path, {
        method: "POST", cache: "no-store", signal: AbortSignal.timeout(8000),
        headers: { "Content-Type": "application/json", [HEADER]: w.send_token || "" }, body: JSON.stringify(body),
      });
      if (res.status === 204) return { ok: true };
      if (res.status === 502) return { why: "Not sent: herdr did not take it. Try again." };
      const j = await res.json().catch(() => null), reason = j && j.reason;
      if (res.status === 409 && reason === "already-sent") return { already: j };
      if (res.status === 403 && reason === "token") {
        if (retried) return { why: RESTART };
        me = null;
        const fresh = await who();
        return fresh.send_token ? attempt(path, body, over, true) : { why: RESTART };
      }
      return { why: (over && over[reason]) || REASONS[reason] || `Not sent: Atlas answered ${res.status}${reason ? ` (${reason})` : ""}.` };
    } catch {
      return { lost: true };
    }
  }
  // POSTs a body; resolves to null when sent, else the sentence that says why not.
  async function post(path, body, over) {
    const r = await attempt(path, body, over);
    return r.ok ? null : r.why || "Not sent: Atlas did not answer. Try again.";
  }
  // Sends one line or one key. over: sentences for some reasons in place of the usual ones.
  const send = (body, over) => post("/api/send", body, over);

  // A send the page can lose the reply to, and then ask about: one random key per press, kept for the retries of the
  // same body only. keyed() makes the holder for one box.
  const keyed = () => ({ held: null });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // The server's word on a key: {outcome, at} (outcome sent, sending, failed or not-found), or null when it could not be read.
  async function status(key, retried) {
    const w = await who();
    try {
      const res = await fetch(`/api/send-status?key=${encodeURIComponent(key)}`, {
        cache: "no-store", signal: AbortSignal.timeout(5000), headers: { [HEADER]: w.send_token || "" },
      });
      if (res.ok) return await res.json();
      if (res.status === 403 && !retried && (await res.json().catch(() => null))?.reason === "token") { me = null; return status(key, true); }
    } catch { /* unknown */ }
    return null;
  }
  const CHECKING = "Atlas did not answer. Checking whether it was sent.";
  const UNSURE = "Not confirmed: Atlas did not answer. Press Send to try again; it cannot go twice.";
  const CHECKS = [1000, 3000, 6000, 10000]; // waits before each look: about 20 s in all
  // Sends a body under the holder's key. checking(sentence) is called while the lost reply is being checked. Resolves to
  //   {sent: true, at, again}  it went (at: the server's time of the row, null when its reply came; again: the key was taken before),
  //   {why}                    it did not go, in his words,
  //   {why, unsure: true}      it may have gone; the next press carries the same key.
  async function sendKeyed(holder, body, over, checking) {
    const text = JSON.stringify(body);
    if (!holder.held || holder.held.text !== text) holder.held = { text, key: crypto.randomUUID() };
    const key = holder.held.key;
    const r = await attempt("/api/send", { ...body, client_key: key }, over);
    let out;
    if (r.ok) out = { sent: true, at: null };
    else if (r.already) out = r.already.outcome === "sent" ? { sent: true, at: r.already.at, again: true } : r.already.outcome === "failed" ? { why: "Not sent: herdr did not take it. Try again." } : null;
    else if (r.lost) out = null;
    else out = { why: r.why };
    if (out) { holder.held = null; return out; }
    // No answer to learn from: ask for the key's status a few times.
    checking(CHECKING);
    for (const wait of CHECKS) {
      await sleep(wait);
      const s = await status(key);
      if (!s) continue;
      if (s.outcome === "sent") { holder.held = null; return { sent: true, at: s.at }; }
      if (s.outcome === "failed") { holder.held = null; return { why: "Not sent: herdr did not take it. Try again." }; }
    }
    return { why: UNSURE, unsure: true };
  }
  // Stores one screenshot for an ask's answer: {path} when stored, else {why}, in his words. Same token and the same
  // one retry after a restart as post.
  const WHY = { "bad-image": "not a complete PNG or JPEG", "too-many": "up to 5 screenshots", "bad-body": "Atlas could not read it", store: "Atlas could not store it" };
  async function upload(ask, file, retried) {
    const w = await who();
    try {
      const res = await fetch("/api/upload", {
        method: "POST", cache: "no-store", signal: AbortSignal.timeout(60000),
        headers: { "Content-Type": "application/octet-stream", [HEADER]: w.send_token || "", "X-Atlas-Ask": ask }, body: file,
      });
      if (res.ok) return { path: (await res.json()).path };
      const reason = await res.json().then((j) => j && j.reason, () => null);
      if (res.status === 403 && reason === "token") {
        if (retried) return { why: "Atlas restarted; reload the page" };
        me = null;
        const fresh = await who();
        return fresh.send_token ? upload(ask, file, true) : { why: "Atlas restarted; reload the page" };
      }
      return { why: WHY[reason] || (REASONS[reason] || `Atlas answered ${res.status}`).replace("Not sent: ", "") };
    } catch {
      return { why: "Atlas did not answer" };
    }
  }
  return { who, typist, send, post, upload, keyed, sendKeyed };
})();

// ---- The message box on an agent's page (term.js calls it once per view) ----
// For the typist only: a box under the screen or History, open from the start. It names who he is writing to. Enter sends;
// Shift+Enter adds a line break, and a pasted block keeps its breaks and goes as one message, up to 100,000 bytes. Keys
// holds the row of keys (Enter, Esc, the arrows, y, n, 1 to 3 ...) and, on a wide screen, Type here, which sends each
// key straight to the agent while a clay outline is on the screen. The row opens by itself while the agent is blocked on
// a question, and closes when the box is used. Ctrl+C asks for a second tap within 3 s.
const LINE_BYTES = 100000;
function typeBox({ root, status, screen, el, button, agent, about, showScreen }) {
  // The listed keys go as keys; y, n, 1, 2 and 3 as single characters, which answer an agent's question.
  const KEYS = ["Enter", "Esc", "Tab", "Shift+Tab", "Ctrl+C", "Up", "Down", "y", "n", "1", "2", "3"], CHARS = ["y", "n", "1", "2", "3"];
  const keysBtn = button("Keys"), here = button("Type here", "tv-btn tv-wide");
  keysBtn.setAttribute("aria-expanded", "false");

  const area = el("div", "tv-type"), keys = el("div", "tv-keys"), box = el("div", "tv-box"), to = el("div", "tv-to"), row = el("div", "tv-row"), msg = el("p", "tv-msg");
  const field = Object.assign(el("textarea"), { rows: 1, autocomplete: "off", spellcheck: false });
  field.setAttribute("aria-label", "Your message; Enter sends it, Shift+Enter adds a line break");
  const send = Object.assign(el("button", "tv-send"), { type: "button" });
  msg.setAttribute("role", "status");
  row.append(send, keysBtn, msg);
  box.append(to, field, row);
  keys.hidden = true;
  area.append(keys, box);

  const held = SEND.keyed(); // the key of a message whose reply was lost, for the same words again
  let armed = 0, queue = Promise.resolve(), waiting = 0, pending = false, auto = false, wasBlocked = false; // armed: when Ctrl+C was first tapped
  const editable = (t) => Boolean(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)));
  const say = (t, ok) => { msg.textContent = t || ""; msg.className = ok ? "tv-msg is-ok" : "tv-msg"; };
  const T = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  // Who the box writes to, from the page's own state: the name, role and team, and the Send button's name.
  function names() {
    const a = about(), n = agent() || "";
    to.textContent = `Message ${n}${a.role || a.team ? `, ${[a.role, a.team && `in ${a.team}`].filter(Boolean).join(" ")}` : ""}`;
    send.textContent = `Send to ${n}`;
    return a;
  }
  // Is the agent working now? The page's own state is read every 5 s and herdr's every 2 s, so it can be 7 s behind a
  // turn that has just begun; read it again as the message goes, and fall back to the page's last state.
  const working = (name) => fetch("/api/state", { cache: "no-store", signal: AbortSignal.timeout(2000) })
    .then((r) => r.json()).then((s) => { const a = s.agents.find((x) => x.name === name); return Boolean(a && a.gone === false && a.state === "working"); })
    .catch(() => about().working);
  // Sends of keys go one at a time, in the order pressed, so a quick run of keys is never dropped or reordered.
  function go(body) {
    const name = agent();
    if (!name) return;
    waiting++;
    area.classList.add("is-busy");
    queue = queue.then(async () => {
      const why = await SEND.send({ agent: name, ...body });
      if (--waiting === 0) area.classList.remove("is-busy");
      say(why || "", !why);
    });
  }
  const keyBtns = KEYS.map((k) => {
    const b = button(k, "tv-btn tv-key");
    b.dataset.key = k;
    b.addEventListener("click", () => press(k));
    keys.append(b);
    return b;
  });
  keys.append(here);
  const ctrlC = keyBtns[KEYS.indexOf("Ctrl+C")];
  function press(k) {
    if (k === "Ctrl+C" && Date.now() - armed > 3000) {
      armed = Date.now();
      ctrlC.textContent = "Tap again to interrupt";
      setTimeout(() => { if (Date.now() - armed >= 3000) ctrlC.textContent = "Ctrl+C"; }, 3000);
      return;
    }
    if (k === "Ctrl+C") { armed = 0; ctrlC.textContent = "Ctrl+C"; }
    go({ kind: CHARS.includes(k) ? "char" : "key", key: k });
  }

  // The message: sent as one line of kind line, which may hold line breaks. One at a time: a second submit waits for the
  // first to finish. The box clears only if it still holds what was sent, so words typed meanwhile stay; a refusal
  // keeps the text.
  async function sendMessage() {
    const raw = field.value, t = raw.trim(), name = agent();
    if (!t || pending || !name) return;
    if (new TextEncoder().encode(t).length > LINE_BYTES) return say("Too long to send: up to 100,000 bytes, about 100,000 plain characters.");
    pending = true;
    send.disabled = true;
    names();
    const busy = await working(name);
    const r = await SEND.sendKeyed(held, { agent: name, kind: "line", text: t }, {
      blocked: `Not sent: ${name} is waiting on a question in its terminal. Answer it with Keys first.`,
      "too-long": "Too long to send: up to 100,000 bytes, about 100,000 plain characters.",
    }, (words) => say(words, true));
    pending = false;
    send.disabled = false;
    if (!r.sent) return say(r.why);
    if (field.value === raw) field.value = "";
    const when = T.format(r.at ? new Date(r.at) : new Date());
    say(r.again ? `Already sent at ${when}, so it was not sent again.` : `Sent ${when}${busy ? `. ${name} is working and will see it at its next step.` : ""}`, true);
  }
  send.addEventListener("click", sendMessage);
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!e.repeat) sendMessage(); } // a held Enter sends once
  });
  // Using the box closes a row that opened by itself.
  field.addEventListener("input", () => { if (auto) setKeys(false); });

  // Keys: the row of keys, on and off.
  function setKeys(on) {
    auto = false;
    keys.hidden = !on;
    keysBtn.classList.toggle("is-on", on);
    keysBtn.setAttribute("aria-expanded", String(on));
    if (!on) setHere(false);
  }
  keysBtn.addEventListener("click", () => setKeys(keys.hidden));

  // Type here: keys straight to the agent while the clay outline is on.
  const CAPTURE = { Enter: "Enter", Escape: "Esc", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", Backspace: "Backspace" };
  function setHere(on) {
    root.classList.toggle("is-capturing", on);
    here.classList.toggle("is-on", on);
    here.textContent = on ? "Stop typing" : "Type here";
    // Keys go to the agent only while the Screen shows, with its clay outline: starting shows it.
    if (on) { showScreen(); setKeys(true); screen.focus({ preventScroll: true }); }
  }
  here.addEventListener("click", () => setHere(!root.classList.contains("is-capturing")));
  document.addEventListener("pointerdown", (e) => {
    if (root.classList.contains("is-capturing") && !screen.contains(e.target) && !here.contains(e.target) && !area.contains(e.target)) setHere(false);
  }, true);
  // Focus moving anywhere but the screen, its button or the input area ends it, as a click outside does.
  document.addEventListener("focusin", (e) => {
    if (root.classList.contains("is-capturing") && !screen.contains(e.target) && !here.contains(e.target) && !area.contains(e.target)) setHere(false);
  });
  document.addEventListener("keydown", (e) => {
    if (!root.classList.contains("is-capturing") || !agent() || editable(e.target)) return; // a field keeps its own keys
    // A key that is composing (an input method's letters, or its own Enter and Esc) is the method's, not the agent's.
    if (e.isComposing || e.keyCode === 229) return;
    // Ctrl+C only as itself; any other chord with Ctrl, Alt or Meta is left alone, before any key is named.
    const ctrlC = e.key.toLowerCase() === "c" && e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey;
    if (!ctrlC && (e.ctrlKey || e.altKey || e.metaKey)) return;
    let key = ctrlC ? "Ctrl+C" : CAPTURE[e.key];
    if (e.key === "Tab") key = e.shiftKey ? "Shift+Tab" : "Tab";
    // A held Enter or Ctrl+C acts once: its repeats never send, arm or confirm.
    if (e.repeat && (key === "Ctrl+C" || e.key === "Enter")) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    if (key) { e.preventDefault(); e.stopImmediatePropagation(); return press(key); }
    if ([...e.key].length !== 1) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    // One character goes only as plain ASCII, space to tilde; any other is said in place, and the box takes it.
    if (/^[\x20-\x7e]$/.test(e.key)) go({ kind: "char", key: e.key });
    else say(`Not sent: Type here sends plain letters, digits and signs. Type "${e.key}" in the box instead.`);
  }, true);

  // The box joins the view only for a typist; for anyone else it never exists on the page. Atlas not answering is asked
  // again every 5 s, so the box appears once it is back, with no reload. allow(false) takes it out again, for the one
  // agent that never shows a terminal.
  let typist = false, blocked = false;
  const put = () => { if (typist && !blocked && !area.isConnected) { names(); root.insertBefore(area, status); } };
  const offer = () => SEND.who().then((w) => {
    if (!("typist" in w)) return setTimeout(offer, 5000);
    typist = Boolean(w.typist && w.send_token);
    put();
  });
  offer();
  return {
    // Another agent opened in this view: the keys close, the box and its message clear.
    reset() { setKeys(false); field.value = ""; say(""); armed = 0; ctrlC.textContent = "Ctrl+C"; wasBlocked = false; names(); },
    // Another view than the Screen took the page: Type here ends.
    leave() { setHere(false); },
    allow(on) { blocked = !on; if (blocked) { setKeys(false); area.remove(); } else put(); },
    // The page's state moved: the names, and the keys row opens when the agent becomes blocked on a question.
    update(isBlocked) {
      names();
      if (isBlocked && !wasBlocked && keys.hidden) { setKeys(true); auto = true; }
      wasBlocked = isBlocked;
    },
  };
}

// Enter in a box presses that form's own Send, only when it is enabled: the same click, so the line, audit and state are
// Send's. Shift+Enter is a line break; Ctrl, Alt and Cmd with Enter, and an input method's own Enter, are left alone; a held
// Enter acts once (a repeat is swallowed), and a disabled Send stops a second send while one is going.
function enterSends(field, button) {
  field.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    if (!e.repeat && !button.disabled) button.click();
  });
}

// ---- Reply to <lead>, on an ask page (report.js calls it for a typist) ----
// A text box with a fixed prefix, sent as one line to the lead's terminal; off, and saying why, when the lead is not
// running or is waiting on a question. After a send: when, a link to watch the terminal, and his text.
function replyBox({ el, lead, question, state, bare }) {
  const box = el("div", bare ? "reply" : "card reply"), prefix = `Dylan, on the ask "${String(question || "").replace(/\s+/g, " ").trim()}":`;
  const a = state && state.agents ? state.agents.find((x) => x.name === lead && x.gone === false) : null;
  const off = !a ? `${lead} is not running.` : a.state === "blocked" ? `${lead} is waiting on a question in its terminal. Answer it there.` : null;
  const pre = el("p", "reply-prefix", prefix), field = Object.assign(el("textarea"), { rows: 3, disabled: Boolean(off) });
  field.setAttribute("aria-label", `Your reply to ${lead}, after the prefix; Enter sends, Shift+Enter adds a line break`);
  const send = Object.assign(el("button", "btn", "Send"), { type: "button", disabled: Boolean(off) });
  const msg = el("p", off ? "reply-off" : "reply-msg", off || ""), watch = el("a", "btn ghost", `Watch ${lead}'s terminal`);
  msg.setAttribute("role", "status");
  watch.href = `/agent/${encodeURIComponent(lead)}`;
  box.append(pre, field, send, msg, watch);
  const T = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  enterSends(field, send);
  const held = SEND.keyed();
  send.addEventListener("click", async () => {
    const raw = field.value, words = raw.replace(/\s*\n\s*/g, " ").trim();
    if (!words) { msg.className = "reply-msg"; msg.textContent = "Write your reply first."; return; }
    send.disabled = true;
    const r = await SEND.sendKeyed(held, { agent: lead, kind: "line", text: `${prefix} ${words}` }, undefined, (t) => { msg.className = "reply-msg is-quiet"; msg.textContent = t; });
    send.disabled = false;
    if (!r.sent) { msg.className = "reply-msg is-warn"; msg.textContent = r.why; return; }
    const sent = el("div", "reply-sent"), q = el("blockquote"), when = `${T.format(r.at ? new Date(r.at) : new Date())} ET`;
    q.append(el("span", "reply-prefix", prefix), " ", words);
    sent.append(el("p", null, r.again ? `Already sent at ${when}, so it was not sent again.` : `Sent to ${lead}'s terminal at ${when}.`), q);
    msg.className = "reply-msg";
    msg.replaceChildren(sent);
    if (field.value === raw) field.value = ""; // words typed while it was sending stay
  });
  return box;
}

// ---- Your answer, on an ask page (report.js calls it for a typist) ----
// One pick (an option's letter, or Something else in his own words), an optional note, and the exact line shown before
// it goes: the line shown is the line sent, built by one function. Sent through Reply's path as kind answer, with the
// ask id. Below it, the state the server reads: sent, recorded (from the team's decisions file) or late.
function answerBox({ el, lead, team, question, askId, opts, rec, def, state, answer, get, cards, box: options, onSent, thumbs }) {
  const box = el("div", "answer"), tail = el("div", "answer-tail");
  const a = state && state.agents ? state.agents.find((x) => x.name === lead && x.gone === false) : null;
  const off = !a ? `${lead} is not running, so nothing can be sent.` : a.state === "blocked" ? `${lead} is waiting on a question in its terminal. Answer it there.` : null;
  const T = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
  const at = (iso) => `${T.format(new Date(iso))} ET`;
  const flat = (t) => String(t || "").replace(/\s*\n\s*/g, " ").trim();
  let pick = "", own = "", note = "", sentLine = "", sentSig = "", pending = false;
  // Screenshots waiting to go with the answer, in the order added: {file, url, key, path (once stored)}.
  const shots = [];
  const sig = () => JSON.stringify([pick, flat(own), flat(note), shots.map((x) => x.key)]);
  const radios = {};
  const choose = (k) => { pick = k; radios[k].checked = true; sync(); };
  // The option cards are the radio group; Something else is its last card, with its own box inside.
  const other = el("label", "opt other"), otherT = el("div", "t", "Something else");
  otherT.append(el("small", null, "in your own words"));
  other.append(otherT);
  cards.other = other;
  options.append(other);
  for (const [k, c] of Object.entries(cards)) {
    const inp = el("input");
    Object.assign(inp, { type: "radio", name: "answer-pick", value: k, disabled: Boolean(off) });
    inp.addEventListener("change", () => choose(k));
    c.prepend(inp);
    radios[k] = inp;
  }
  const ownField = Object.assign(el("textarea"), { placeholder: "What you want instead", disabled: Boolean(off) }), noteField = Object.assign(el("textarea"), { placeholder: "Optional. You can paste or drop a screenshot.", disabled: Boolean(off) });
  ownField.setAttribute("aria-label", "Your own answer; Enter sends, Shift+Enter adds a line break");
  noteField.setAttribute("aria-label", `A note for ${lead}, optional; Enter sends, Shift+Enter adds a line break`);
  // Inside the cards the arrows move only the pick, also from a link in a card (radios do it natively).
  options.addEventListener("keydown", (e) => {
    const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key], ks = Object.keys(radios);
    if (!d || e.target.tagName !== "A" || off || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    const k = ks[(ks.indexOf(pick) + d + ks.length) % ks.length];
    choose(k);
    radios[k].focus();
  });
  other.append(ownField);
  const noteLbl = el("div", "lbl", `A note for ${lead}, if you want one`), lineLbl = el("div", "lbl", `This line goes to ${lead}'s terminal`), shown = el("div", "line");
  const go = Object.assign(el("button", null, `Send to ${lead}`), { type: "button" }), hint = el("span", null, "Enter sends. You can change your answer later; a new one replaces it.");
  const row = el("div", "go"), msg = el("p", "reply-msg"), landed = el("div", "landed");
  const held = SEND.keyed();
  const pics = el("div", "pics"), refuse = el("p", "quiet");
  refuse.setAttribute("role", "status");
  msg.setAttribute("role", "status");
  row.append(go, hint);

  // The one line: the same text is shown and sent, except that the paths of the screenshots are in what is sent
  // (full: they are all stored). A second answer carries "replacing his answer of <time>".
  const line = (full) => {
    if (sentSig && sig() === sentSig) return sentLine; // as sent, until he changes the pick or the words
    const w = pick === "other" ? `Something else: ${flat(own)}` : (() => { const o = opts.find((x) => x.key === pick); return o ? `${o.key}. ${o.title}` : ""; })();
    if (!w || (pick === "other" && !flat(own))) return "";
    const n = flat(note), q = flat(question);
    return `Dylan answers the ask "${q}" (${askId})${answer && answer.sent_at ? `, replacing his answer of ${T.format(new Date(answer.sent_at))}` : ""}: ${w}${n ? `. Note: ${n}` : ""}${shots.length ? `. Screenshots: ${full ? shots.map((x) => x.path).join(", ") : `${shots.length}, sent with your answer`}` : ""}`;
  };
  // ---- Screenshots: paste into either box, or drop on the answer ----
  const MAX_SHOTS = 5, MAX_BYTES = 5 << 20;
  const kind = async (f) => {
    const h = new Uint8Array(await f.slice(0, 8).arrayBuffer());
    return h.length >= 8 && [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((b, i) => h[i] === b) || h.length >= 3 && h[0] === 0xff && h[1] === 0xd8 && h[2] === 0xff;
  };
  const drawPics = () => {
    pics.replaceChildren(...shots.map((x, i) => {
      const f = el("figure", "pic"), img = el("img"), rm = Object.assign(el("button", "pic-rm", "Remove"), { type: "button" });
      img.src = x.url;
      img.alt = `Screenshot ${i + 1}`;
      rm.setAttribute("aria-label", `Remove screenshot ${i + 1}`);
      rm.addEventListener("click", () => {
        if (pending) return;
        URL.revokeObjectURL(x.url);
        shots.splice(shots.indexOf(x), 1);
        refuse.textContent = "";
        drawPics();
        sync();
      });
      f.append(img, rm);
      return f;
    }));
  };
  async function addShots(files) {
    for (const f of files) {
      if (off || pending) return;
      refuse.textContent = "";
      if (shots.length >= MAX_SHOTS) { refuse.textContent = "Up to 5 screenshots"; return; }
      if (!(await kind(f))) { refuse.textContent = "PNG or JPEG only"; continue; }
      if (f.size >= MAX_BYTES) { refuse.textContent = "Over 5 MB"; continue; }
      shots.push({ file: f, url: URL.createObjectURL(f), key: Math.random().toString(36).slice(2), path: "" });
    }
    drawPics();
    sync();
  }
  const pasted = (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return; // plain text pastes as usual
    e.preventDefault();
    addShots(files);
  };
  ownField.addEventListener("paste", pasted);
  noteField.addEventListener("paste", pasted);
  const dragging = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  box.addEventListener("dragover", (e) => { if (dragging(e) && !off) { e.preventDefault(); box.classList.add("is-drop"); } });
  box.addEventListener("dragleave", (e) => { if (!box.contains(e.relatedTarget)) box.classList.remove("is-drop"); });
  box.addEventListener("drop", (e) => {
    box.classList.remove("is-drop");
    if (!dragging(e)) return;
    e.preventDefault();
    if (!off) addShots([...e.dataTransfer.files]);
  });
  function sync() {
    const l = line();
    lineLbl.hidden = shown.hidden = !l;
    shown.textContent = l;
    go.disabled = Boolean(off) || pending || !l || l === sentLine;
    go.textContent = l && l === sentLine ? "Sent" : `Send to ${lead}`;
    for (const [k, c] of Object.entries(cards || {})) c.classList.toggle("chosen", k === pick);
  }
  function draw() {
    landed.hidden = !answer;
    if (!answer) return;
    const s = answer.state, t = el("div"), small = el("small");
    landed.className = `landed ${s === "recorded" ? "ok" : s === "late" ? "late" : "sent"}`;
    if (s === "recorded") {
      t.textContent = `${answer.letter ? `Answered: ${answer.letter}` : "Answered"}, recorded by ${answer.recorded_by || answer.lead || lead} at ${at(answer.recorded_at)}`;
      if (answer.recorded_by) small.append("On the work item's trail, with your words and its Effect.");
      else small.append("In the team's decisions file", ...(answer.decisions ? [", ", el("code", null, answer.decisions)] : []), ", with your words and its Effect.");
    } else if (s === "late") {
      t.textContent = `Sent at ${at(answer.sent_at)}, not yet recorded`;
      small.textContent = `${lead} has not recorded it after 30 minutes. Open its terminal to check, or send it again.`;
    } else {
      t.textContent = `Sent to ${lead} at ${at(answer.sent_at)}`;
      small.textContent = `Waiting for ${lead} to record it in ${team}'s decisions.`;
    }
    landed.replaceChildren(t, small);
    if (answer.shots && answer.shots.length) landed.append(thumbs(answer.shots));
    if (s === "late") { const w = el("a", "btn ghost", `Open ${lead}'s terminal`); w.href = `/agent/${encodeURIComponent(lead)}`; landed.append(w); }
  }
  // The box is always open: a click or typing in it picks Something else; tabbing through it does not.
  ownField.addEventListener("pointerdown", () => { if (pick !== "other") choose("other"); });
  ownField.addEventListener("input", () => { own = ownField.value; if (own.trim() && pick !== "other") choose("other"); else sync(); });
  noteField.addEventListener("input", () => { note = noteField.value; sync(); });
  enterSends(ownField, go);
  enterSends(noteField, go);
  // With an answer picked, Enter anywhere that is not a text box, a button, a link or the viewer sends it too: with focus on
  // an option's radio or on the page. The other boxes and the viewer keep their own Enter.
  document.addEventListener("keydown", (e) => {
    const t = e.target, tag = t && t.tagName;
    if (e.key !== "Enter" || e.defaultPrevented || e.repeat || e.isComposing || e.keyCode === 229 || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
    if (!pick || !t || t.isContentEditable || /^(TEXTAREA|SELECT|BUTTON|A)$/.test(tag) || (tag === "INPUT" && t.type !== "radio") || t.closest(".lightbox")) return;
    e.preventDefault();
    if (!go.disabled) go.click();
  });
  go.addEventListener("click", async () => {
    // What is sent is snapshotted now: the line and the pick as they are, whatever he edits while it is in flight.
    const snap = sig(), sentPick = pick;
    if (!line() || pending || go.disabled) return;
    pending = true;
    sync();
    // Every screenshot is stored first, one at a time; if one fails nothing is sent and all stays as it was.
    for (const [i, x] of shots.entries()) {
      if (x.path) continue; // stored by an earlier try
      msg.className = "reply-msg"; msg.textContent = `Uploading screenshot ${i + 1} of ${shots.length}`;
      const r = await SEND.upload(askId, x.file);
      if (!r.path) {
        pending = false;
        msg.className = "reply-msg is-warn"; msg.textContent = `Not sent: screenshot ${i + 1} did not upload (${r.why}). Nothing was sent.`;
        sync();
        return;
      }
      x.path = r.path;
    }
    const text = line(true);
    const r = await SEND.sendKeyed(held, { agent: lead, kind: "answer", ask: askId, pick: sentPick, text }, undefined, (t) => { msg.className = "reply-msg is-quiet"; msg.textContent = t; });
    pending = false;
    if (!r.sent) { msg.className = "reply-msg is-warn"; msg.textContent = r.why; sync(); return; }
    msg.className = "reply-msg"; msg.textContent = "";
    sentLine = text; sentSig = snap;
    await refresh();
    sync();
    if (r.again) msg.textContent = `Already sent at ${at(r.at)}, so it was not sent again.`;
    if (onSent) {
      try { await onSent(); }
      catch { msg.textContent = "Your answer was sent, but Atlas could not read the next ask. Reload to continue."; }
    }
  });
  // The server's own state, read again now and every 15 s: sent turns to recorded when the file says so, or to late.
  async function refresh() {
    try { const r = await get(); if (r) answer = r.answer || null; } catch { /* the last state stays */ }
    draw();
    // A new answer, once sent, shows the replacing form on the next one.
    sync();
  }
  setInterval(refresh, 15000);
  tail.append(row, msg, landed);
  box.append(options, noteLbl, noteField, pics, refuse, lineLbl, shown, tail);
  if (off) box.append(el("p", "reply-off", off));
  draw();
  sync();
  return { node: box };
}
