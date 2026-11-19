"use strict";

function createOfflineReplay(bundle) {
  const scenes = new Map(bundle.scenes.map(scene => [scene.initial.scenario.id, scene]));
  const storageKey = "threadplan-replay-" + bundle.version;
  let tasks = new Map();
  try {
    tasks = new Map(JSON.parse(sessionStorage.getItem(storageKey) || "[]")
      .filter(([, task]) => scenes.has(task.scenario)));
  } catch {
    // Some browsers disable storage for file URLs. The running tab still works.
  }

  function persist() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify([...tasks]));
    } catch {
      // Keep the current tab usable when browser storage is unavailable.
    }
  }

  function phase(task) {
    const scene = scenes.get(task.scenario);
    return task.choice ? scene.choices[task.choice] : scene.initial;
  }

  function view(task) {
    const { actions, ...base } = phase(task);
    return structuredClone({ ...base, id: task.id, closed: task.closed,
      phase: task.choice ? 1 : 0, calendar: task.calendar, commands: task.commands });
  }

  function create(scenario) {
    if (!scenes.has(scenario)) throw new Error("not_found");
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, "0")).join("");
    const task = { id, scenario, choice: null, closed: false, commands: [],
      calendar: structuredClone(scenes.get(scenario).initial.initial_calendar) };
    tasks.set(id, task);
    persist();
    return view(task);
  }

  function prepare(task, activity) {
    if (task.closed) throw new Error("approval_source_changed");
    if (!Object.hasOwn(phase(task).actions, activity)) throw new Error("not_found");
    let receipt = task.commands.find(command => command.activity === activity);
    if (!receipt) {
      receipt = { id: activity, activity, state: "PREPARED", current: true };
      task.commands.push(receipt);
      persist();
    }
    return structuredClone(receipt);
  }

  function execute(task, id, recover) {
    const receipt = task.commands.find(command => command.id === id);
    if (!receipt) throw new Error("not_found");
    const trace = phase(task).actions[receipt.activity];
    if (recover && trace.recover &&
        ["OUTCOME_UNKNOWN", "VERIFY_PENDING", "IN_FLIGHT"].includes(receipt.state)) {
      Object.assign(receipt, trace.recover);
    } else if (!recover && receipt.state === "PREPARED") {
      if (task.closed) throw new Error("approval_source_changed");
      const events = new Map(task.calendar.map(event => [event.id, event]));
      for (const id of trace.remove) events.delete(id);
      for (const event of trace.upsert) events.set(event.id, structuredClone(event));
      task.calendar = [...events.values()];
      Object.assign(receipt, trace.confirm);
    }
    persist();
    return structuredClone(receipt);
  }

  return {
    request(path, data) {
      if (path === "/api/scenarios" && data === undefined) {
        return { cases: bundle.scenes.map(scene => scene.initial.scenario), offline: true, live_enabled: false };
      }
      if (path === "/api/tasks") {
        if (data) return create(data.scenario);
        return [...tasks.values()].reverse().map(task => ({
          id: task.id, title: phase(task).scenario.title, closed: task.closed,
        }));
      }
      const match = /^\/api\/tasks\/([a-f0-9]{32})(?:\/([a-z]+))?$/.exec(path);
      const task = match && tasks.get(match[1]);
      if (!task) throw new Error("not_found");
      if (data === undefined && !match[2]) return view(task);
      if (!data) throw new Error("not_found");
      if (match[2] === "prepare") return prepare(task, data.activity);
      if (match[2] === "confirm" || match[2] === "recover") {
        return execute(task, data.command, match[2] === "recover");
      }
      if (match[2] === "clarify") {
        const choices = scenes.get(task.scenario).choices;
        if (task.closed || task.choice || !Object.hasOwn(choices, data.choice)) {
          throw new Error("approval_source_changed");
        }
        task.choice = data.choice;
        task.calendar = structuredClone(phase(task).initial_calendar);
        persist();
        return view(task);
      }
      if (match[2] === "restart") {
        task.closed = true;
        return create(task.scenario);
      }
      throw new Error("not_found");
    },
  };
}
