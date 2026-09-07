const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const date = (v) =>
  v
    ? new Date(typeof v === "number" ? v * 1000 : v).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "";
const localDate = (v) => {
  const d = new Date(typeof v === "number" ? v * 1000 : v);
  return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const names = {
  queued: "Queued",
  running: "Running",
  waiting: "Waiting for capacity",
  review: "Ready for review",
  paused: "Paused",
  done: "Completed",
  failed: "Needs attention",
  cancelled: "Cancelled",
};
let data = null,
  view = "tasks",
  filter = "all",
  projectFilter = "",
  editingTask = null,
  editingProject = null,
  pauseTarget = null,
  detailId = null,
  currentTask = null,
  refreshing = false;
async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      $("#content").hidden = true;
      $("#login").hidden = false;
    }
    throw Error(
      value.error ||
        (value.detail
          ? "Check the fields and try again."
          : "Could not save. Try again."),
    );
  }
  return value;
}
let noticeTimer;
function notice(message, error = false) {
  const n = $("#notice");
  clearTimeout(noticeTimer);
  n.textContent = message;
  n.classList.toggle("error", error);
  n.hidden = !message;
  if (message && !error)
    noticeTimer = setTimeout(() => {
      n.hidden = true;
    }, 6000);
}
function openDialog(id) {
  const d = $(id);
  $(".form-error", d)?.replaceChildren();
  d.showModal();
  document.body.classList.add("modal-open");
}
function closeDialog(d) {
  d.close();
  if (!document.querySelector("dialog[open]"))
    document.body.classList.remove("modal-open");
  if (d.id === "detail-dialog") detailId = null;
}
$$(".close-dialog").forEach((b) =>
  b.addEventListener("click", () => closeDialog(b.closest("dialog"))),
);
$$("dialog").forEach((d) => {
  d.addEventListener("close", () => {
    if (!document.querySelector("dialog[open]"))
      document.body.classList.remove("modal-open");
    if (d.id === "detail-dialog") detailId = null;
  });
  d.addEventListener("click", (e) => {
    if (e.target === d) {
      const r = d.getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        closeDialog(d);
    }
  });
});
function projectName(id) {
  return data?.projects.find((p) => p.id === id)?.name || "";
}
function accountName(id) {
  return id === "codex" ? "Codex" : id === "claude" ? "Claude" : "Automatic";
}
function isPaused() {
  const p = data.preferences;
  return p.paused || (p.paused_until && new Date(p.paused_until) > new Date());
}
function navigate(next, project = "") {
  view = next;
  projectFilter = project;
  filter = "all";
  location.hash = project ? "project=" + project : next;
  render();
}
$$("[data-view]").forEach((b) => (b.onclick = () => navigate(b.dataset.view)));
$$("[data-filter]").forEach(
  (b) =>
    (b.onclick = () => {
      filter = b.dataset.filter;
      renderList();
    }),
);
$("#search").oninput = renderList;
function render() {
  if (!data) return;
  $("#host").textContent = data.host;
  $("#content").hidden = false;
  $("#login").hidden = true;
  $("#task-count").textContent = data.tasks.filter(
    (t) => !["done", "cancelled", "review"].includes(t.state),
  ).length;
  $("#review-count").textContent = data.tasks.filter(
    (t) => t.state === "review",
  ).length;
  $$("[data-view]").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view && !projectFilter),
  );
  $("#project-links").innerHTML = data.projects
    .map(
      (p) =>
        `<button class="project-link ${projectFilter === p.id ? "active" : ""}" data-project="${esc(p.id)}">${esc(p.name)}</button>`,
    )
    .join("");
  $$("[data-project]").forEach(
    (b) => (b.onclick = () => navigate("tasks", b.dataset.project)),
  );
  const titles = {
    tasks: [
      "WORKSPACE",
      "Tasks",
      "Queued work runs when capacity is available.",
    ],
    review: [
      "WORKSPACE",
      "Ready for review",
      "Approve a result or request changes.",
    ],
    done: ["WORKSPACE", "Completed", "Your approved results."],
    settings: ["WORKSPACE", "Settings", "Capacity and connected accounts."],
  };
  const title = projectFilter
    ? [
        "PROJECT",
        projectName(projectFilter),
        "Related work, with a shared allowance.",
      ]
    : titles[view] || titles.tasks;
  $("#eyebrow").textContent = title[0];
  $("#page-title").textContent = title[1];
  $("#page-description").textContent = title[2];
  $("#breadcrumb").textContent = projectFilter
    ? "Projects / " + projectName(projectFilter)
    : "Workspace / " +
      {
        tasks: "Tasks",
        review: "Review",
        done: "Completed",
        settings: "Settings",
      }[view];
  $("#task-view").hidden = view === "settings";
  $("#settings-view").hidden = view !== "settings";
  $(".tabs").hidden = view !== "tasks";
  $("#pause-all").textContent = isPaused() ? "Resume work" : "Pause work";
  $("#capacity").innerHTML = data.accounts
    .map(
      (a) =>
        `<div class="account-mini" title="${a.connected ? "Available capacity" : "Fresh quota readings are unavailable"}"><div class="account-name"><span class="account-glyph" aria-hidden="true">${a.provider === "claude" ? "✳" : "›_"}</span>${accountName(a.provider)}</div>${a.connected ? `<div class="account-values">${a.windows.map((w) => `<div title="Resets ${esc(date(w.resets_at))}"><strong>${Math.floor(w.remaining)}<span>%</span></strong><span>${esc(w.label)} left</span></div>`).join("")}</div>` : '<span class="account-unavailable">Capacity unavailable</span>'}</div>`,
    )
    .join("");
  const running = data.tasks.filter((t) => t.state === "running").length;
  $("#footer-status").textContent = isPaused()
    ? data.preferences.paused_until
      ? "Paused until " + date(data.preferences.paused_until)
      : "Background work paused"
    : running
      ? "Working on " + running + " task"
      : data.execution_enabled
        ? "Ready when capacity is available"
        : "Execution is disabled on this host";
  renderList();
  renderSettings();
}
function renderList() {
  if (!data) return;
  $$("[data-filter]").forEach((b) =>
    b.classList.toggle("active", b.dataset.filter === filter),
  );
  const q = $("#search").value.toLowerCase();
  let tasks = data.tasks.filter((t) =>
    view === "review"
      ? t.state === "review"
      : view === "done"
        ? ["done", "cancelled"].includes(t.state)
        : !["done", "cancelled", "review"].includes(t.state),
  );
  if (projectFilter)
    tasks = data.tasks.filter(
      (t) =>
        t.project === projectFilter && !["done", "cancelled"].includes(t.state),
    );
  if (filter !== "all")
    tasks = tasks.filter((t) =>
      filter === "queued"
        ? ["queued", "waiting"].includes(t.state)
        : t.state === filter,
    );
  tasks = tasks.filter((t) =>
    (t.title + " " + projectName(t.project)).toLowerCase().includes(q),
  );
  const order = {
    running: 0,
    failed: 1,
    review: 2,
    queued: 3,
    waiting: 4,
    paused: 5,
  };
  tasks.sort(
    (a, b) =>
      (order[a.state] ?? 9) - (order[b.state] ?? 9) || b.created - a.created,
  );
  if (!tasks.length) {
    const empty =
      view === "review"
        ? [
            "✓",
            "Nothing waiting on you.",
            "Finished work will arrive here with its result and evidence.",
          ]
        : view === "done"
          ? [
              "↗",
              "A little more room in your day.",
              "Approved results will stay here for later.",
            ]
          : filter !== "all" || q
            ? ["⌕", "No matching tasks.", "Try another filter or search."]
            : [
                "↗",
                "What can we take off your plate?",
                "Give it a clear outcome. Backfill handles the timing and brings the result back to you.",
              ];
    $("#task-list").innerHTML =
      `<div class="empty"><div class="empty-symbol" aria-hidden="true">${empty[0]}</div><h2>${empty[1]}</h2><p>${empty[2]}</p>${view === "tasks" && filter === "all" && !q ? '<button class="primary" id="empty-new">Create your first task ↗</button><div class="suggestions"><button class="suggestion" data-template="history">Summarize a project</button><button class="suggestion" data-template="review">Review recent changes</button><button class="suggestion" data-template="research">Research a question</button></div>' : ""}</div>`;
    $("#empty-new")?.addEventListener("click", () => newTask());
    $$("[data-template]").forEach(
      (b) => (b.onclick = () => newTask(b.dataset.template)),
    );
    return;
  }
  $("#task-list").innerHTML = tasks
    .map(
      (t) =>
        `<div class="task-row ${esc(t.state)}" role="button" tabindex="0" data-task="${esc(t.id)}" aria-label="Open ${esc(t.title)}"><div class="task-symbol" aria-hidden="true">${{ running: "↻", review: "✓", done: "✓", failed: "!", paused: "Ⅱ" }[t.state] || "↗"}</div><div class="task-main"><div class="task-title">${esc(t.title)}</div><div class="task-meta">${t.project ? `<span>${esc(projectName(t.project))}</span><span>·</span>` : ""}<span>${t.selected_provider ? accountName(t.selected_provider) : t.provider === "auto" ? "Automatic account" : accountName(t.provider)}</span>${t.priority === "high" ? "<span>· High priority</span>" : ""}${t.schedule !== "once" ? `<span>· ${t.schedule === "daily" ? "Daily" : "Weekly"}</span>` : ""}${t.due > Date.now() / 1000 ? `<span>· ${esc(date(t.due))}</span>` : ""}</div></div><span class="badge ${esc(t.state)}">${esc(names[t.state])}</span><span class="task-arrow" aria-hidden="true">↗</span></div>`,
    )
    .join("");
  $$("[data-task]").forEach((el) => {
    el.onclick = () => showTask(el.dataset.task);
    el.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        showTask(el.dataset.task);
      }
    };
  });
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    data = await api("/v2/overview");
    render();
    if (
      detailId &&
      !$("#feedback")?.value &&
      document.activeElement?.tagName !== "TEXTAREA"
    ) {
      const task = await api("/v2/tasks/" + detailId);
      if (
        task.state !== currentTask?.state ||
        task.output !== currentTask?.output
      )
        renderDetail(task);
    }
  } catch (e) {
    notice(e.message, true);
  } finally {
    refreshing = false;
  }
}
const templates = {
  history: [
    "Summarize this project",
    "Read the project files and summarize what it does, its main components, and the work represented here. Cite the files you used. Finish with open questions.",
  ],
  review: [
    "Review recent changes",
    "Inspect this project for a small set of concrete correctness issues. Explain each finding with file references and a way to verify it. Do not change files.",
  ],
  research: [
    "Research a question",
    "Investigate [question]. Use primary sources and provide a concise answer with evidence, links, and remaining uncertainty.",
  ],
};
function newTask(template) {
  editingTask = null;
  $("#task-form").reset();
  $("#task-dialog-title").textContent = "What needs doing?";
  $("#create-task").textContent = "Queue task ↗";
  $("#project").innerHTML =
    '<option value="">No project</option>' +
    data.projects
      .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
      .join("");
  $("#project").value = projectFilter;
  $(".task-options").open = false;
  if (template) {
    $("#title").value = templates[template][0];
    $("#instructions").value = templates[template][1];
  }
  openDialog("#task-dialog");
}
$("#new-task").onclick = () => {
  if (data) newTask();
};
$("#task-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const body = {
    title: $("#title").value,
    instructions: $("#instructions").value,
    project: $("#project").value || null,
    priority: $("#priority").value,
    provider: $("#provider").value,
    allowance: Number($("#allowance").value),
    schedule: $("#schedule").value,
    scheduled_at: $("#scheduled_at").value
      ? new Date($("#scheduled_at").value).toISOString()
      : null,
    folder: $("#folder").value,
    access: $("#access").value,
    source_url: $("#source_url").value,
  };
  const submit = $("button[type=submit]", form);
  submit.disabled = true;
  try {
    await api(
      editingTask ? "/v2/tasks/" + editingTask : "/v2/tasks",
      editingTask ? "PUT" : "POST",
      body,
    );
    closeDialog($("#task-dialog"));
    notice(
      editingTask
        ? "Task updated."
        : "Task queued. We’ll pick it up when capacity is available.",
    );
    navigate("tasks", body.project || "");
    await refresh();
  } catch (err) {
    $(".form-error", form).textContent = err.message;
  } finally {
    submit.disabled = false;
  }
};
function projectDialog(project) {
  editingProject = project?.id || null;
  $("#project-form").reset();
  $("#project-dialog h2").textContent = project
    ? "Project settings"
    : "New project";
  $("#project-name").value = project?.name || "";
  $("#project-folder").value = project?.folder || "";
  $("#project-allowance").value = project?.allowance || 10;
  openDialog("#project-dialog");
}
$("#new-project").onclick = () => {
  if (data) projectDialog();
};
$("#project-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const button = $("button[type=submit]", form);
  button.disabled = true;
  try {
    const value = await api(
      "/v2/projects" + (editingProject ? "/" + editingProject : ""),
      editingProject ? "PUT" : "POST",
      {
        name: $("#project-name").value,
        folder: $("#project-folder").value,
        allowance: Number($("#project-allowance").value),
      },
    );
    closeDialog($("#project-dialog"));
    await refresh();
    if (!editingProject) navigate("tasks", value.id);
    notice("Project saved.");
  } catch (err) {
    $(".form-error", form).textContent = err.message;
  } finally {
    button.disabled = false;
  }
};
function markdown(text) {
  let code = false;
  let result = "";
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      result += code ? "</code></pre>" : "<pre><code>";
      code = !code;
      continue;
    }
    if (code) {
      result += esc(line) + "\n";
      continue;
    }
    const heading = line.match(/^(#{1,3}) (.*)/);
    if (heading) {
      result += `<h3>${esc(heading[2])}</h3>`;
      continue;
    }
    const formatted = esc(line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    result += line ? `<p>${formatted}</p>` : "";
  }
  return result + (code ? "</code></pre>" : "");
}
async function showTask(id) {
  try {
    const task = await api("/v2/tasks/" + id);
    detailId = id;
    renderDetail(task);
    openDialog("#detail-dialog");
  } catch (e) {
    notice(e.message, true);
  }
}
function renderDetail(t) {
  currentTask = t;
  const active = ["queued", "running", "waiting"].includes(t.state);
  $("#detail-content").innerHTML =
    `<div class="detail-header"><div><span class="badge ${esc(t.state)}">${esc(names[t.state])}</span><h2>${esc(t.title)}</h2></div><button class="icon" id="detail-close" aria-label="Close">×</button></div><div class="detail-meta"><span>${esc(projectName(t.project) || "Independent task")}</span><span>${accountName(t.selected_provider || t.provider)}</span><span>${t.allowance}% allowance</span>${t.schedule !== "once" ? `<span>Repeats ${esc(t.schedule)}</span>` : ""}</div>${t.reason ? `<p class="reason">${esc(t.reason)}</p>` : ""}${t.paused_until ? `<p class="reason">Resumes ${esc(date(t.paused_until))}</p>` : ""}<details class="detail-section" ${!t.output ? "open" : ""}><summary>Instructions</summary><div class="instructions">${esc(t.instructions)}</div>${t.folder ? `<p class="detail-meta">${esc(t.folder)}</p>` : ""}${t.source_url ? `<a href="${esc(t.source_url)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>` : ""}</details><div class="detail-section"><h3>${t.state === "running" ? "Work in progress" : t.state === "review" ? "Result" : "Latest result"}</h3>${t.output ? `<div class="result">${markdown(t.output)}</div>` : `<p class="instructions">${t.state === "running" ? "The executor is working. Its result will appear here." : "The result will appear here after this task runs."}</p>`}</div>${t.state === "review" ? '<div class="feedback"><label>Want something changed?<textarea id="feedback" rows="3" placeholder="Describe the change. The next pass will include this result and your feedback."></textarea></label></div>' : ""}<div class="detail-actions"><div>${t.output ? `<a class="quiet" href="/v2/tasks/${esc(t.id)}/result">Download result</a>` : ""}${!["running", "review", "done", "cancelled"].includes(t.state) ? '<button class="quiet" id="edit-task">Edit task</button>' : ""}</div><div>${t.state === "review" ? '<button class="secondary" data-action="revise">Request changes</button><button class="primary" data-action="approve">Approve result ✓</button>' : active ? '<button class="quiet" data-action="cancel">Cancel task</button><button class="secondary" id="pause-task">Pause task</button>' : ["paused", "waiting", "failed", "cancelled"].includes(t.state) ? '<button class="primary" data-action="retry">Resume task ↗</button>' : ""}</div></div>${t.attempts.length ? `<details class="explanation"><summary>Run history · ${t.attempts.length}</summary>${t.attempts.map((a) => `<div class="history-row"><span>${esc(date(a.started))} · ${accountName(a.provider)}</span><span>${esc(names[a.state] || a.state)}</span></div>`).join("")}</details>` : ""}`;
  $("#detail-close").onclick = () => closeDialog($("#detail-dialog"));
  $("#pause-task")?.addEventListener("click", () => {
    closeDialog($("#detail-dialog"));
    pauseTarget = t.id;
    $("#pause-title").textContent = "Pause this task";
    openDialog("#pause-dialog");
  });
  $$("[data-action]", $("#detail-dialog")).forEach(
    (b) =>
      (b.onclick = async () => {
        b.disabled = true;
        try {
          await api("/v2/tasks/" + t.id + "/actions", "POST", {
            action: b.dataset.action,
            feedback: $("#feedback")?.value || "",
          });
          closeDialog($("#detail-dialog"));
          await refresh();
          notice(
            b.dataset.action === "approve"
              ? "Result approved."
              : b.dataset.action === "revise"
                ? "Feedback saved. Another pass is queued."
                : "Task updated.",
          );
        } catch (e) {
          notice(e.message, true);
          b.disabled = false;
        }
      }),
  );
  $("#edit-task")?.addEventListener("click", () => {
    closeDialog($("#detail-dialog"));
    newTask();
    editingTask = t.id;
    $("#task-dialog-title").textContent = "Edit task";
    $("#create-task").textContent = "Save changes";
    for (const key of [
      "title",
      "instructions",
      "project",
      "priority",
      "provider",
      "allowance",
      "schedule",
      "folder",
      "access",
      "source_url",
    ])
      $("#" + key).value = t[key] ?? "";
    $("#scheduled_at").value = t.scheduled_at ? localDate(t.scheduled_at) : "";
  });
}
$("#pause-all").onclick = async () => {
  if (!data) return;
  if (isPaused()) {
    try {
      await api("/v2/preferences", "PUT", {
        ...data.preferences,
        paused: false,
        paused_until: null,
      });
      await refresh();
      notice("Background work resumed.");
    } catch (e) {
      notice(e.message, true);
    }
  } else {
    pauseTarget = null;
    $("#pause-title").textContent = "Pause background work";
    openDialog("#pause-dialog");
  }
};
$("#pause-duration").onchange = () => {
  $("#pause-date-label").hidden = $("#pause-duration").value !== "custom";
  $("#pause-date").required = $("#pause-duration").value === "custom";
};
$("#pause-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const val = $("#pause-duration").value;
  const until =
    val === "custom"
      ? new Date($("#pause-date").value).toISOString()
      : Number(val)
        ? new Date(Date.now() + Number(val) * 1000).toISOString()
        : null;
  try {
    if (pauseTarget)
      await api("/v2/tasks/" + pauseTarget + "/actions", "POST", {
        action: "pause",
        until,
      });
    else
      await api("/v2/preferences", "PUT", {
        ...data.preferences,
        paused: !until,
        paused_until: until,
      });
    closeDialog($("#pause-dialog"));
    await refresh();
    notice(
      until ? "Paused until " + date(until) + "." : "Paused until you resume.",
    );
  } catch (err) {
    $(".form-error", form).textContent = err.message;
  }
};
function renderSettings() {
  if (document.activeElement !== $("#reserve")) {
    $("#reserve").value = data.preferences.reserve;
    $("#reserve-value").textContent = data.preferences.reserve + "%";
  }
  $("#account-settings").innerHTML = data.accounts
    .map(
      (a) =>
        `<div class="connection"><span>${accountName(a.provider)}</span><small>${a.connected ? "Connected" : "Quota reading unavailable"}</small></div>`,
    )
    .join("");
  $("#project-settings").innerHTML = data.projects.length
    ? data.projects
        .map(
          (p) =>
            `<div class="project-setting"><div>${esc(p.name)}<small>${p.allowance}% per account${p.folder ? " · " + esc(p.folder) : ""}</small></div><button class="quiet" data-edit-project="${esc(p.id)}">Edit</button></div>`,
        )
        .join("")
    : "<p>No projects yet. Add one from the sidebar.</p>";
  $$("[data-edit-project]").forEach(
    (b) =>
      (b.onclick = () =>
        projectDialog(
          data.projects.find((p) => p.id === b.dataset.editProject),
        )),
  );
}
$("#reserve").oninput = () => {
  $("#reserve-value").textContent = $("#reserve").value + "%";
};
$("#preferences-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/v2/preferences", "PUT", {
      ...data.preferences,
      reserve: Number($("#reserve").value),
    });
    await refresh();
    notice("Personal reserve updated.");
  } catch (err) {
    notice(err.message, true);
  }
};
$("#refresh-meters").onclick = async (e) => {
  const b = e.currentTarget;
  b.disabled = true;
  b.textContent = "Checking…";
  try {
    await api("/v1/meters/refresh", "POST", {});
    await refresh();
    notice("Account connections refreshed.");
  } catch (err) {
    notice(err.message, true);
  } finally {
    b.disabled = false;
    b.textContent = "Refresh connections";
  }
};
window.addEventListener("hashchange", () => {
  const h = location.hash.slice(1);
  if (h.startsWith("project=")) {
    projectFilter = h.slice(8);
    view = "tasks";
  } else if (["tasks", "review", "done", "settings"].includes(h)) {
    projectFilter = "";
    view = h;
  }
  render();
});
(async () => {
  const ticket = new URLSearchParams(location.hash.slice(1)).get("ticket");
  try {
    if (ticket) {
      history.replaceState(null, "", location.pathname);
      await api("/v1/dashboard/session", "POST", { ticket });
    } else {
      const hash = location.hash.slice(1);
      if (["tasks", "review", "done", "settings"].includes(hash)) view = hash;
      else if (hash.startsWith("project=")) projectFilter = hash.slice(8);
    }
    await refresh();
    setInterval(refresh, 4000);
  } catch (e) {
    notice(e.message, true);
    $("#login").hidden = false;
  }
})();
