function createThreadPlanGuide(template, openLibrary) {
  const guide = template.content.firstElementChild.cloneNode(true);
  const at = name => guide.querySelector('[data-guide="' + name + '"]');
  const compact = window.matchMedia("(max-width: 730px)");
  const motion = window.matchMedia("(prefers-reduced-motion: no-preference)");
  const reset = () => ({ planned: false, confirmed: false, before: false,
                        recorded: false, reminderPreview: false, reminded: false });
  let state = reset();

  function show(name, visible) { at(name).hidden = !visible; }
  function message(number) { return guide.querySelector('[data-message="' + number + '"]'); }
  function activate(name, handler) { at(name).addEventListener("click", handler); }

  for (const [name, number] of [["review-quote", 5], ["sync-quote", 6]]) {
    const source = message(number);
    const cite = document.createElement("cite");
    cite.textContent = " — " + source.querySelector(".tpg-speaker").textContent;
    at(name).append("“" + source.querySelector("p").textContent + "”", cite);
  }
  for (const number of [3, 4]) {
    const source = message(number);
    const line = document.createElement("li");
    line.textContent = source.querySelector(".tpg-speaker").textContent + "：" + source.querySelector("p").textContent;
    at("review-context").append(line);
  }

  function markEvidence() {
    for (const number of [3, 4, 5, 6]) {
      message(number).dataset.evidence = state.planned && (number >= 5 || at("context").open);
    }
  }

  function render() {
    const original = !state.confirmed || state.before;
    show("intro", !state.planned);
    show("plan", state.planned);
    show("confirm", !state.confirmed);
    show("applied", state.confirmed);
    show("compare", state.confirmed);
    show("restart", state.planned);
    at("verdict").textContent = state.confirmed ? "已改期" : "建议改期";
    at("result-state").textContent = state.confirmed ? "已读回核对" : "尚未执行";
    at("result-state").classList.toggle("tpg-neutral", !state.confirmed);
    at("before").setAttribute("aria-pressed", state.before);
    at("after").setAttribute("aria-pressed", !state.before);
    at("result-note").textContent = !state.confirmed ? "这是原日历，确认后会在这里更新。"
      : original ? "正在查看改期前的日历。" : "只改了发布评审；验收走查没动，接口联调没创建。";
    at("review-time").textContent = original ? "14:00–14:30" : "16:00–16:30";
    at("review").dataset.changed = !original;
    const first = at(original ? "review" : "walk");
    if (at("friday").firstElementChild !== first) at("friday").prepend(first);
    show("changed", !original);
    show("unchanged", !original);
    show("saturday", state.recorded && !original);
    show("reminder", state.reminded);
    show("record", !state.recorded);
    show("followup", state.recorded);
    show("preview", !state.reminderPreview);
    show("reminder-plan", state.reminderPreview && !state.reminded);
    at("extra-status").textContent = state.reminded ? "已补提醒，仍是同一条样品交接。" : state.recorded ? "已记下样品交接。" : "";
    markEvidence();
  }

  function emphasize(name) {
    const target = at(name);
    if (motion.matches && target.animate) {
      target.animate([{ opacity: .45, transform: "translateY(5px)" }, { opacity: 1, transform: "translateY(0)" }],
        { duration: 220, easing: "cubic-bezier(.2,.8,.3,1)" });
    }
  }

  function renderWithMotion(delay = 0) {
    const rows = [at("review"), at("walk")];
    rows.forEach(row => row.getAnimations?.().forEach(animation => animation.cancel()));
    const positions = () => {
      const top = at("friday").getBoundingClientRect().top;
      return rows.map(row => row.getBoundingClientRect().top - top);
    };
    const previous = motion.matches ? positions() : null;
    render();
    if (!previous) return;
    const next = positions();
    rows.forEach((row, index) => {
      const delta = previous[index] - next[index];
      if (delta && row.animate) row.animate(
        [{ transform: "translateY(" + delta + "px)" }, { transform: "translateY(0)" }],
        { delay, duration: 320, easing: "cubic-bezier(.22,.75,.3,1)", fill: "backwards" });
    });
  }

  activate("analyze", () => {
    state.planned = true;
    if (compact.matches) at("source").open = false;
    render();
    emphasize("plan");
    guide.querySelector("#tpg-decision-title").focus({ preventScroll: !compact.matches });
  });
  at("context").addEventListener("toggle", markEvidence);
  activate("confirm", () => {
    if (!state.planned || state.confirmed) return;
    state.confirmed = true;
    renderWithMotion(120);
    emphasize("applied");
    guide.querySelector("#tpg-result-title").focus({ preventScroll: true });
    at("result").scrollIntoView({ behavior: motion.matches ? "smooth" : "auto", block: "start" });
  });
  activate("before", () => { if (!state.before) { state.before = true; renderWithMotion(); } });
  activate("after", () => { if (state.before) { state.before = false; renderWithMotion(); } });
  activate("record", () => {
    state.recorded = true;
    state.before = false;
    renderWithMotion();
    emphasize("saturday");
    at("preview").focus();
  });
  activate("preview", () => {
    state.reminderPreview = true;
    render();
    emphasize("reminder-plan");
    at("apply").focus();
  });
  activate("apply", () => {
    if (!state.recorded || !state.reminderPreview || state.reminded) return;
    state.reminded = true;
    state.before = false;
    renderWithMotion();
    emphasize("reminder");
    at("extra").querySelector("summary").focus({ preventScroll: true });
  });
  activate("restart", () => {
    state = reset();
    for (const name of ["context", "extra"]) at(name).open = false;
    at("source").open = true;
    renderWithMotion();
    at("analyze").focus();
  });
  activate("explore", openLibrary);
  render();
  return guide;
}
