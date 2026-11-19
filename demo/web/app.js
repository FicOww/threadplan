"use strict";

const main = document.querySelector("#main");
const feedback = document.querySelector("#feedback");
const dialog = document.querySelector("#confirmation");
const confirmButton = document.querySelector("#confirmation-submit");
const PAGE_SIZE = 12;
const operationLabels = { CREATE: "新增日程", UPDATE: "修改日程", CANCEL: "取消日程" };
const commandLabels = {
  PREPARED: "等待确认", SUCCEEDED: "已核验", IN_FLIGHT: "正在处理",
  OUTCOME_UNKNOWN: "结果待核查", VERIFY_PENDING: "等待读回核验", FAILED_BEFORE_WRITE: "未写入",
};
const decisionLabels = {
  PROPOSE: "待确认", WAIT: "需要补充", NO_ACTION: "无需变更",
  UNSUPPORTED: "暂不支持", FAILED: "处理失败",
};
const errors = {
  not_found: "找不到这个任务。可以回到场景库重新开始。",
  approval_expired: "确认已过期，请重新开始场景。",
  approval_source_changed: "任务已变化，请查看最新结果。",
  scenario_version_unavailable: "这个场景的版本已变化，请重新开始。",
  session_required: "会话已失效，请刷新页面。",
  sandbox_disabled: "本机尚未启用实时试用。",
  sandbox_busy: "已有一条任务正在分析，请稍后再试。",
  sandbox_daily_limit: "今天的试用额度已用完。",
  constructed_input_confirmation_required: "请确认这是合成内容，并同意发送分析。",
  invalid_simulated_event_time: "日程时间需要包含时区，结束时间需晚于开始时间。",
  actor_not_in_conversation: "请填写对话中实际出现的模拟发送者 ID。",
};
let catalog;
let selected = "全部";
let query = "";
let page = 1;
let noticeTimer;
let pollTimer;
let sandboxSeed;
let routeVersion = 0;

function element(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter(value => value !== undefined && value !== null));
  return node;
}

function button(text, className, action) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", () => action(node));
  return node;
}

function notify(message) {
  window.clearTimeout(noticeTimer);
  feedback.textContent = message;
  noticeTimer = window.setTimeout(() => { feedback.textContent = ""; }, 6200);
}

async function api(path, data) {
  if (window.threadplanOffline) {
    try {
      return window.threadplanOffline.request(path, data);
    } catch (error) {
      throw new Error(errors[error.message] || "这次操作未能完成。请查看现有任务状态。");
    }
  }
  const response = await fetch(path, data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(errors[result.error] || "这次操作未能完成。请查看现有任务状态。");
  return result;
}

async function run(control, action) {
  control.disabled = true;
  try {
    await action();
  } catch (error) {
    notify(error.message);
  } finally {
    control.disabled = false;
  }
}

function route(view, task, replace = false) {
  const query = task ? "task=" + encodeURIComponent(task) : view === "library" ? "" : "view=" + view;
  const url = window.threadplanOffline ? "#" + query : "/" + (query ? "?" + query : "");
  window.history[replace ? "replaceState" : "pushState"]({}, "", url);
  return renderRoute(true);
}

function headline(title, description) {
  return element("header", "section-head", element("h1", "", title),
    description && element("p", "quiet", description));
}

function library() {
  const grouped = new Map();
  for (const entry of catalog.cases) grouped.set(entry.category, (grouped.get(entry.category) || 0) + 1);
  const filters = element("aside", "filters");
  filters.setAttribute("aria-label", "按任务筛选");
  const list = element("div", "case-list");
  const count = element("span", "count");
  const pager = element("div", "pager");
  const search = element("input", "");
  search.type = "search";
  search.value = query;
  search.placeholder = "搜索场景";
  const label = element("label", "", element("span", "", "搜索场景"), search);
  label.firstChild.className = "visually-hidden";

  function renderList() {
    const visible = catalog.cases.filter(entry => (selected === "全部" || entry.category === selected) &&
      (entry.title + " " + entry.category).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
    const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
    page = Math.min(page, pages);
    count.textContent = visible.length + " 个场景";
    list.replaceChildren();
    if (!visible.length) list.append(element("p", "quiet", "没有找到匹配场景。"));
    for (const entry of visible.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)) {
      const launch = button("", "case", control => run(control, async () => {
        const task = await api("/api/tasks", { scenario: entry.id });
        await route("task", task.id);
      }));
      launch.replaceChildren(element("span", "", element("span", "case-title", entry.title),
        element("span", "case-type", entry.category)), element("span", "arrow", "→"));
      launch.lastChild.setAttribute("aria-hidden", "true");
      list.append(launch);
    }
    pager.replaceChildren();
    if (pages > 1) {
      const back = button("上一页", "secondary", () => changePage(-1));
      const next = button("下一页", "secondary", () => changePage(1));
      back.disabled = page === 1;
      next.disabled = page === pages;
      pager.append(back, element("span", "count", page + " / " + pages), next);
    }
  }
  function changePage(offset) {
    page += offset;
    renderList();
    list.querySelector("button")?.focus();
  }
  for (const [name, total] of [["全部", catalog.cases.length], ...grouped]) {
    const filter = button("", "", control => {
      selected = name;
      page = 1;
      for (const node of filters.children) node.setAttribute("aria-pressed", String(node === control));
      renderList();
    });
    filter.setAttribute("aria-pressed", String(selected === name));
    filter.append(element("span", "", name), element("span", "count", total));
    filters.append(filter);
  }
  search.addEventListener("input", () => { query = search.value; page = 1; renderList(); });
  renderList();
  main.replaceChildren(headline("场景库", "选择一段对话，试着把安排落实到日历。"),
    element("div", "library", filters, element("section", "",
      element("div", "searchbar", label, count), list, pager)));
}

async function historyView() {
  const tasks = await api("/api/tasks");
  const list = element("ul", "history-list");
  for (const task of tasks) {
    const link = button("", "row-link", () => route("task", task.id));
    link.append(element("span", "", task.title), element("span", "quiet", task.closed ? "已重置" : "继续"));
    list.append(element("li", "", link));
  }
  main.replaceChildren(headline("最近任务"), tasks.length ? list : element("p", "quiet", "还没有任务。"));
}

function consent(text) {
  const check = element("input", "");
  check.type = "checkbox";
  check.required = true;
  return { check, label: element("label", "consent", check, element("span", "", text)) };
}

function inputLabel(name, node) {
  return element("label", "", element("span", "", name), node);
}

function sandboxForm() {
  if (!catalog.live_enabled) {
    main.replaceChildren(headline("自由试用"), element("p", "quiet", "本机尚未启用实时试用。"),
      button("返回场景库", "text-button", () => route("library")));
    return;
  }
  const form = element("form", "sandbox-form");
  const type = element("select", "", element("option", "", "试写一条安排"), element("option", "", "导入合成对话"));
  const note = element("textarea", "");
  note.maxLength = 4500;
  const noteField = inputLabel("安排", note);
  const intake = element("textarea", "");
  const actor = element("input", "");
  const importPanel = element("div", "sandbox-form", inputLabel("合成对话 JSON", intake),
    inputLabel("你的模拟发送者 ID", actor));
  const zone = element("input", "");
  zone.value = sandboxSeed?.timezone || "Asia/Shanghai";
  const events = element("textarea", "");
  events.value = JSON.stringify(sandboxSeed?.events || [], null, 2);
  if (sandboxSeed) {
    type.selectedIndex = 1;
    intake.value = JSON.stringify(sandboxSeed.intake, null, 2);
    actor.value = sandboxSeed.actor;
    sandboxSeed = undefined;
  }
  const advanced = evidence("日历与时区", element("div", "sandbox-form", inputLabel("时区", zone),
    inputLabel("模拟日历 JSON", events), element("p", "quiet", "每项包含 title、start_at、end_at。时间需带时区，例如 2026-11-20T14:00:00+08:00。")));
  function setType() {
    const importing = type.selectedIndex === 1;
    importPanel.hidden = !importing;
    intake.required = actor.required = importing;
    noteField.hidden = importing;
    note.required = !importing;
  }
  type.addEventListener("change", setType);
  setType();
  const permission = consent("这些是合成内容，我同意将它们发送给 OpenRouter 分析。");
  const submit = element("button", "primary", "分析安排");
  submit.type = "submit";
  form.append(inputLabel("输入方式", type), noteField, importPanel, advanced, permission.label, submit);
  form.addEventListener("submit", event => {
    event.preventDefault();
    run(submit, async () => {
      const data = { timezone: zone.value, constructed: permission.check.checked };
      try {
        data.events = JSON.parse(events.value);
        if (type.selectedIndex === 1) data.intake = JSON.parse(intake.value);
      } catch {
        throw new Error("请检查 JSON 格式，再开始分析。");
      }
      if (data.intake) data.actor = actor.value;
      else data.note = note.value;
      const result = await api("/api/sandbox", data);
      await route("task", result.id);
    });
  });
  main.replaceChildren(headline("自由试用", "写一条模拟安排，或导入合成对话。结果可在模拟日历里验证。"), form);
}

function followupForm(task) {
  const answer = element("textarea", "");
  answer.required = true;
  answer.maxLength = 1200;
  const permission = consent("我同意将这条合成补充发送给 OpenRouter。");
  const submit = element("button", "primary", "提交补充");
  submit.type = "submit";
  const form = element("form", "followup-form", inputLabel("补充信息", answer), permission.label, submit);
  form.addEventListener("submit", event => {
    event.preventDefault();
    run(submit, async () => {
      await api(`/api/tasks/${task.id}/followup`, { answer: answer.value, constructed: permission.check.checked });
      await renderRoute(true);
    });
  });
  return evidence("补充信息", form);
}

function formatted(value, timezone, options) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value :
    new Intl.DateTimeFormat("zh-CN", { timeZone: timezone, ...options }).format(date);
}

function clock(value, timezone) {
  return formatted(value, timezone, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function eventTime(fields, timezone) {
  if (!fields?.start_at) return "";
  const options = { month: "numeric", day: "numeric" };
  const startDay = formatted(fields.start_at, timezone, options);
  const endDay = formatted(fields.end_at, timezone, options);
  const finish = fields.end_at ? "–" + (endDay === startDay ? "" : endDay + " ") + clock(fields.end_at, timezone) : "";
  return startDay + " " + clock(fields.start_at, timezone) + finish;
}

function evidence(title, content) {
  return element("details", "", element("summary", "", title), content);
}

function conversation(messages, timezone) {
  const list = element("ol", "conversation");
  for (const message of messages) {
    const stamp = element("time", "", formatted(message.sent_at, timezone,
      { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }));
    stamp.dateTime = message.sent_at;
    list.append(element("li", "", element("div", "message-byline",
      element("strong", "", message.sender_id), stamp), element("p", "message-body", message.text)));
  }
  return list;
}

function operationView(task, operation) {
  const target = task.records.find(record => record.event_ref === operation.event_ref);
  const before = target?.fields || {};
  const after = { ...before, ...operation.changes };
  const beforeTime = eventTime(before, task.timezone);
  const afterTime = eventTime(after, task.timezone);
  const title = (operation.kind === "CANCEL" ? before.title : after.title) || "无标题日程";
  const time = element("p", "times");
  if (beforeTime && beforeTime !== afterTime) time.append(element("span", "previous", beforeTime), element("span", "", "→"));
  if (afterTime) time.append(element("strong", "", afterTime));
  const fields = element("div", "event-detail");
  if (after.location) fields.append(element("p", "", after.location));
  for (const [key, label] of Object.entries({ title: "名称", location: "地点", description: "备注" })) {
    if (operation.kind === "UPDATE" && Object.hasOwn(operation.changes, key) && before[key] !== after[key]) {
      fields.append(element("p", "", label + " " + (before[key] || "未设置") + " → " + after[key]));
    }
  }
  return { title, beforeTime, afterTime, time, fields };
}

function askApproval(task, item, command, change) {
  const kind = item.operation.kind;
  const label = kind === "CANCEL" ? "确认取消" : kind === "UPDATE" ? "确认修改" : "确认新增";
  document.querySelector("#confirmation-title").textContent = operationLabels[kind];
  let details = change.title + "\n" + (kind === "UPDATE" && change.beforeTime !== change.afterTime ?
    change.beforeTime + " → " + change.afterTime : change.afterTime);
  details += window.threadplanOffline ? "\n在浏览器内回放这次模拟日历变更。" :
    "\n操作仅在此场景的模拟日历生效。";
  document.querySelector("#confirmation-detail").textContent = details;
  confirmButton.textContent = label;
  confirmButton.classList.toggle("destructive", kind === "CANCEL");
  confirmButton.onclick = () => run(confirmButton, async () => {
    await api(`/api/tasks/${task.id}/confirm`, { command: command.id });
    showCalendarChange(task, item.operation);
    dialog.close();
    await renderRoute(true);
  });
  dialog.showModal();
}

function resultView(task, item, index) {
  const command = task.commands.find(entry => entry.current && entry.activity === item.activity_ref);
  const status = command ? commandLabels[command.state] : decisionLabels[item.decision];
  const visualState = ["结果待核查", "等待读回核验", "需要补充"].includes(status) ? " attention" :
    ["处理失败", "未写入"].includes(status) ? " danger" : "";
  const operation = item.operation;
  const change = operation && operationView(task, operation);
  const label = change?.title || (item.clarification_question || (task.items.length > 1 ? "安排 " + (index + 1) : status));
  const head = element("header", "result-head", element("p", "status" + visualState,
    change ? operationLabels[operation.kind] + " · " + status : status), element("h3", "", label));
  const card = element("article", "result", head);
  if (change) {
    card.append(change.time);
    if (change.fields.childNodes.length) card.append(change.fields);
  }
  if (item.reason && item.reason !== label) card.append(element("p", "reason", item.reason));
  if (command?.acknowledged === false) card.append(element("p", "reason", "目标状态已核对，原请求未收到确认。"));

  const candidates = task.records.filter(record => (item.candidate_refs || []).includes(record.event_ref));
  if (candidates.length) {
    const list = element("ul", "candidate-list");
    for (const { fields } of candidates) list.append(element("li", "",
      [fields.title || "无标题", eventTime(fields, task.timezone), fields.location].filter(Boolean).join(" · ")));
    card.append(list);
  }
  const actions = element("div", "result-actions");
  if (operation && !task.closed && (!command || ["PREPARED", "FAILED_BEFORE_WRITE"].includes(command.state))) {
    const action = operation.kind === "CANCEL" ? "审阅取消" : operation.kind === "UPDATE" ? "审阅变更" : "审阅新增";
    actions.append(button(action, "primary", control => run(control, async () => {
      const frozen = await api(`/api/tasks/${task.id}/prepare`, { activity: item.activity_ref });
      if (frozen.state === "SUCCEEDED") await renderRoute(true);
      else askApproval(task, item, frozen, change);
    })));
  }
  if (command && ["OUTCOME_UNKNOWN", "VERIFY_PENDING", "IN_FLIGHT"].includes(command.state)) {
    actions.append(button("核查结果", "primary", control => run(control, async () => {
      const receipt = await api(`/api/tasks/${task.id}/recover`, { command: command.id });
      await renderRoute(true);
      if (receipt.state !== "SUCCEEDED") notify("目前无法核实结果。已保留原操作供后续核查。");
    })));
  }
  if (item.decision === "WAIT") {
    for (const choice of task.choices) actions.append(button(choice, "secondary", control => run(control, async () => {
      await api(`/api/tasks/${task.id}/clarify`, { choice });
      await renderRoute(true);
    })));
  }
  if (actions.childNodes.length) card.append(actions);
  const supported = task.messages.filter(message => (item.evidence_message_ids || []).includes(message.id));
  if (supported.length) {
    const reasons = evidence("查看依据", conversation(supported, task.timezone));
    reasons.className = "result-actions";
    card.append(reasons);
  }
  return card;
}

function renderTask(task) {
  const resultStack = element("section", "result-stack", element("h2", "", "处理结果"));
  for (const [index, item] of task.items.entries()) resultStack.append(resultView(task, item, index));
  if (!task.items.length) {
    const message = task.state === "RUNNING" ? "正在分析，稍后可从最近任务继续查看。" :
      task.model?.http_status === 429 ? "免费模型当前限流，请稍后重新试用。" :
      task.state === "FAILED" ? "这次分析未完成。模拟日历没有执行此分析的新操作。" :
      "没有可显示的结果。可以重新开始。";
    const status = element("p", "quiet", message);
    status.setAttribute("role", "status");
    resultStack.append(status);
  }
  if (task.live && task.live_available && task.items.some(item => item.decision === "WAIT")) {
    resultStack.append(followupForm(task));
  }
  for (const command of task.commands.filter(entry => !entry.current && ["OUTCOME_UNKNOWN", "VERIFY_PENDING", "IN_FLIGHT"].includes(entry.state))) {
    resultStack.append(element("article", "result", element("h3", "", "先前版本 · " + commandLabels[command.state]),
      button("核查先前结果", "secondary", control => run(control, async () => {
        const verified = await api(`/api/tasks/${task.id}/recover`, { command: command.id });
        await renderRoute(true);
        notify(verified.state === "SUCCEEDED" ? "先前结果已核验。" : "目前仍无法核实先前结果。");
      }))));
  }

  const trace = element("ul", "detail-list");
  const states = { COMPLETE: "完整", PARTIAL: "部分", FAILED: "失败" };
  for (const read of task.reads) {
    const label = read.query.kind === "get_events" ? "读取候选详情" : "查询目标时段";
    const span = read.query.range ? " · " + eventTime(read.query.range, task.timezone) : "";
    trace.append(element("li", "", label + " · " + (states[read.read_status] || read.read_status) + span));
  }
  if (!task.reads.length) trace.append(element("li", "", "本次没有日历查询。"));
  if (task.model) trace.prepend(element("li", "", "OpenRouter · " + task.model.calls + " 次模型尝试 · " +
    (task.model.actual_model || "实际模型未返回")));
  for (const command of task.commands) trace.append(element("li", "", "模拟执行 · " + commandLabels[command.state]));
  const context = element("section", "context-stack", element("header", "", element("h2", "", "原始对话"),
    element("p", "quiet", task.live ? "合成输入 · 实时模型分析" : task.scenario.source === "recorded_development_output" ?
      "合成对话 · 历史模型输出回放" : "合成对话 · 预设响应回放")),
    conversation(task.messages, task.timezone), evidence("查看执行记录", trace));
  const taskActions = element("div", "form-actions");
  if (!task.live) {
    taskActions.append(button("重新开始", "text-button", control => run(control, async () => {
      const next = await api(`/api/tasks/${task.id}/restart`, {});
      await route("task", next.id, true);
    })));
  }
  if (task.live_available) {
    taskActions.append(button(task.live ? "新建试用" : "自由试用此场景", "text-button", () => {
      if (!task.live) {
        const source = { kind: "conversation", provider: "threadplan_sandbox", conversation_id: "synthetic-" + task.scenario.id };
        sandboxSeed = { timezone: task.timezone, actor: task.actor, events: task.initial_calendar.map(({ fields }) =>
          Object.fromEntries(Object.entries(fields).filter(([key]) => ["title", "start_at", "end_at", "location", "description"].includes(key)))),
          intake: { schema_version: "0.3", request_id: source.conversation_id + "-input", source, messages: task.messages } };
      }
      route("try");
    }));
  }
  main.replaceChildren(element("div", "task-bar", button(task.live ? "← 最近任务" : "← 场景库", "back",
    () => route(task.live ? "history" : "library")), taskActions),
    headline(task.scenario.title, task.closed ? "该场景已重置，仍可查看处理记录。" : null),
    element("div", "task-layout", resultStack, element("aside", "task-side", calendarPanel(task), context)));
}

async function renderRoute(focus = false) {
  window.clearTimeout(pollTimer);
  const currentVersion = ++routeVersion;
  const params = new URLSearchParams(window.threadplanOffline ? location.hash.slice(1) : location.search);
  const id = params.get("task");
  let tab = ["history", "try"].includes(params.get("view")) ? params.get("view") : "library";
  main.setAttribute("aria-busy", "true");
  try {
    catalog ||= await api("/api/scenarios");
    document.querySelector("#sandbox-nav").hidden = !catalog.live_enabled;
    document.querySelector(".mode").textContent = catalog.offline ? "离线回放" : "模拟试用";
    if (id) {
      const task = await api("/api/tasks/" + encodeURIComponent(id));
      if (currentVersion !== routeVersion) return;
      renderTask(task);
      if (task.live) tab = "try";
      if (task.state === "RUNNING") pollTimer = window.setTimeout(() => renderRoute(), 1500);
    }
    else if (tab === "history") await historyView();
    else if (tab === "try") sandboxForm();
    else library();
    for (const link of document.querySelectorAll("nav [data-nav]")) {
      if (link.dataset.nav === tab) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    if (focus) main.focus({ preventScroll: true });
  } catch (error) {
    main.replaceChildren(headline("暂时无法打开"), element("p", "quiet", error.message),
      button("返回场景库", "text-button", () => route("library")));
  } finally {
    main.setAttribute("aria-busy", "false");
  }
}

for (const link of document.querySelectorAll("[data-nav]")) {
  link.addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    route(link.dataset.nav);
  });
}
window.addEventListener("popstate", () => renderRoute());
renderRoute();
