// Fills in ImmoScout24's contact form on an ad's page; apply.py runs this in the
// homefind browser window. Fields are found by their labels, as on screen, so
// apply.toml can name them the way the form does ("Haustiere" for "Haustiere
// (außer Kleintiere, z. B. Hamster)"). Returns what it did, for apply.py to
// report, and marks the Abschicken button (data-homefind-submit) for it to press.
// With `auto`, an ad ImmoScout says you've contacted before is left alone.
async ({ message, fields, auto }) => {
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
  const norm = (s) => String(s ?? "").replace(/[*\s]+/g, " ").trim().toLowerCase();
  const digits = (s) => String(s ?? "").replace(/\D/g, "");
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const textOf = (el) => (el?.innerText ?? el?.textContent ?? "").replace(/\s+/g, " ").trim();
  const CONTROLS = "input, select, textarea, [role=switch], [role=checkbox], [role=combobox], button[aria-haspopup=listbox]";

  if (document.title.includes("Roboter") || document.querySelector("#captcha-container")) return { captcha: true };

  // The field's label: its <label>, aria-labelledby or aria-label, else the text next to it
  // in the smallest box around it that holds no other field.
  const labelOf = (el) => {
    const ids = el.getAttribute("aria-labelledby");
    const own = el.labels?.length ? textOf(el.labels[0])
      : ids ? ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" ")
      : el.getAttribute("aria-label");
    if (own) return own;
    for (let box = el.parentElement, i = 0; box && i < 5; box = box.parentElement, i++) {
      if ([...box.querySelectorAll(CONTROLS)].some((c) => !c.contains(el) && !el.contains(c))) break;
      const text = [...box.childNodes].filter((n) => !n.contains(el) && !(n.matches?.(CONTROLS) || n.querySelector?.(CONTROLS)))
        .map(textOf).join(" ").trim();
      if (text) return text;
    }
    return el.getAttribute("placeholder") || "";
  };

  const messageBox = () => {
    const boxes = [...document.querySelectorAll("textarea")].filter((t) => shown(t) && !t.disabled);
    return boxes.find((t) => /nachricht|message/i.test(labelOf(t) + " " + t.name + " " + t.id)) ||
           boxes.find((t) => t.closest("form"));
  };
  const already = textOf(document.body).match(/(bereits|schon) (kontaktiert|angefragt|eine nachricht)[^.]{0,40}|du hast [^.]{0,40}(kontaktiert|angefragt|eine nachricht gesendet)|nachricht (gesendet|verschickt) am [\d.]+/i)?.[0];
  if (already && auto) return { already };

  let box = messageBox();
  if (!box) {
    const buttons = [...document.querySelectorAll("button, a, [role=button]")].filter(shown);
    const opener = buttons.find((b) => /^(nachricht( schreiben| senden)?|anbieter:?(in)? kontaktieren|kontaktieren|kontakt aufnehmen|anfrage senden|e-mail senden)$/i.test(textOf(b))) ||
                   buttons.find((b) => /nachricht|kontaktieren/i.test(textOf(b)) && !/hilfe|melden|teilen/i.test(textOf(b)));
    if (!opener) return { error: "found no button to contact the landlord" };
    opener.click();
    for (let i = 0; i < 40 && !(box = messageBox()); i++) await sleep(250);
    if (!box) return { error: "the contact form didn't open" };
    await sleep(500);  // let it finish appearing (and fill in what ImmoScout knows of you)
  }
  const root = box.closest("form") || box.closest("[role=dialog], dialog") || document.body;

  // How React (and the like) take a value: through the element's own setter, then an input event.
  const put = (el, value) => {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    el.focus();
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
  };
  const isOn = (el) => el.matches("input") ? el.checked : el.getAttribute("aria-checked") === "true" ||
                                                          !!el.querySelector("input:checked");
  const valueOf = (el) => el.matches("input[type=checkbox], input[type=radio], [role=switch], [role=checkbox]") ? isOn(el)
    : el instanceof HTMLSelectElement ? (el.value === "" ? "" : el.selectedOptions[0]?.text.trim() ?? "")
    : el.matches("input, textarea") ? el.value : textOf(el);
  const same = (have, want) => typeof want === "boolean" ? have === want
    : norm(have) === norm(want) || (typeof want === "number" && digits(have) === digits(want));

  const filled = [], unset = [];
  put(box, message);
  if (box.value.trim() !== message.trim()) unset.push(["Nachricht", "didn't take the text"]);

  const controls = [...root.querySelectorAll(CONTROLS)].filter((el) => el !== box && !el.disabled && !el.readOnly &&
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
    } else if (el instanceof HTMLSelectElement) {
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
  for (const el of controls) {  // mark what's left to do, for you in the window
    if (missing.includes(labels.get(el)) || unset.some(([l]) => l === labels.get(el))) el.style.outline = "3px solid #e5484d";
  }

  const buttons = [...root.querySelectorAll("button, input[type=submit]")].filter(shown);
  const submit = buttons.find((b) => /^(abschicken|absenden|senden|nachricht senden|anfrage senden)$/i.test(textOf(b) || b.value)) ||
                 buttons.find((b) => b.type === "submit");
  const report = { filled, unset, missing, loggedOut, already: already || null,
                   fields: controls.map((el) => [labels.get(el), el.tagName.toLowerCase() + (el.type ? ":" + el.type : ""),
                                                 String(valueOf(el)).slice(0, 40)]) };
  if (!submit) return { ...report, error: "found no Abschicken button" };
  submit.setAttribute("data-homefind-submit", "");
  submit.scrollIntoView({ block: "center" });  // the last step, for you to take
  submit.style.outline = "3px solid #00d9b1";
  submit.style.outlineOffset = "3px";
  await sleep(800);  // let the form check what it was given
  return report;
}
