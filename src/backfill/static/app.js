const state = {
  tasks: [],
  providers: [],
  runs: [],
  control: null,
};

const $ = (selector) => document.querySelector(selector);
const taskDialog = $("#task-dialog");
const runDialog = $("#run-dialog");
let toastTimer;

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (method !== "GET") {
    headers.set("X-Backfill-Request", "dashboard");
  }
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      // The status text is enough when a proxy returned non-JSON.
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 3800);
}

function relativeTime(value) {
  if (!value) return "unknown";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (absolute < 60) return formatter.format(seconds, "second");
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

function providerColor(provider) {
  return provider === "codex" ? "#167c62" : "#d5502f";
}

function renderProviders() {
  const grid = $("#provider-grid");
  if (!state.providers.length) {
    grid.innerHTML = '<div class="loading-card">No provider snapshots yet. Refresh signals to probe Devbox.</div>';
    return;
  }
  grid.innerHTML = state.providers
    .sort((a, b) => a.provider_id.localeCompare(b.provider_id))
    .map((provider) => {
      const windows = provider.windows
        .slice(0, 4)
        .map(
          (window) => `
            <div class="quota-window">
              <div class="quota-ring" style="--remaining:${window.remaining_percent}">
                <b>${Math.round(window.remaining_percent)}%</b>
              </div>
              <div class="quota-label">
                <strong>${escapeHtml(window.name)}</strong>
                <span>${window.resets_at ? `Resets ${relativeTime(window.resets_at)}` : "Reset unknown"}</span>
              </div>
            </div>`,
        )
        .join("");
      return `
        <article class="provider-card" data-ready="${provider.ready}" style="--provider-color:${providerColor(provider.provider_id)}">
          <div class="provider-top">
            <div>
              <div class="provider-name">${escapeHtml(provider.provider_id)}</div>
              <div class="provider-meta">${escapeHtml(provider.plan || "plan unknown")} · ${escapeHtml(provider.source)}${provider.stale ? " · stale" : ""}</div>
            </div>
            <span class="signal-badge ${provider.ready ? "" : "down"}">${provider.ready ? "line open" : "no signal"}</span>
          </div>
          ${windows ? `<div class="window-grid">${windows}</div>` : ""}
          ${provider.error ? `<p class="provider-error">${escapeHtml(provider.error)}</p>` : ""}
        </article>`;
    })
    .join("");
}

function renderTasks() {
  const list = $("#task-list");
  $("#queue-count").textContent = `${state.tasks.length} ${state.tasks.length === 1 ? "job" : "jobs"}`;
  if (!state.tasks.length) {
    list.innerHTML = '<div class="empty-state"><span>∅</span><p>No work is waiting in the yard.</p></div>';
    return;
  }
  list.innerHTML = state.tasks
    .map((task) => {
      const active = task.status === "running";
      const recoverable = ["blocked", "failed", "paused", "cancelled"].includes(task.status);
      const actions = active
        ? `<button class="mini-button" data-task-action="pause" data-task-id="${task.id}">Pause</button>
           <button class="mini-button" data-task-action="cancel" data-task-id="${task.id}">Cancel</button>`
        : recoverable
          ? `<button class="mini-button" data-task-action="retry" data-task-id="${task.id}">Requeue</button>`
          : task.status === "queued"
            ? `<button class="mini-button" data-task-action="pause" data-task-id="${task.id}">Hold</button>`
            : "";
      return `
        <article class="task-card">
          <div class="priority-stamp">${task.priority}</div>
          <div>
            <h4 class="task-title">${escapeHtml(task.title)}</h4>
            <div class="task-meta">
              <span class="status-pill" data-status="${task.status}">${escapeHtml(task.status)}</span>
              ${escapeHtml(task.preferred_provider)} · est. ${task.estimated_cost_percent}% · ${task.max_runtime_minutes} min ceiling
            </div>
            ${task.status_reason ? `<p class="task-reason">${escapeHtml(task.status_reason)}</p>` : ""}
          </div>
          <div class="task-actions">${actions}</div>
        </article>`;
    })
    .join("");
}

function renderRuns() {
  const list = $("#run-list");
  if (!state.runs.length) {
    list.innerHTML = '<div class="empty-state compact"><span>—</span><p>No runs recorded.</p></div>';
    return;
  }
  const taskMap = new Map(state.tasks.map((task) => [task.id, task]));
  list.innerHTML = state.runs
    .slice(0, 12)
    .map((run) => {
      const task = taskMap.get(run.task_id);
      return `
        <article class="run-card" data-run-id="${run.id}">
          <div class="run-top">
            <div class="run-title">${escapeHtml(task?.title || "Unknown job")}</div>
            <span class="status-pill" data-status="${run.status}">${escapeHtml(run.status)}</span>
          </div>
          <div class="run-time">${escapeHtml(run.provider)} · started ${relativeTime(run.started_at)}</div>
          ${run.summary ? `<p class="run-summary">${escapeHtml(run.summary)}</p>` : ""}
        </article>`;
    })
    .join("");
}

function renderControl() {
  if (!state.control) return;
  const enabled = state.control.scheduler_enabled;
  const paused = state.control.paused;
  $("#scheduler-state").textContent = !enabled ? "Disabled" : paused ? "Paused" : "Dispatching";
  $("#running-count").textContent = state.control.running_count;
  $("#pause-button").textContent = paused ? "Resume" : "Pause";
  $("#pause-button").disabled = !enabled;
  $("#tick-button").disabled = !enabled || paused;
}

async function loadAll({ quiet = false } = {}) {
  try {
    const [me, tasks, providers, runs, control] = await Promise.all([
      api("/api/me"),
      api("/api/tasks/"),
      api("/api/providers/"),
      api("/api/runs/"),
      api("/api/control"),
    ]);
    $("#owner-email").textContent = me.login;
    Object.assign(state, { tasks, providers, runs, control });
    renderProviders();
    renderTasks();
    renderRuns();
    renderControl();
  } catch (error) {
    if (!quiet) toast(error.message);
    $("#owner-email").textContent = "Access unavailable";
  }
}

async function mutate(path, options, message) {
  try {
    const result = await api(path, options);
    if (message) toast(message);
    await loadAll({ quiet: true });
    return result;
  } catch (error) {
    toast(error.message);
    throw error;
  }
}

$("#new-task-button").addEventListener("click", () => taskDialog.showModal());
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const payload = Object.fromEntries(form.entries());
  for (const key of ["priority", "estimated_cost_percent", "max_runtime_minutes"]) {
    payload[key] = Number(payload[key]);
  }
  if (!payload.branch_name) delete payload.branch_name;
  try {
    await mutate(
      "/api/tasks/",
      { method: "POST", body: JSON.stringify(payload) },
      "Job added to the priority ledger.",
    );
    formElement.reset();
    taskDialog.close();
  } catch {
    // Keep the form open so the input is not lost.
  }
});

$("#task-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-task-action]");
  if (!button) return;
  await mutate(
    `/api/tasks/${button.dataset.taskId}/actions/${button.dataset.taskAction}`,
    { method: "POST" },
    `Job ${button.dataset.taskAction} requested.`,
  );
});

$("#refresh-providers").addEventListener("click", async () => {
  await mutate("/api/providers/refresh", { method: "POST" }, "Provider signals refreshed.");
});

$("#pause-button").addEventListener("click", async () => {
  const action = state.control?.paused ? "resume" : "pause";
  await mutate(`/api/control/${action}`, { method: "POST" }, `Scheduler ${action}d.`);
});

$("#tick-button").addEventListener("click", async () => {
  const result = await mutate("/api/control/tick", { method: "POST" });
  toast(result.reason || `Dispatch decision: ${result.decision}`);
});

$("#run-list").addEventListener("click", async (event) => {
  const card = event.target.closest("[data-run-id]");
  if (!card) return;
  const run = state.runs.find((candidate) => candidate.id === card.dataset.runId);
  $("#run-dialog-title").textContent = run ? `${run.provider} / ${run.status}` : "Movement log";
  $("#run-events").textContent = "Loading…";
  runDialog.showModal();
  try {
    const events = await api(`/api/runs/${card.dataset.runId}/events`);
    $("#run-events").textContent =
      events.map((item) => `[${item.kind}] ${item.message}`).join("\n\n") || "No transcript events.";
  } catch (error) {
    $("#run-events").textContent = error.message;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key.toLowerCase() === "n" && !event.metaKey && !event.ctrlKey && !event.altKey) {
    const tag = document.activeElement?.tagName;
    if (!["INPUT", "TEXTAREA", "SELECT"].includes(tag)) taskDialog.showModal();
  }
});

loadAll();
setInterval(() => loadAll({ quiet: true }), 15_000);
