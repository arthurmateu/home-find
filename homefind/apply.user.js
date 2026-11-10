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
// from auto-apply; send also presses the form's send button), or when you press "✉ Fill in" in the
// corner. Whenever a form is sent on an ad, it tells homefind, which moves the ad to Applied.
// apply.toml names the fields the way the German form does; SAME finds them on ImmoScout24's
// English pages too, which get filled in as they are.
(() => {
  "use strict";
  const HOMEFIND = "http://localhost:__PORT__";
  const VERSION = "__VERSION__";
  const adId = () => location.pathname.match(/^\/(?:en\/)?expose\/(\d+)/)?.[1];
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
  const norm = (s) => String(s ?? "").replace(/[*\s]+/g, " ").trim().toLowerCase();
  const digits = (s) => String(s ?? "").replace(/\D/g, "");
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const textOf = (el) => (el?.innerText ?? el?.textContent ?? "").replace(/\s+/g, " ").trim();
  const CONTROLS = "input, select, textarea, [role=switch], [role=checkbox], [role=combobox], button[aria-haspopup=listbox]";
  // The form's send button ("Abschicken in 5 s" while ImmoScout24 makes you wait), and its name.
  const SUBMIT = /^(abschicken|absenden|senden|nachricht senden|anfrage senden|send|send (message|request|enquiry|inquiry)|submit)( in \d+ s)?$/i;
  const sendName = (b) => (textOf(b) || b?.value || "Abschicken").replace(/ in \d+ s$/i, "");
  // What ImmoScout says once a message is sent (in German, then on its English pages).
  const sentTexts = () => textOf(document.body).match(new RegExp(
    "(nachricht|anfrage)[^.]{0,60}(gesendet|versendet|verschickt|übermittelt)|erfolgreich (gesendet|versendet|verschickt|übermittelt)|" +
    "\\b(message|request|enquiry|inquiry)\\b[^.]{0,60}\\b(sent|delivered|submitted)\\b|\\b(successfully (sent|delivered|submitted)|(sent|delivered|submitted) successfully)\\b",
    "gi")) || [];
  // Words that mean the same on the form, labels and options alike: its German ones (as apply.toml
  // has them) and its English ones, as ImmoScout24's own texts have them.
  const SAME = [
    ["Anrede", "Salutation"], ["Herr", "Mr."], ["Frau", "Ms."], ["Divers", "Diverse"],
    ["Vorname", "First name"], ["Nachname", "Last name"], ["Telefonnummer", "Phone number"],
    ["Straße", "Street"], ["Hausnummer", "House number"], ["Postleitzahl", "Postal code"], ["Ort", "City"],
    ["Einzugstermin", "Move-in date"], ["ab sofort", "From now on"], ["flexibel", "Flexible"],
    ["Haushaltsgröße", "Household size"], ["Einpersonenhaushalt", "Single-person household"],
    ["Zwei Erwachsene", "Two adults"], ["Familie", "Family"], ["Wohngemeinschaft", "Shared flat"],
    ["Erwachsene", "Adults"], ["Kinder", "Children"], ["Haustiere", "Pets"], ["Beschäftigungsart", "Employment type"],
    ["Angestellte:r", "Employee"], ["Arbeiter:in", "Worker"], ["Selbstständige:r", "Self-employed"],
    ["Beamter:Beamtin", "Civil servant"], ["Auszubildende:r", "Trainee"], ["Student:in", "Student"],
    ["Arbeitssuchende:r", "Job seeker"], ["Rentner:in", "Retired"],
    ["Monatliches netto Haushaltseinkommen", "Monthly net household income"],
    ["Bewerbungsunterlagen", "Application documents"], ["Vorhanden", "Available"], ["Nicht vorhanden", "Not available"],
    ["Nutzung für gewerbliche Zwecke", "Use for commercial purposes"],
    ["Mietrückstände in den letzten 5 Jahren", "Rent arrears in the last 5 years"],
    ["Insolvenzverfahren in den letzten 5 Jahren", "Insolvency proceedings in the last 5 years"],
    ["Raucher:in", "Smoker"], ["Ja", "Yes"], ["Nein", "No"],
    ["Anbieter:in darf dein Profil sehen", "Agent may view your profile"],
    ["Anbieter:in folgen für Inserate & Profi-Tipps", "Follow agent for listings & pro tips"],
    ["Erhalte Angebote von Umzugsfirmen", "Receive offers from moving companies"],
    ["Stich hervor: Ja zur Kautionsbürgschaft+", "Stand out: Choose Kautionsbürgschaft+"],
  ];
  const alike = (word) => {
    const w = norm(word);
    return (SAME.find((group) => group.some((s) => norm(s) === w)) || [word]).map(norm);
  };

  // ---- talking to homefind
  const call = (method, path, body) => new Promise((done, fail) => GM_xmlhttpRequest({
    method, url: HOMEFIND + path, timeout: 10000,
    headers: { "Content-Type": "application/json" }, data: body ? JSON.stringify(body) : undefined,
    onload: (r) => r.status < 300 ? done(r.responseText ? JSON.parse(r.responseText) : null)
                                  : fail(new Error(r.responseText || `homefind answered HTTP ${r.status}`)),
    onerror: () => fail(new Error(`homefind isn't running (${HOMEFIND})`)),
    ontimeout: () => fail(new Error("homefind didn't answer")),
  }));
  // How the application is going, for the ad's card in homefind (it ignores ads it doesn't know).
  const report = (step, body) => {
    if (adId()) call("POST", "/api/apply-report", { key: `immoscout:${adId()}`, step, ...body }).catch(() => {});
  };
  const tell = (step, note, by) => report(step, { note, by });

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
           buttons.find((b) => /^(((send|write)( a)? )?message|contact( the)?( provider| landlord| agent| owner)?|send (an )?(e-?mail|request|enquiry|inquiry))$/i.test(textOf(b))) ||
           buttons.find((b) => /nachricht|kontaktieren|message|contact/i.test(textOf(b)) &&
                               !/hilfe|melden|teilen|help|report|share|contact us|^messages$/i.test(textOf(b)));
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
  // Whether an amount falls in an option that names a range ("2.000 - 3.000 €", "< 500 €", "> 5.000 €"):
  // first going by from-up-to (500 in "500 - 1.000"), then taking either end (5000 in "4.000 - 5.000").
  const holds = (text, n, ends) => {
    const t = text.replace(/(\d)\.(?=\d{3}(?!\d))/g, "$1");  // 2.000 → 2000
    const nums = (t.match(/\d+/g) || []).map(Number);
    if (nums.length === 2 && /\d\s*[-–]\s*\d/.test(t)) return nums[0] <= n && (ends ? n <= nums[1] : n < nums[1]);
    if (nums.length !== 1) return false;
    if (/^\s*(<|≤|unter|bis|weniger als|under|up to|less than)/i.test(t)) return ends ? n <= nums[0] : n < nums[0];
    if (/^\s*(>|≥|über|ab|mehr als|over|from|more than)/i.test(t)) return ends ? n >= nums[0] : n > nums[0];
    return false;
  };
  // The option to pick for `want`: by its text (or value) or a word that means the same (SAME), else,
  // for an amount, the range it's in.
  const pick = (opts, want, text) => {
    const words = alike(want);
    return opts.find((o) => words.includes(norm(text(o))) || (o.value !== undefined && words.includes(norm(o.value)))) ||
           opts.find((o) => words.some((w) => w && norm(text(o)).startsWith(w))) ||
           (typeof want === "number" ? opts.find((o) => holds(text(o), want, false)) ||
                                       opts.find((o) => holds(text(o), want, true)) : undefined);
  };

  // Fills in the form (opening it first if need be) and returns what it did, with the send button.
  // With `auto`, an ad ImmoScout says you've contacted before is left alone.
  async function fill({ message, fields }, auto) {
    const already = textOf(document.body).match(/(bereits|schon) (kontaktiert|angefragt|eine nachricht)[^.]{0,40}|du hast [^.]{0,40}(kontaktiert|angefragt|eine nachricht gesendet)|nachricht (gesendet|verschickt) am [\d.]+/i)?.[0] ||
                    textOf(document.body).match(/already (contacted|enquired|inquired|messaged)[^.]{0,40}|(you've|you have) (contacted|messaged)[^.]{0,40}|message sent on [\d./]+/i)?.[0];
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
      const words = alike(name);
      const fits = (el) => !used.has(el) && ![...used].some((u) => u.contains(el) || el.contains(u));
      const el = controls.find((c) => fits(c) && words.includes(norm(labels.get(c)))) ||
                 controls.find((c) => fits(c) && words.some((w) => w && norm(labels.get(c)).startsWith(w)));
      if (!el) continue;  // this ad's form doesn't ask for it
      used.add(el);
      let why = "", expect = want;  // what the field should show then: for a dropdown, the option picked
      if (el.matches("input[type=checkbox], [role=switch], [role=checkbox]")) {
        if (isOn(el) !== !!want) el.click();
      } else if (el.tagName === "SELECT") {
        const opts = [...el.options];
        const opt = pick(opts, want, (o) => o.text);
        if (opt) { put(el, opt.value); expect = opt.text.trim(); }
        else why = `no option “${want}” (it has ${opts.map((o) => o.text.trim()).filter(Boolean).join(", ")})`;
      } else if (!el.matches("input, textarea")) {  // a dropdown of its own making: open it, pick the option
        el.click();
        await sleep(400);
        const opt = pick([...document.querySelectorAll("[role=option]")].filter(shown), want, textOf);
        if (opt) { expect = textOf(opt); opt.click(); } else { why = `no option “${want}”`; el.click(); }
      } else {
        put(el, String(want));
      }
      await sleep(60);
      if (!why && !same(valueOf(el), expect)) why = `shows “${valueOf(el)}” instead`;
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
    const done = { filled, unset, missing, loggedOut, submit,
                   fields: controls.map((el) => [labels.get(el), el.tagName.toLowerCase() + (el.type ? ":" + el.type : ""),
                                                 String(valueOf(el)).slice(0, 40)]) };
    if (!submit) return { ...done, error: "found no button to send the form" };
    submit.scrollIntoView({ block: "center" });  // the last step, for you to take
    submit.style.outline = "3px solid #00d9b1";
    submit.style.outlineOffset = "3px";
    await sleep(800);  // let the form check what it was given
    return done;
  }

  // ---- fill in (and maybe send): `how` is fill (you send; ✉ Apply or the corner button), auto (the
  // same, from auto-apply) or send (auto-apply presses the send button too).
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
      report("form", { url: location.href, form: { ...r, submit: !!r.submit } });
      if (r.already) {
        const note = `Not applied: ImmoScout says you've been in touch already (“${r.already}”).`;
        say(note, "wait");
        return tell("failed", note);
      }
      const press = sendName(r.submit);
      const problems = [...(r.error ? [r.error] : []), ...(r.loggedOut ? ["you're not logged in to ImmoScout24"] : []),
                        ...r.unset.map(([label, why]) => `${label}: ${why}`),
                        ...r.missing.map((label) => `${label}: apply.toml doesn't say`)];
      if (how === "send" && !problems.length) {
        say("Sending…");
        for (let i = 0; i < 120 && r.submit.disabled; i++) await sleep(500);  // "Abschicken in 30 s"
        if (!r.submit.disabled) {
          tell("sending", `Pressed ${press}, waiting for ImmoScout to confirm…`);
          watch("homefind", r.submit);
          r.submit.click();
          return;
        }
        problems.push(`${press} stayed greyed out`);
      }
      const note = problems.length ? `Fill in the rest, then press ${press}: ${problems.join("; ")}.`
                                   : `Check the form and press ${press}.`;
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

  // ---- the form was sent (by you, or by `run`): it went through once ImmoScout says so, or the form
  // closes without complaining. Ads homefind sent by itself close their tab after a few seconds.
  let watching = false;
  function watch(by, button) {
    if (watching || !adId()) return;
    watching = true;
    const press = sendName(button);
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
                                : `Pressed ${press}, but ImmoScout didn't say it was sent: check the ad in Firefox.`;
        say(note, "wait");
        tell("ready", note);
      }
    }, 500);
  }
  const isSend = (b) => SUBMIT.test(textOf(b) || b.value || "") && !!b.closest("form, [role=dialog], dialog")?.querySelector("textarea");
  document.addEventListener("click", (e) => {
    const b = e.target.closest?.("button, input[type=submit]");
    if (b && isSend(b)) watch("you", b);
  }, true);
  document.addEventListener("submit", (e) => { if (e.target.querySelector?.("textarea")) watch("you", e.submitter); }, true);

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
