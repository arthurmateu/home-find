// ==UserScript==
// @name         homefind: ImmoScout24 contact form
// @namespace    homefind
// @version      __VERSION__
// @description  Fills in ImmoScout24's contact form with your details from homefind's apply.toml, and tells homefind once it's sent.
// @match        https://www.immobilienscout24.de/*
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @grant        window.close
// @run-at       document-start
// @noframes
// @updateURL    http://localhost:__PORT__/homefind.user.js
// @downloadURL  http://localhost:__PORT__/homefind.user.js
// ==/UserScript==

// homefind's Firefox helper (homefind serves it at /homefind.user.js, for Tampermonkey; apply.py
// has the whole story). On an ImmoScout24 ad it fills in the contact form with what's in
// apply.toml when homefind opened the ad for that (#homefind=fill from ✉ Apply, =auto or =send
// from auto-apply; send also presses Abschicken), or when you press "✉ Fill in" in the corner.
// Whenever Abschicken goes through on an ad, it tells homefind, which moves the ad to Applied.
(() => {
  "use strict";
  const HOMEFIND = "http://localhost:__PORT__";
  const VERSION = "__VERSION__";
  const adId = () => location.pathname.match(/^\/expose\/(\d+)/)?.[1];
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
  const norm = (s) => String(s ?? "").replace(/[*\s]+/g, " ").trim().toLowerCase();
  const digits = (s) => String(s ?? "").replace(/\D/g, "");
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const textOf = (el) => (el?.innerText ?? el?.textContent ?? "").replace(/\s+/g, " ").trim();
  const CONTROLS = "input, select, textarea, [role=switch], [role=checkbox], [role=combobox], button[aria-haspopup=listbox]";
  const SUBMIT = /^(abschicken|absenden|senden|nachricht senden|anfrage senden)$/i;
  // What ImmoScout says once a message is sent.
  const sentTexts = () => textOf(document.body).match(/(nachricht|anfrage)[^.]{0,60}(gesendet|versendet|verschickt|übermittelt)|erfolgreich (gesendet|versendet|verschickt|übermittelt)/gi) || [];

  // ---- talking to homefind
  const call = (method, path, body) => new Promise((done, fail) => GM_xmlhttpRequest({
    method, url: HOMEFIND + path, timeout: 10000,
    headers: { "Content-Type": "application/json" }, data: body ? JSON.stringify(body) : undefined,
    onload: (r) => r.status < 300 ? done(r.responseText ? JSON.parse(r.responseText) : null)
                                  : fail(new Error(r.responseText || `homefind answered HTTP ${r.status}`)),
    onerror: () => fail(new Error(`homefind isn't running (${HOMEFIND})`)),
    ontimeout: () => fail(new Error("homefind didn't answer")),
  }));
  // Where the application is at, for the ad's card in homefind (it ignores ads it doesn't know).
  const tell = (step, note, by) => {
    if (adId()) call("POST", "/api/apply-report", { key: `immoscout:${adId()}`, step, note, by }).catch(() => {});
  };

  // ---- the box in the corner: what it did, and "✉ Fill in"
  let box;
  function panel() {
    if (box) return box;
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647";
    const root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `<style>
      .box { font: 14px/1.4 system-ui, sans-serif; background: #fff; color: #1d1d1b; border: 1px solid #d5d5cf;
             border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,.18); padding: 10px 12px; max-width: 340px;
             display: grid; gap: 8px; }
      .head { display: flex; gap: 10px; align-items: center; justify-content: space-between; }
      b { color: #1f6f5c; } p { margin: 0; } p.wait { color: #a2560c; } p.bad { color: #a3322a; } p.ok { color: #1b7a4c; }
      button { font: inherit; font-weight: 600; border: 0; border-radius: 8px; padding: 6px 12px; cursor: pointer;
               background: #1f6f5c; color: #fff; }
      button:disabled { opacity: .6; cursor: default; }
    </style><div class="box"><div class="head"><b>homefind</b><button id="go">✉ Fill in</button></div><p id="say" hidden></p></div>`;
    document.documentElement.append(host);
    box = { host, say: root.getElementById("say"), go: root.getElementById("go") };
    box.go.addEventListener("click", () => run("fill"));
    return box;
  }
  function say(text, kind = "") {
    const b = panel();
    b.say.textContent = text;
    b.say.className = kind;
    b.say.hidden = !text;
  }

  // ---- filling in the form. Fields are found by their labels, as on screen, so apply.toml can name
  // them the way the form does ("Haustiere" for "Haustiere (außer Kleintiere, z. B. Hamster)").

  // The field's label: its <label>, aria-labelledby or aria-label, else the text next to it
  // in the smallest box around it that holds no other field.
  const labelOf = (el) => {
    const ids = el.getAttribute("aria-labelledby");
    const own = el.labels?.length ? textOf(el.labels[0])
      : ids ? ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" ")
      : el.getAttribute("aria-label");
    if (own) return own;
    for (let p = el.parentElement, i = 0; p && i < 5; p = p.parentElement, i++) {
      if ([...p.querySelectorAll(CONTROLS)].some((c) => !c.contains(el) && !el.contains(c))) break;
      const text = [...p.childNodes].filter((n) => !n.contains(el) && !(n.matches?.(CONTROLS) || n.querySelector?.(CONTROLS)))
        .map(textOf).join(" ").trim();
      if (text) return text;
    }
    return el.getAttribute("placeholder") || "";
  };
  const opener = () => {  // the ad's button for writing to the landlord
    const buttons = [...document.querySelectorAll("button, a, [role=button]")].filter(shown);
    return buttons.find((b) => /^(nachricht( schreiben| senden)?|anbieter:?(in)? kontaktieren|kontaktieren|kontakt aufnehmen|anfrage senden|e-mail senden)$/i.test(textOf(b))) ||
           buttons.find((b) => /nachricht|kontaktieren/i.test(textOf(b)) && !/hilfe|melden|teilen/i.test(textOf(b)));
  };
  const messageBox = () => {
    const boxes = [...document.querySelectorAll("textarea")].filter((t) => shown(t) && !t.disabled);
    return boxes.find((t) => /nachricht|message/i.test(labelOf(t) + " " + t.name + " " + t.id)) ||
           boxes.find((t) => t.closest("form"));
  };
  // How React (and the like) take a value: through the element's own setter, then an input event.
  const put = (el, value) => {
    const proto = { SELECT: HTMLSelectElement, TEXTAREA: HTMLTextAreaElement }[el.tagName] || HTMLInputElement;
    el.focus();
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
  };
  const isOn = (el) => el.matches("input") ? el.checked : el.getAttribute("aria-checked") === "true" ||
                                                          !!el.querySelector("input:checked");
  const valueOf = (el) => el.matches("input[type=checkbox], input[type=radio], [role=switch], [role=checkbox]") ? isOn(el)
    : el.tagName === "SELECT" ? (el.value === "" ? "" : el.selectedOptions[0]?.text.trim() ?? "")
    : el.matches("input, textarea") ? el.value : textOf(el);
  const same = (have, want) => typeof want === "boolean" ? have === want
    : norm(have) === norm(want) || (typeof want === "number" && digits(have) === digits(want));

  // Fills in the form (opening it first if need be) and returns what it did, with the Abschicken button.
  // With `auto`, an ad ImmoScout says you've contacted before is left alone.
  async function fill({ message, fields }, auto) {
    const already = textOf(document.body).match(/(bereits|schon) (kontaktiert|angefragt|eine nachricht)[^.]{0,40}|du hast [^.]{0,40}(kontaktiert|angefragt|eine nachricht gesendet)|nachricht (gesendet|verschickt) am [\d.]+/i)?.[0];
    if (already && auto) return { already };
    let msg = messageBox();
    if (!msg) {
      const button = opener();
      if (!button) return { error: "found no button to contact the landlord" };
      button.click();
      for (let i = 0; i < 40 && !(msg = messageBox()); i++) await sleep(250);
      if (!msg) return { error: "the contact form didn't open" };
      await sleep(500);  // let it finish appearing (and fill in what ImmoScout knows of you)
    }
    const root = msg.closest("form") || msg.closest("[role=dialog], dialog") || document.body;
    const filled = [], unset = [];
    put(msg, message);
    if (msg.value.trim() !== message.trim()) unset.push(["Nachricht", "didn't take the text"]);

    const controls = [...root.querySelectorAll(CONTROLS)].filter((el) => el !== msg && !el.disabled && !el.readOnly &&
      !(el.matches("input") && /^(hidden|submit|button|file|image|reset)$/.test(el.type)));
    const labels = new Map(controls.map((el) => [el, labelOf(el).replace(/\s*\*\s*$/, "")]));
    const used = new Set();
    for (const [name, want] of Object.entries(fields)) {
      const key = norm(name);
      const fits = (el) => !used.has(el) && ![...used].some((u) => u.contains(el) || el.contains(u));
      const el = controls.find((c) => fits(c) && norm(labels.get(c)) === key) ||
                 controls.find((c) => fits(c) && norm(labels.get(c)).startsWith(key));
      if (!el) continue;  // this ad's form doesn't ask for it
      used.add(el);
      let why = "";
      if (el.matches("input[type=checkbox], [role=switch], [role=checkbox]")) {
        if (isOn(el) !== !!want) el.click();
      } else if (el.tagName === "SELECT") {
        const opts = [...el.options];
        const opt = opts.find((o) => norm(o.text) === norm(want) || norm(o.value) === norm(want)) ||
                    opts.find((o) => norm(o.text).startsWith(norm(want)));
        if (opt) put(el, opt.value);
        else why = `no option “${want}” (it has ${opts.map((o) => o.text.trim()).filter(Boolean).join(", ")})`;
      } else if (!el.matches("input, textarea")) {  // a dropdown of its own making: open it, pick the option
        el.click();
        await sleep(400);
        const opt = [...document.querySelectorAll("[role=option]")].filter(shown).find((o) => norm(textOf(o)) === norm(want));
        if (opt) opt.click(); else { why = `no option “${want}”`; el.click(); }
      } else {
        put(el, String(want));
      }
      await sleep(60);
      if (!why && !same(valueOf(el), want)) why = `shows “${valueOf(el)}” instead`;
      if (why) unset.push([labels.get(el), why]); else filled.push(labels.get(el));
    }

    // Asked for, but neither ImmoScout nor apply.toml filled it in.
    const required = (el) => el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(labelOf(el));
    const missing = controls.filter((el) => required(el) && !used.has(el) && shown(el) &&
                                            (valueOf(el) === "" || valueOf(el) === false)).map((el) => labels.get(el));
    const email = [...root.querySelectorAll("input")].find((el) => el.type === "email" || /^e-?mail/i.test(norm(labelOf(el))));
    const loggedOut = !!email && !email.disabled && !email.readOnly && !email.value;
    for (const el of controls) {  // mark what's left to do
      if (missing.includes(labels.get(el)) || unset.some(([l]) => l === labels.get(el))) el.style.outline = "3px solid #e5484d";
    }
    const buttons = [...root.querySelectorAll("button, input[type=submit]")].filter(shown);
    const submit = buttons.find((b) => SUBMIT.test(textOf(b) || b.value)) || buttons.find((b) => b.type === "submit");
    const report = { filled, unset, missing, loggedOut, submit,
                     fields: controls.map((el) => [labels.get(el), el.tagName.toLowerCase() + (el.type ? ":" + el.type : ""),
                                                   String(valueOf(el)).slice(0, 40)]) };
    if (!submit) return { ...report, error: "found no Abschicken button" };
    submit.scrollIntoView({ block: "center" });  // the last step, for you to take
    submit.style.outline = "3px solid #00d9b1";
    submit.style.outlineOffset = "3px";
    await sleep(800);  // let the form check what it was given
    return report;
  }

  // ---- fill in (and maybe send): `how` is fill (you send; ✉ Apply or the corner button), auto (the
  // same, from auto-apply) or send (auto-apply presses Abschicken too).
  let busy = false;
  async function run(how) {
    if (busy) return;
    busy = true;
    panel().go.disabled = true;
    say("Filling in the form…");
    tell("filling", "Filling in the form in Firefox…");
    try {
      const profile = await call("GET", "/api/apply-profile");
      const r = await fill(profile, how !== "fill");
      call("POST", "/api/apply-report", { key: `immoscout:${adId()}`, step: "form", url: location.href,
                                          form: { ...r, submit: !!r.submit } }).catch(() => {});
      if (r.already) {
        const note = `Not applied: ImmoScout says you've been in touch already (“${r.already}”).`;
        say(note, "wait");
        return tell("failed", note);
      }
      const problems = [...(r.error ? [r.error] : []), ...(r.loggedOut ? ["you're not logged in to ImmoScout24"] : []),
                        ...r.unset.map(([label, why]) => `${label}: ${why}`),
                        ...r.missing.map((label) => `${label}: apply.toml doesn't say`)];
      if (how === "send" && !problems.length) {
        tell("sending", "Pressed Abschicken, waiting for ImmoScout to confirm…");
        say("Sending…");
        pressing = true;
        try { r.submit.click(); } finally { pressing = false; }
        return;
      }
      const note = problems.length ? `Fill in the rest, then press Abschicken: ${problems.join("; ")}.`
                                   : "Check the form and press Abschicken.";
      say(note, problems.length ? "wait" : "");
      tell("ready", (how === "send" ? "Not sent by itself. " : "") + note.replace("Check the form", "Check the form in Firefox"));
    } catch (e) {
      say(`Couldn't fill it in: ${e.message}`, "bad");
      tell("failed", `Couldn't fill it in: ${e.message}`);
    } finally {
      busy = false;
      panel().go.disabled = false;
    }
  }

  // ---- Abschicken was pressed (by you, or by `run`): it went through once ImmoScout says so, or the
  // form closes without complaining. Ads homefind sent by itself close their tab after a few seconds.
  let pressing = false, watching = false;
  function watch(by) {
    if (watching || !adId()) return;
    watching = true;
    const before = new Set(sentTexts()), start = Date.now();
    const timer = setInterval(() => {
      const complaints = [...document.querySelectorAll("[role=alert], [aria-invalid=true]")]
        .some((el) => shown(el) && (el.matches("[aria-invalid=true]") || textOf(el)));
      const sent = sentTexts().some((t) => !before.has(t)) || (!messageBox() && !complaints);
      if (!sent && Date.now() - start < (complaints ? 3000 : 20000)) return;
      clearInterval(timer);
      watching = false;
      if (sent) {
        say(by === "homefind" ? "✓ Sent by homefind." : "✓ Sent. homefind has it on Applied now.", "ok");
        tell("sent", by === "homefind" ? "Sent by homefind." : "Sent.", by);
        if (by === "homefind") setTimeout(() => window.close(), 5000);
      } else {
        const note = complaints ? "ImmoScout didn't take the form: see what it says there."
                                : "Pressed Abschicken, but ImmoScout didn't say it was sent: check the ad in Firefox.";
        say(note, "wait");
        tell("ready", note);
      }
    }, 500);
  }
  const isSend = (b) => SUBMIT.test(textOf(b) || b.value || "") && !!b.closest("form, [role=dialog], dialog")?.querySelector("textarea");
  document.addEventListener("click", (e) => {
    const b = e.target.closest?.("button, input[type=submit]");
    if (b && isSend(b)) watch(pressing ? "homefind" : "you");
  }, true);
  document.addEventListener("submit", (e) => { if (e.target.querySelector?.("textarea")) watch(pressing ? "homefind" : "you"); }, true);

  // ---- on an ad: what homefind opened it for (read now, before the page's own scripts can change
  // the address; kept for this tab until it's done), then the corner box once the page is up.
  const asked = location.hash.match(/homefind=(fill|auto|send)/)?.[1];
  if (asked && adId()) sessionStorage.setItem(`homefind:${adId()}`, asked);
  addEventListener("load", async () => {
    let shownFor = null;
    const update = () => {  // the page can move to another ad without loading (and away from ads)
      if (adId() === shownFor) return;
      shownFor = adId();
      panel().host.hidden = !shownFor;
      say("");
      if (shownFor) call("GET", `/api/apply-hello?version=${VERSION}`).catch(() => {});
    };
    update();
    setInterval(update, 1000);
    const how = adId() && sessionStorage.getItem(`homefind:${adId()}`);
    if (!how) return;
    sessionStorage.removeItem(`homefind:${adId()}`);  // once: a reload doesn't send it again
    history.replaceState(history.state, "", location.href.replace(/#homefind=\w+$/, ""));
    for (let i = 0; i < 40 && !messageBox() && !opener(); i++) await sleep(250);  // the page may still be drawing it
    run(how);
  });
})();
