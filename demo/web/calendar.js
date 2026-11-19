"use strict";

const calendarSelections = new Map();
const calendarFormatters = new Map();
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
const minutesPerDay = 24 * 60;
const hourHeight = 61;

function calendarParts(value, zone) {
  const instant = new Date(value);
  if (!value || Number.isNaN(instant.valueOf())) return null;
  if (!calendarFormatters.has(zone)) {
    calendarFormatters.set(zone, new Intl.DateTimeFormat("en-CA", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }));
  }
  const part = Object.fromEntries(calendarFormatters.get(zone).formatToParts(instant)
    .filter(item => item.type !== "literal").map(item => [item.type, item.value]));
  return { day: [part.year, part.month, part.day].join("-"), minute: +part.hour * 60 + +part.minute };
}

function shiftDay(day, days) {
  const instant = new Date(day + "T00:00:00Z");
  instant.setUTCDate(instant.getUTCDate() + days);
  return instant.toISOString().slice(0, 10);
}

function calendarWeek(day) {
  const weekday = new Date(day + "T00:00:00Z").getUTCDay();
  const monday = shiftDay(day, -((weekday + 6) % 7));
  return Array.from({ length: 7 }, (_, index) => shiftDay(monday, index));
}

function dayLabel(day) {
  return +day.slice(5, 7) + "月" + +day.slice(8) + "日";
}

function operationCalendarDay(task, operation) {
  const target = task.records.find(record => record.event_ref === operation.event_ref);
  return calendarParts(operation.changes?.start_at || target?.fields.start_at, task.timezone)?.day;
}

function calendarSelection(task) {
  if (!calendarSelections.has(task.id)) {
    const operation = task.items.find(item => item.operation)?.operation;
    const day = (operation && operationCalendarDay(task, operation)) ||
      calendarParts(task.calendar[0]?.fields.start_at, task.timezone)?.day ||
      calendarParts(task.calendar_anchor, task.timezone).day;
    calendarSelections.set(task.id, { day, before: false });
  }
  return calendarSelections.get(task.id);
}

function showCalendarChange(task, operation) {
  const view = calendarSelection(task);
  view.before = false;
  view.day = operationCalendarDay(task, operation) || view.day;
}

function daySegments(events, day, zone) {
  const segments = [];
  for (const event of events) {
    const begin = calendarParts(event.fields.start_at, zone);
    const end = calendarParts(event.fields.end_at, zone);
    if (!begin || !end || day < begin.day || day > end.day || (day === end.day && end.minute === 0)) continue;
    const start = begin.day === day ? begin.minute : 0;
    let finish = end.day === day ? end.minute : minutesPerDay;
    // At a repeated local hour the wall-clock end can equal or precede the start.
    if (finish <= start) {
      const elapsed = (Date.parse(event.fields.end_at) - Date.parse(event.fields.start_at)) / 60000;
      finish = Math.min(minutesPerDay, start + elapsed);
    }
    segments.push({ event, start, finish });
  }
  return segments.sort((a, b) => a.start - b.start || b.finish - a.finish);
}

function eventChanges(task) {
  const before = new Map(task.initial_calendar.map(event => [event.id, event.fields]));
  const after = new Map(task.calendar.map(event => [event.id, event.fields]));
  const changes = new Map();
  for (const [id, fields] of after) {
    const prior = before.get(id);
    if (!prior) changes.set(id, "新增");
    else if (["title", "location", "description", "start_at", "end_at"].some(key => fields[key] !== prior[key])) {
      changes.set(id, "已调整");
    }
  }
  for (const id of before.keys()) if (!after.has(id)) changes.set(id, "当前已移出");
  return changes;
}

function calendarTimeline(segments, comparison, zone, changes) {
  if (!segments.length) return element("p", "calendar-empty", "这一天暂无日程");
  const timeline = [...segments, ...comparison];
  const firstHour = Math.max(0, Math.floor(Math.min(...timeline.map(item => item.start)) / 60) - 1);
  const lastEvent = Math.max(...timeline.map(item => item.finish));
  const lastHour = Math.min(24, Math.max(firstHour + 4, Math.ceil(lastEvent / 60) + 1));
  const canvas = element("div", "calendar-hours");
  canvas.style.height = (lastHour - firstHour) * hourHeight + "px";
  for (let hour = firstHour; hour <= lastHour; hour++) {
    const tick = element("div", "calendar-tick", element("span", "", String(hour).padStart(2, "0") + ":00"));
    tick.style.top = (hour - firstHour) * hourHeight + "px";
    canvas.append(tick);
  }

  const laneEnds = [];
  for (const segment of segments) {
    const lane = laneEnds.findIndex(end => end <= segment.start);
    segment.lane = lane < 0 ? laneEnds.length : lane;
    laneEnds[segment.lane] = Math.max(segment.finish, segment.start + 42 * 60 / hourHeight);
  }
  const layer = element("div", "calendar-events");
  for (const { event, start, finish, lane } of segments) {
    const title = event.fields.title || "无标题日程";
    const when = eventTime(event.fields, zone);
    const change = changes.get(event.id);
    const item = element("div", "calendar-event" + (change ? " calendar-event-changed" : ""),
      element("strong", "", title),
      element("span", "", when),
      change && element("span", "calendar-change", change));
    item.title = [title, when, event.fields.location, change].filter(Boolean).join(" · ");
    item.style.top = (start / 60 - firstHour) * hourHeight + "px";
    item.style.height = Math.max(42, (finish - start) / 60 * hourHeight) + "px";
    item.style.left = `calc(${lane * 100 / laneEnds.length}% + 2px)`;
    item.style.width = `calc(${100 / laneEnds.length}% - 5px)`;
    layer.append(item);
  }
  canvas.append(layer);
  return element("div", "calendar-scroll", canvas);
}

function calendarPanel(task) {
  const selection = calendarSelection(task);
  const panel = element("section", "calendar-panel");
  panel.setAttribute("aria-label", "模拟日历");
  const changes = eventChanges(task);
  const unresolved = task.commands.some(command =>
    ["OUTCOME_UNKNOWN", "VERIFY_PENDING", "IN_FLIGHT"].includes(command.state));

  function render(focusKey) {
    const week = calendarWeek(selection.day);
    const events = selection.before ? task.initial_calendar : task.calendar;
    const alternate = selection.before ? task.calendar : task.initial_calendar;
    const navigation = [
      button("‹", "calendar-nav", () => { selection.day = shiftDay(selection.day, -7); render("previous"); }),
      button("›", "calendar-nav", () => { selection.day = shiftDay(selection.day, 7); render("next"); }),
    ];
    navigation[0].setAttribute("aria-label", "上一周");
    navigation[1].setAttribute("aria-label", "下一周");
    navigation[0].dataset.calendarControl = "previous";
    navigation[1].dataset.calendarControl = "next";
    const previous = button("执行前", "calendar-toggle", () => { selection.before = true; render("before"); });
    const latest = button("当前", "calendar-toggle", () => { selection.before = false; render("current"); });
    previous.setAttribute("aria-pressed", String(selection.before));
    latest.setAttribute("aria-pressed", String(!selection.before));
    previous.dataset.calendarControl = "before";
    latest.dataset.calendarControl = "current";
    const days = element("div", "calendar-week");
    for (const [index, day] of week.entries()) {
      const hasEvents = daySegments(events, day, task.timezone).length > 0;
      const control = button("", "calendar-day", () => { selection.day = day; render(day); });
      control.dataset.calendarControl = day;
      control.append(element("span", "", weekdays[index]), element("strong", "", +day.slice(8)),
        element("span", hasEvents ? "calendar-dot visible" : "calendar-dot"));
      control.setAttribute("aria-pressed", String(day === selection.day));
      control.setAttribute("aria-label", dayLabel(day) + (hasEvents ? " 有日程" : ""));
      days.append(control);
    }
    const dayEvents = daySegments(events, selection.day, task.timezone);
    const comparison = daySegments(alternate, selection.day, task.timezone);
    panel.replaceChildren(
      element("div", "calendar-heading", element("h2", "", "模拟日历"),
        element("div", "calendar-switch", previous, latest)),
      element("div", "calendar-week-heading", element("p", "", week[0].slice(0, 4) + "年 " +
        dayLabel(week[0]) + "–" + dayLabel(week[6])), element("div", "calendar-nav-group", ...navigation)),
      days,
      element("div", "calendar-date", element("strong", "", dayLabel(selection.day)),
        element("span", "quiet", unresolved && !selection.before ? "快照 · 执行结果待核查" : task.timezone)),
      calendarTimeline(dayEvents, comparison, task.timezone, changes)
    );
    if (focusKey) {
      [...panel.querySelectorAll("[data-calendar-control]")]
        .find(node => node.dataset.calendarControl === focusKey)?.focus();
    }
  }

  render();
  return panel;
}
