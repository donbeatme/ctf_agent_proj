const STEPS = ["model", "skills", "agents", "experience", "run", "audit", "history"];
const ENGINE_STATES = [
  "PLANNING", "PLAN_REVIEW", "SCHEDULING", "EXECUTING",
  "STEP_EVAL", "REFLECTING", "DONE", "FAILED",
];

const state = {
  step: "model",
  runId: null,
  pollTimer: null,
  signalAfter: 0,
  eventAfter: 0,
  skills: [],
  historyFocus: "",
  source: "text",
  attachments: [],
  parsedTask: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function setStep(id) {
  state.step = id;
  document.querySelectorAll(".wizard-nav-item, .wizard-step").forEach((el) => {
    el.classList.toggle("active", el.dataset.step === id);
  });
  const i = STEPS.indexOf(id);
  $("wizard-step-indicator").textContent = `第 ${i + 1} / ${STEPS.length} 步`;
  $("btn-step-prev").disabled = i <= 0;
  $("btn-step-next").disabled = i >= STEPS.length - 1;
  $("btn-step-next").textContent = i >= STEPS.length - 1 ? "完成" : "下一步";
  if (id === "skills") loadSkills();
  if (id === "agents") loadCapabilities();
  if (id === "history") loadHistory();
  if (id === "run") refreshActive();
  if (id === "audit" && state.runId && $("audit-run-id") && !$("audit-run-id").value) {
    $("audit-run-id").value = state.runId;
  }
}

function renderPills(current) {
  $("state-pills").innerHTML = ENGINE_STATES.map((s) => {
    let cls = "";
    if (s === current) cls = s === "FAILED" ? "fail" : "now";
    else if (current === "DONE" && s !== "FAILED") cls = "done";
    return `<li class="${cls}">${s}</li>`;
  }).join("");
}

function renderDag(target, blueprint, stepResults) {
  const steps = (blueprint && blueprint.steps) || {};
  const results = stepResults || {};
  const ids = Object.keys(steps);
  if (!ids.length) {
    target.innerHTML = "<li class='meta'>尚无计划步骤</li>";
    return;
  }
  target.innerHTML = ids.map((id) => {
    const s = steps[id];
    const st = s.status || (results[id] && results[id].verdict) || "?";
    const dep = (s.depends_on || []).join(", ") || "∅";
    return `<li>
      <span class="sid">${id}</span>
      <span class="badge ${st}">${st}</span>
      ${s.skill_id ? `<span class="meta">skill=${s.skill_id}</span>` : ""}
      <div>${s.instruction || ""}</div>
      <div class="meta">criterion: ${s.criterion || ""} · depends_on: ${dep} · attempts=${s.attempts || 0}</div>
    </li>`;
  }).join("");
}

async function loadConfig() {
  $("model-status").textContent = "正在读取…";
  try {
    const c = await api("/api/config");
    $("model-base-url").value = c.LLM_BASE_URL || "";
    $("model-name").value = c.LLM_MODEL || "";
    $("model-enable-tools").checked = !(c.LLM_ENABLE_TOOLS === false || c.LLM_ENABLE_TOOLS === "false");
    $("model-key-set").textContent = c.key_set ? "已设置" : "未设置";
    $("model-current").textContent = c.LLM_MODEL || "-";
    $("model-tools-label").textContent = $("model-enable-tools").checked ? "开" : "关";
    $("model-status").textContent = "已加载 model_config（环境变量优先）。";
    if ($("engine-budget")) {
      $("engine-budget").textContent = JSON.stringify(c.engine || {}, null, 2);
    }
  } catch (e) {
    $("model-status").textContent = e.message;
  }
}

async function saveConfig() {
  $("model-status").textContent = "保存中…";
  try {
    const body = {
      LLM_BASE_URL: $("model-base-url").value.trim(),
      LLM_MODEL: $("model-name").value.trim(),
      LLM_ENABLE_TOOLS: $("model-enable-tools").checked,
    };
    const key = $("model-api-key").value.trim();
    if (key) body.LLM_API_KEY = key;
    const c = await api("/api/config", { method: "POST", body: JSON.stringify(body) });
    $("model-api-key").value = "";
    $("model-key-set").textContent = c.key_set ? "已设置" : "未设置";
    $("model-current").textContent = c.LLM_MODEL || "-";
    $("model-tools-label").textContent = $("model-enable-tools").checked ? "开" : "关";
    $("model-status").textContent = "已写入 model_config.json，后续 LLM 调用即时生效。";
  } catch (e) {
    $("model-status").textContent = e.message;
  }
}

async function envCheck() {
  $("env-status").textContent = "探测中…";
  try {
    const r = await api("/api/env-check");
                const missing = r.missing || 0;
                const avail = r.available || 0;
                $("env-status").textContent = `available ${avail} / missing ${missing} / total ${r.total || 0}`;
    $("env-report").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("env-status").textContent = e.message;
  }
}

async function loadSkills() {
  const cat = $("skill-category").value;
  const q = $("skill-search").value.trim();
  $("skill-status").textContent = "加载中…";
  const qs = new URLSearchParams();
  if (cat) qs.set("category", cat);
  if (q) qs.set("q", q);
  const data = await api("/api/skills?" + qs.toString());
  if (!$("skill-category").dataset.filled) {
    data.categories.forEach((c) => {
      const o = document.createElement("option");
      o.value = c; o.textContent = c;
      $("skill-category").appendChild(o);
    });
    $("skill-category").dataset.filled = "1";
  }
  state.skills = data.items;
  $("skill-list").innerHTML = data.items.map((it) =>
    `<li data-id="${it.doc_id}"><strong>${it.doc_id}</strong> <span class="meta">${it.kind} · ${it.category}</span><div class="meta">${it.description}</div></li>`
  ).join("");
  $("skill-status").textContent = `${data.items.length} 条文档`;
  const tools = await api("/api/tools");
  $("tool-installer").textContent = "installer_path: " + tools.installer_path;
  $("tool-list").innerHTML = (tools.manifest || []).map((t) =>
    `<li><strong>${t.tool_id}</strong> <span class="meta">${t.install_method}</span><div class="meta">${t.description} · ${t.verify_check || "manual"}</div></li>`
  ).join("");
}

async function probeSandbox() {
  $("sandbox-status").textContent = "探测中…";
  try {
    const r = await api("/api/sandbox");
    $("sandbox-status").textContent = r.note || "完成";
    $("sandbox-report").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("sandbox-status").textContent = e.message;
  }
}

const CAP_LABEL = {
  wired: "已接线",
  wired_declare: "声明已接线",
  stub: "接口桩",
  reserved: "未接线",
  frontend_reserved: "前端预留",
};

async function loadCapabilities() {
  $("cap-status").textContent = "加载中…";
  try {
    const r = await api("/api/capabilities");
    $("cap-list").innerHTML = (r.layers || []).map((L) =>
      `<li class="cap-item status-${L.status}">
        <div class="cap-head">
          <strong>${L.name}</strong>
          <span class="badge ${L.status}">${CAP_LABEL[L.status] || L.status}</span>
        </div>
        <div class="meta">契约: ${L.contract}</div>
        <div class="meta">实现: ${L.impl || "—"}</div>
        ${L.note ? `<div class="meta">${L.note}</div>` : ""}
      </li>`
    ).join("");
    $("cap-status").textContent = `${(r.layers || []).length} 个能力层`;
  } catch (e) {
    $("cap-status").textContent = e.message;
  }
}

async function queryExperience() {
  const topics = ($("exp-topics").value || "").split(",").map((s) => s.trim()).filter(Boolean);
  try {
    const r = await api("/api/experience/query", {
      method: "POST",
      body: JSON.stringify({ topics, role: $("exp-role").value }),
    });
    $("exp-query-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("exp-query-out").textContent = e.message;
  }
}

async function recordExperience() {
  try {
    const r = await api("/api/experience/record", {
      method: "POST",
      body: JSON.stringify({
        event: {
          topic: $("exp-rec-topic").value.trim(),
          outcome: $("exp-rec-outcome").value,
          summary: $("exp-rec-summary").value.trim(),
        },
      }),
    });
    $("exp-record-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("exp-record-out").textContent = e.message;
  }
}

function auditRunId() {
  return ($("audit-run-id").value || state.runId || "").trim();
}

async function loadAuditReport() {
  const id = auditRunId();
  if (!id) {
    $("audit-status").textContent = "请填写 run_id";
    return;
  }
  $("audit-status").textContent = "生成中…";
  $("audit-product").classList.add("hidden");
  try {
    const r = await api("/api/runs/" + encodeURIComponent(id) + "/report");
    $("audit-report").textContent = r.markdown || JSON.stringify(r, null, 2);
    $("audit-status").textContent = `状态 ${r.status} · events ${r.event_count} · steps ${r.step_count}`;
  } catch (e) {
    $("audit-status").textContent = e.message;
  }
}

async function loadAuditProduct() {
  const id = auditRunId();
  if (!id) {
    $("audit-status").textContent = "请填写 run_id";
    return;
  }
  try {
    const r = await api("/api/runs/" + encodeURIComponent(id) + "/product");
    $("audit-product").classList.remove("hidden");
    $("audit-product").textContent = JSON.stringify(r, null, 2);
    $("audit-status").textContent = `product · ${r.status}`;
  } catch (e) {
    $("audit-status").textContent = e.message;
  }
}

async function verifyFlag() {
  try {
    const r = await api("/api/flag/verify", {
      method: "POST",
      body: JSON.stringify({
        flag: $("flag-value").value.trim(),
        run_id: $("flag-run-id").value.trim() || state.runId || null,
      }),
    });
    $("flag-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("flag-out").textContent = e.message;
  }
}

async function hitlPending() {
  try {
    const r = await api("/api/hitl/pending");
    $("hitl-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("hitl-out").textContent = e.message;
  }
}

async function hitlDecide() {
  try {
    const r = await api("/api/hitl/decide", {
      method: "POST",
      body: JSON.stringify({
        run_id: $("hitl-run-id").value.trim() || state.runId || null,
        decision: $("hitl-decision").value,
      }),
    });
    $("hitl-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("hitl-out").textContent = e.message;
  }
}

async function openSkill(id) {
  const d = await api("/api/skills/" + encodeURIComponent(id));
  $("skill-detail").classList.remove("hidden");
  $("skill-detail-title").textContent = d.doc_id;
  $("skill-detail-body").textContent = d.text;
}

function focusRun(runId) {
  state.runId = runId;
  state.signalAfter = 0;
  state.eventAfter = 0;
  $("signal-log").textContent = "";
  $("event-timeline").innerHTML = "";
  if (state.pollTimer) clearInterval(state.pollTimer);
  pollRun();
  state.pollTimer = setInterval(pollRun, 900);
}

async function pollRun() {
  if (!state.runId) return;
  try {
    const snap = await api("/api/runs/" + encodeURIComponent(state.runId));
    $("run-id").textContent = snap.run_id;
    $("run-status").textContent = snap.status + (snap.alive ? " · live" : "");
    $("run-step").textContent = snap.current_step || "-";
    $("run-tokens").textContent = snap.run_tokens || 0;
    $("run-message").textContent = snap.task && snap.task.title
      ? `${snap.task.title} — ${snap.task.description || ""}`
      : "运行中";
    $("run-fail").textContent = snap.fail_reason || snap.error || "";
    $("run-controls").classList.toggle("hidden", !snap.alive);
    renderPills(snap.status);
    renderDag($("dag-list"), snap.blueprint, snap.steps);

    const sig = await api(`/api/runs/${encodeURIComponent(state.runId)}/signals?after=${state.signalAfter}`);
    (sig.signals || []).forEach((s) => {
      state.signalAfter = Math.max(state.signalAfter, s.seq || 0);
      const line = `[${s.ts}] ${s.signal} ${JSON.stringify(s.data)}\n`;
      $("signal-log").textContent = ($("signal-log").textContent + line).slice(-12000);
    });
    $("signal-log").scrollTop = $("signal-log").scrollHeight;

    const ev = await api(`/api/runs/${encodeURIComponent(state.runId)}/events?after=${state.eventAfter}`);
    (ev.events || []).forEach((e) => {
      state.eventAfter = e._i || state.eventAfter;
      const li = document.createElement("li");
      li.textContent = `${e.ts || ""} ${e.agent || ""} ${e.kind}${e.step_id ? " " + e.step_id : ""}${e.verdict ? " → " + e.verdict : ""}`;
      $("event-timeline").appendChild(li);
    });

    const log = await api(`/api/runs/${encodeURIComponent(state.runId)}/log?tail=180`);
    $("run-log").textContent = log.log || "";
    $("run-log").scrollTop = $("run-log").scrollHeight;

    if (!snap.alive && (snap.status === "DONE" || snap.status === "FAILED")) {
      refreshActive();
    }
  } catch (e) {
    $("run-message").textContent = e.message;
  }
}

async function refreshActive() {
  const data = await api("/api/runs");
  const runs = data.runs || [];
  $("active-runs").innerHTML = runs.slice(0, 8).map((r) =>
    `<button type="button" class="run-chip ${r.run_id === state.runId ? "active" : ""}" data-id="${r.run_id}">${r.run_id} · ${r.status}</button>`
  ).join("") || "<span class='meta'>暂无 run</span>";
}

function setSource(src) {
  state.source = src;
  document.querySelectorAll(".source-tab").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  document.querySelectorAll(".source-pane").forEach((p) => p.classList.toggle("active", p.dataset.src === src));
  invalidateParse();
}

function invalidateParse() {
  state.parsedTask = null;
  $("btn-start").disabled = true;
  $("type-box").classList.add("hidden");
  $("start-hint").textContent = "来源已变更，请重新「解析题型」。";
}

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result || "");
      const i = s.indexOf(",");
      resolve(i >= 0 ? s.slice(i + 1) : s);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function ensureUploads() {
  const input = $("task-files");
  const files = Array.from(input.files || []);
  if (!files.length) return state.attachments || [];
  $("upload-status").textContent = `上传 ${files.length} 个文件…`;
  const payload = [];
  for (const f of files) {
    if (f.size > 25 * 1024 * 1024) throw new Error(`${f.name} 超过 25MB`);
    payload.push({ name: f.name, mime: f.type, content_b64: await fileToB64(f) });
  }
  const res = await api("/api/challenge/upload", {
    method: "POST",
    body: JSON.stringify({ files: payload }),
  });
  state.attachments = res.attachments || [];
  $("task-file-list").innerHTML = state.attachments.map((a) =>
    `<li><strong>${a.name}</strong> <span class="meta">${a.size} B · ${a.path}</span></li>`
  ).join("");
  $("upload-status").textContent = `已保存 ${state.attachments.length} 个附件到 downloads/uploads/`;
  return state.attachments;
}

async function buildParseBody() {
  const override = $("task-type-override").value || null;
  const goals = $("task-goals").value.trim();
  const body = { category_override: override || undefined };
  if (goals) body.goals = goals.split(",").map((g) => ({ id: g.trim() })).filter((g) => g.id);

  if (state.source === "text") {
    body.title = $("task-title").value.trim();
    body.description = $("task-desc").value.trim();
    body.challenge_id = $("task-cid").value.trim();
  } else if (state.source === "json") {
    body.json = $("task-json").value.trim();
    if (!body.json) throw new Error("请粘贴 JSON");
  } else if (state.source === "url") {
    body.target_url = $("task-url").value.trim();
    body.description = $("task-url-desc").value.trim();
    body.title = body.title || "远程目标";
    if (!body.target_url) throw new Error("请填写目标 URL");
  } else if (state.source === "files") {
    body.attachments = await ensureUploads();
    body.title = $("task-file-title").value.trim() || "附件题";
    body.description = $("task-file-desc").value.trim();
    if (!body.attachments.length) throw new Error("请选择附件");
  }
  return body;
}

function showClassification(data) {
  state.parsedTask = data.task;
  const c = data.classification || {};
  $("type-box").classList.remove("hidden");
  $("type-primary").textContent = `${c.label || "-"} (${c.primary || "-"})`;
  $("type-confidence").textContent = c.confidence != null ? String(c.confidence) : "-";
  $("type-goals").textContent = (data.goals_preview || []).join(", ") || "-";
  $("type-ranked").innerHTML = (c.ranked || []).map((h) =>
    `<li><strong>${h.label}</strong> score=${h.score} · ${(h.evidence || []).slice(0, 4).join("; ")}</li>`
  ).join("");
  $("type-preview").textContent = JSON.stringify({
    challenge_type: data.task && data.task.challenge_type,
    attachments: data.task && data.task.attachments,
    description: data.task && data.task.description,
  }, null, 2);
  $("btn-start").disabled = false;
  $("start-hint").textContent = "题型已确认。点击启动后进入 ChallengeUnderstander → Engine.run。";
}

async function parseChallenge() {
  $("start-hint").textContent = "正在解析题型…";
  try {
    const body = await buildParseBody();
    const data = await api("/api/challenge/parse", { method: "POST", body: JSON.stringify(body) });
    showClassification(data);
  } catch (e) {
    $("start-hint").textContent = e.message;
    $("btn-start").disabled = true;
  }
}

async function startTask() {
  if (!state.parsedTask) {
    $("start-hint").textContent = "请先解析题型";
    return;
  }
  $("start-hint").textContent = "正在 Engine.run…";
  $("btn-start").disabled = true;
  try {
    const r = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ task: state.parsedTask }),
    });
    $("start-hint").textContent =
      `已启动 ${r.run_id} · 题型 ${r.challenge_type_label || r.challenge_type || "-"}`;
    focusRun(r.run_id);
    refreshActive();
  } catch (e) {
    $("start-hint").textContent = e.message;
    $("btn-start").disabled = false;
  }
}

async function loadHistory() {
  $("history-status").textContent = "加载中…";
  const data = await api("/api/runs");
  const runs = data.runs || [];
  $("history-list").innerHTML = runs.map((r) =>
    `<li data-id="${r.run_id}"><strong>${r.run_id}</strong> <span class="badge ${r.status}">${r.status}</span>
     <div class="meta">${r.created_at || ""} · ${(r.task && r.task.title) || ""} · ${r.step_count} steps</div></li>`
  ).join("");
  $("history-status").textContent = `${runs.length} 条`;
}

async function openHistory(id) {
  state.historyFocus = id;
  const snap = await api("/api/runs/" + encodeURIComponent(id));
  const ev = await api(`/api/runs/${encodeURIComponent(id)}/events?after=0`);
  const log = await api(`/api/runs/${encodeURIComponent(id)}/log?tail=200`);
  $("history-detail").classList.remove("hidden");
  $("history-title").textContent = snap.run_id;
  $("hist-id").textContent = snap.run_id;
  $("hist-status").textContent = snap.status;
  $("hist-tokens").textContent = snap.run_tokens || 0;
  $("hist-steps").textContent = snap.step_count || 0;
  $("hist-meta").textContent = JSON.stringify({ task: snap.task, goals: snap.goal_list, fail: snap.fail_reason }, null, 2);
  renderDag($("hist-dag"), snap.blueprint, snap.steps);
  $("hist-events").innerHTML = (ev.events || []).map((e) =>
    `<li>${e.ts || ""} ${e.kind} ${e.step_id || ""} ${e.verdict || ""}</li>`
  ).join("");
  $("hist-log").textContent = log.log || "";
}

function bind() {
  document.querySelectorAll(".wizard-nav-item").forEach((b) => b.addEventListener("click", () => setStep(b.dataset.step)));
  $("btn-step-prev").addEventListener("click", () => setStep(STEPS[Math.max(0, STEPS.indexOf(state.step) - 1)]));
  $("btn-step-next").addEventListener("click", () => {
    const i = STEPS.indexOf(state.step);
    if (i < STEPS.length - 1) setStep(STEPS[i + 1]);
  });
  $("btn-save-model").addEventListener("click", saveConfig);
  $("btn-reload-model").addEventListener("click", loadConfig);
  $("btn-env-check").addEventListener("click", envCheck);
  $("btn-sandbox").addEventListener("click", probeSandbox);
  $("btn-capabilities").addEventListener("click", loadCapabilities);
  $("btn-exp-query").addEventListener("click", queryExperience);
  $("btn-exp-record").addEventListener("click", recordExperience);
  $("btn-audit-report").addEventListener("click", loadAuditReport);
  $("btn-audit-product").addEventListener("click", loadAuditProduct);
  $("btn-audit-use-current").addEventListener("click", () => {
    if (state.runId) $("audit-run-id").value = state.runId;
  });
  $("btn-flag-verify").addEventListener("click", verifyFlag);
  $("btn-hitl-pending").addEventListener("click", hitlPending);
  $("btn-hitl-decide").addEventListener("click", hitlDecide);
  $("skill-category").addEventListener("change", loadSkills);
  $("skill-search").addEventListener("input", () => { clearTimeout(state._sk); state._sk = setTimeout(loadSkills, 200); });
  $("skill-list").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) openSkill(li.dataset.id);
  });
  $("btn-skill-close").addEventListener("click", () => $("skill-detail").classList.add("hidden"));
  $("btn-start").addEventListener("click", () => startTask().catch((e) => { $("start-hint").textContent = e.message; }));
  $("btn-parse").addEventListener("click", () => parseChallenge());
  document.querySelectorAll(".source-tab").forEach((b) => b.addEventListener("click", () => setSource(b.dataset.src)));
  ["task-title", "task-desc", "task-cid", "task-goals", "task-json", "task-url", "task-url-desc",
   "task-file-title", "task-file-desc", "task-type-override"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", invalidateParse);
    if (el) el.addEventListener("change", invalidateParse);
  });
  $("task-files").addEventListener("change", () => {
    state.attachments = [];
    const files = Array.from($("task-files").files || []);
    $("task-file-list").innerHTML = files.map((f) =>
      `<li><strong>${f.name}</strong> <span class="meta">${f.size} B（待上传）</span></li>`
    ).join("");
    invalidateParse();
  });
  $("btn-stop").addEventListener("click", () => {
    if (!state.runId) return;
    api("/api/runs/" + encodeURIComponent(state.runId) + "/stop", { method: "POST" })
      .then(() => { $("run-message").textContent = "已请求停止"; });
  });
  $("btn-active-reload").addEventListener("click", refreshActive);
  $("active-runs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-id]");
    if (b) focusRun(b.dataset.id);
  });
  $("btn-history-reload").addEventListener("click", loadHistory);
  $("history-list").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) openHistory(li.dataset.id);
  });
  $("btn-hist-close").addEventListener("click", () => $("history-detail").classList.add("hidden"));
  $("btn-hist-focus").addEventListener("click", () => {
    if (!state.historyFocus) return;
    setStep("run");
    focusRun(state.historyFocus);
  });
  $("btn-hist-resume").addEventListener("click", async () => {
    if (!state.historyFocus) return;
    await api("/api/runs/" + encodeURIComponent(state.historyFocus) + "/resume", { method: "POST" });
    setStep("run");
    focusRun(state.historyFocus);
  });
  $("btn-hist-delete").addEventListener("click", async () => {
    if (!state.historyFocus) return;
    if (!confirm("删除 " + state.historyFocus + " ?")) return;
    await api("/api/runs/" + encodeURIComponent(state.historyFocus), { method: "DELETE" });
    $("history-detail").classList.add("hidden");
    loadHistory();
  });
}

bind();
loadConfig();
renderPills("");
refreshActive().catch(() => {});
