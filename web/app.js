const STEPS = ["model", "run", "agents", "audit", "history", "skills", "experience", "usage", "mcp", "user"];
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
  tools: [],
  toolCategory: "",
  sandboxItems: [],
  sandboxCategory: "",
  agentRole: "",
  blackboardType: "",
  reviewStatus: "",
  historyStatus: "",
  usageView: "",
  usageChartView: "stage",
  mcpType: "",
  historyRuns: [],
  reviewItems: [],
  usageItems: [],
  dashboardWindow: "week",
  dashboardTimer: null,
  dashboardLoading: false,
  dashboardTelemetry: null,
  telemetryPromise: null,
  taskPage: "intake",
  taskTabs: [],
  runSnapshot: null,
  runEvents: [],
  runLog: "",
  mcpItems: [],
  blackboardItems: [],
  agentRoleItems: [],
};

const $ = (id) => document.getElementById(id);

const CTF_TYPE_NAV = [
  "ctf-web", "ctf-pwn", "ctf-crypto", "ctf-reverse",
  "ctf-forensics", "ctf-misc", "ctf-osint", "ctf-malware", "ctf-ai-ml",
];

const CTF_TYPE_LABEL = {
  "ctf-web": "Web 安全",
  "ctf-pwn": "二进制攻防",
  "ctf-crypto": "密码安全",
  "ctf-reverse": "逆向分析",
  "ctf-forensics": "取证分析",
  "ctf-misc": "综合场景",
  "ctf-osint": "情报研判",
  "ctf-malware": "恶意样本分析",
  "ctf-ai-ml": "AI 安全",
};

const AGENT_ROLE_ITEMS = [
  { id: "intake", type: "输入", title: "任务情报感知", badge: "wired", value: "Text / URL / JSON / Files", desc: "统一接入任务情报、附件、远程地址和平台 JSON。", detail: "输入层负责把用户投递的多源信息规范成行动任务，保留附件、目标 URL、成果口令格式和平台元数据。" },
  { id: "understander", type: "理解", title: "RealTaskUnderstander", badge: "wired", value: "真实任务解析", desc: "读取本地任务目录、metadata、附件并生成目标列表。", detail: "输出场景类型、置信度、目标列表、artifacts、target_info，为能力检索、工具筛选和模型提示词提供结构化上下文。" },
  { id: "planner", type: "规划", title: "Planner", badge: "wired", value: "真实 LLM", desc: "召回能力库，生成可解释 DAG。", detail: "Planner 将行动目标拆成步骤，标注依赖、验收标准、能力引用和重试策略。" },
  { id: "executor", type: "执行", title: "Executor", badge: "wired", value: "Web 真实沙箱", desc: "Web 默认通过 RealExecutor 连接 SSH 沙箱并运行命令。", detail: "点击派发后，后端先确保 Match 专用 Lima VM 已启动，再为任务创建独立 Docker 容器；工具调用、命令输出和容器生命周期会持续写入任务事件流。" },
  { id: "evaluator", type: "验收", title: "Evaluator + Platform Submit", badge: "wired", value: "本地核验/平台提交", desc: "验证证据、候选成果口令、失败反思。", detail: "Evaluator 负责 step_eval、review、reflect、eval_goals；成果审核入口已接平台适配器的本地核验与提交能力。" },
];

const BLACKBOARD_FALLBACK = [{
  id: "experience-unavailable",
  type: "后端状态",
  title: "经验沉淀未接入",
  badge: "reserved",
  value: "reserved",
  desc: "当前后端返回预留契约，没有可展示的经验条目。",
  detail: "数据来源: GET /api/experience。只有接口返回 items 后，本页才会展示真实线索、失败路径和可复用打法。",
}];

const REVIEW_ITEMS = [
  { id: "pending", type: "待审核", title: "暂无待审核成果", badge: "reserved", value: "0", desc: "候选出现后显示来源步骤、证据片段和置信度。", detail: "审核卡片应包含成果候选、run_id、step_id、工具命令、证据摘要、模型解释和 approve/reject/replan 动作。" },
  { id: "approved", type: "已通过", title: "人工确认 / 自动验收", badge: "wired", value: "0", desc: "通过后进入报告与复盘交付。", detail: "通过的成果口令会绑定证据链，写入 product 和最终交付报告。" },
  { id: "rejected", type: "已驳回", title: "误报与重规划", badge: "neutral", value: "0", desc: "错误候选会反馈给 Planner。", detail: "驳回原因用于避免重复提交，并触发重新检索技能或改换工具链。" },
  { id: "manual", type: "人工接管", title: "需要队员判断", badge: "reserved", value: "0", desc: "高风险提交或证据不足时进入人工席。", detail: "适合决赛环境中要求人工把关提交频率、误封风险和不可解释答案的场景。" },
];

const USAGE_ITEMS = [
  { id: "overview", type: "总览", title: "总 Tokens", value: "0", desc: "当前无活跃任务。", detail: "总览聚合 Prompt、Completion、工具解释、报告输出和重试消耗。" },
  { id: "stage", type: "阶段", title: "Agent 阶段分布", value: "0", desc: "理解、规划、执行解释、复盘报告。", detail: "阶段维度用于定位成本集中在任务情报理解、DAG 规划、工具输出解释还是报告生成。" },
  { id: "model", type: "模型", title: "模型策略", value: "默认", desc: "主模型、低成本模型、多模型协同。", detail: "模型维度用于对比不同模型连接、上下文预算、成本估算和失败重试。" },
  { id: "challenge", type: "场景", title: "场景成本", value: "0", desc: "Web 安全、二进制攻防、密码安全等场景成本对比。", detail: "场景维度适合赞助展示：展示不同攻防场景的自动化覆盖、平均 token 和研判时长。" },
];

const MCP_FALLBACK = [{
  id: "mcp-registry-unavailable",
  type: "后端状态",
  title: "MCP 注册表未接入",
  badge: "reserved",
  value: "not exposed",
  desc: "现有后端只提供内部能力声明与工具目录，没有 MCP Server 注册表接口。",
  detail: "数据来源: GET /api/capabilities。这里不会把内部 Python 组件伪装成已同步的 MCP Server。",
}];

const SAMPLE_TASKS = {
  web: {
    title: "Web 登录接口研判",
    description: "目标 Web 服务存在登录接口与上传入口。请识别可疑参数、检查认证绕过与文件上传风险，并给出可复核的成果口令。",
    id: "sample-web",
    type: "ctf-web",
  },
  crypto: {
    title: "RSA 参数安全分析",
    description: "给定 RSA 公钥参数 n、e 与密文 c，疑似存在低指数或共享因子风险。请完成参数分析并输出成果口令。",
    id: "sample-crypto",
    type: "ctf-crypto",
  },
  binary: {
    title: "二进制服务风险复现",
    description: "远程服务暴露交互式二进制程序，样本疑似存在栈溢出。请完成保护检查、输入点定位、风险复现与证据归档。",
    id: "sample-binary",
    type: "ctf-pwn",
  },
  reverse: {
    title: "逆向样本算法还原",
    description: "附件样本包含输入校验逻辑，疑似经过轻量混淆。请还原关键算法、说明判断路径并生成可验证成果。",
    id: "sample-reverse",
    type: "ctf-reverse",
  },
  forensics: {
    title: "流量证据包分析",
    description: "给定网络流量包，疑似包含异常登录、文件传输和隐藏线索。请提取关键会话、还原证据链并输出成果口令。",
    id: "sample-forensics",
    type: "ctf-forensics",
  },
};

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
  if (id === "model") startDashboard();
  if (id === "run") loadPlatformStatus();
  if (id === "skills") loadSkills();
  if (id === "agents") {
    loadCapabilities();
  }
  if (id === "experience") loadBlackboardStatus();
  if (id === "history") loadHistory();
  if (id === "usage") loadUsageSummary();
  if (id === "mcp") loadMcpStatus();
  if (id === "run") refreshActive();
  if (id === "audit") loadReviewQueue();
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
    if ($("model-check-time")) $("model-check-time").textContent = new Date().toLocaleTimeString();
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
    if ($("model-check-time")) $("model-check-time").textContent = new Date().toLocaleTimeString();
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

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = n;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i += 1;
  }
  return `${size.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function skillGlyph(category) {
  const c = String(category || "").toLowerCase();
  if (c.includes("web")) return "WEB";
  if (c.includes("pwn")) return "PWN";
  if (c.includes("crypto")) return "CRY";
  if (c.includes("reverse") || c.includes("rev")) return "REV";
  if (c.includes("forensic")) return "FOR";
  if (c.includes("osint")) return "OSI";
  if (c.includes("misc")) return "MSC";
  return "SKL";
}

function skillChain(category, kind) {
  const c = String(category || "").toLowerCase();
  if (c.includes("web")) return "Recon -> Exploit -> Proof";
  if (c.includes("pwn")) return "保护检查 -> 调试验证 -> 风险复现";
  if (c.includes("crypto")) return "Math -> Script -> Verify";
  if (c.includes("reverse") || c.includes("rev")) return "Static -> Trace -> Patch";
  if (c.includes("forensic")) return "Extract -> Carve -> Recover";
  if (c.includes("osint")) return "Source -> Correlate -> Proof";
  return `${kind || "Skill"} -> Tool -> Evidence`;
}

const DASHBOARD_COLORS = ["#25f2b4", "#ffbe55", "#9b8cff", "#37d6ff", "#ff5c7a", "#7da1a8", "#6d5bd0"];

function timestamp(value) {
  const n = Date.parse(value || "");
  return Number.isFinite(n) ? n : 0;
}

function dashboardBuckets(windowKey) {
  const count = windowKey === "day" ? 12 : windowKey === "month" ? 30 : 7;
  const stepMs = windowKey === "day" ? 2 * 60 * 60 * 1000 : 24 * 60 * 60 * 1000;
  const end = Date.now();
  const start = end - count * stepMs;
  const labels = Array.from({ length: count }, (_, i) => {
    const d = new Date(start + (i + 1) * stepMs);
    return windowKey === "day"
      ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
      : `${d.getMonth() + 1}/${d.getDate()}`;
  });
  return { count, stepMs, start, end, labels };
}

async function fetchAllRunEvents(runId, maxEvents = 6000) {
  const events = [];
  let after = 0;
  while (events.length < maxEvents) {
    const payload = await api(`/api/runs/${encodeURIComponent(runId)}/events?after=${after}`);
    const batch = payload.events || [];
    if (!batch.length) break;
    events.push(...batch);
    const next = Number((batch[batch.length - 1] || {})._i || after);
    if (next <= after) break;
    after = next;
    if (batch.length < 300) break;
  }
  return events.slice(0, maxEvents);
}

async function fetchTelemetry(limit = 60) {
  if (state.telemetryPromise) return state.telemetryPromise;
  state.telemetryPromise = (async () => {
    const data = await api("/api/runs");
    const runs = (data.runs || []).slice(0, limit);
    const eventRows = await Promise.all(runs.map(async (run) => {
      const events = await fetchAllRunEvents(run.run_id).catch(() => []);
      return [run.run_id, events];
    }));
    const runtime = await api("/api/sandbox/runtime").catch((e) => ({ ready: false, error: e.message }));
    return { runs, eventMap: new Map(eventRows), runtime };
  })();
  try {
    return await state.telemetryPromise;
  } finally {
    state.telemetryPromise = null;
  }
}

function roleTokenTotals(events) {
  const totals = { planner: 0, executor: 0, evaluator: 0, other: 0 };
  events.filter((e) => e.kind === "llm_usage").forEach((e) => {
    const detail = e.detail || {};
    const role = String(detail.role || e.agent || "other").toLowerCase();
    const key = role === "planner" ? "planner"
      : role === "executor" ? "executor"
        : role.includes("evaluator") ? "evaluator" : "other";
    totals[key] += Number(detail.total_tokens || 0);
  });
  return totals;
}

function buildDashboardData(telemetry, windowKey) {
  const buckets = dashboardBuckets(windowKey);
  const attempts = Array(buckets.count).fill(0);
  const solved = Array(buckets.count).fill(0);
  const tokens = Array(buckets.count).fill(0);
  const sandboxes = Array(buckets.count).fill(0);
  const submissions = Array(buckets.count).fill(0);
  const correctSubmissions = Array(buckets.count).fill(0);
  const categoryCounts = new Map();
  let funnelCandidate = 0;
  let funnelEvidence = 0;
  let funnelApproved = 0;
  let funnelReplan = 0;
  const windowEvents = [];
  const windowRuns = telemetry.runs.filter((run) => {
    const at = timestamp(run.created_at);
    return at >= buckets.start && at <= buckets.end;
  });

  windowRuns.forEach((run) => {
    const at = timestamp(run.created_at) || buckets.end - 1;
    const index = Math.max(0, Math.min(buckets.count - 1, Math.floor((at - buckets.start) / buckets.stepMs)));
    const events = telemetry.eventMap.get(run.run_id) || [];
    const submitEvents = events.filter((e) => e.kind === "submission");
    const correct = submitEvents.filter((e) => (e.detail || {}).correct === true);
    attempts[index] += 1;
    if (correct.length) solved[index] += 1;
    tokens[index] += Number(run.run_tokens || 0);
    sandboxes[index] += events.filter((e) => e.kind === "sandbox.container_created").length;
    submissions[index] += submitEvents.length;
    correctSubmissions[index] += correct.length;
    const category = runCategory(run);
    categoryCounts.set(category, (categoryCounts.get(category) || 0) + 1);
    funnelCandidate += submitEvents.length;
    funnelApproved += correct.length;
    funnelReplan += events.filter((e) => e.kind === "replan").length;
    if (submitEvents.length && events.some((e) => e.kind === "tool_result") && events.some((e) => e.kind === "step_record")) {
      funnelEvidence += submitEvents.length;
    }
    windowEvents.push(...events);
  });

  const categories = Array.from(categoryCounts.entries()).map(([label, value], i) => [label, value, DASHBOARD_COLORS[i % DASHBOARD_COLORS.length]]);
  if (!categories.length) categories.push(["暂无任务", 0, DASHBOARD_COLORS[0]]);
  const running = telemetry.runs.filter((r) => r.alive).length;
  const queued = telemetry.runs.filter((r) => r.phase === "queued" || r.status === "QUEUED").length;
  const doneCount = windowRuns.filter((r) => r.status === "DONE").length;
  const otherCount = telemetry.runs.filter((r) => !r.alive && r.status !== "DONE" && r.phase !== "queued").length;
  const review = windowRuns.filter((run) => run.status === "DONE" && !(telemetry.eventMap.get(run.run_id) || []).some((e) => e.kind === "submission" && (e.detail || {}).correct === true)).length;
  const submitTotal = sum(submissions);
  const approvedTotal = sum(correctSubmissions);
  return {
    labels: buckets.labels,
    solved,
    attempts,
    accuracy: submissions.map((n, i) => n ? Math.round((correctSubmissions[i] / n) * 100) : 0),
    tokens,
    sandboxes,
    categories,
    windowRuns,
    windowEvents,
    running,
    queued,
    doneCount,
    otherCount,
    review,
    funnelCandidate,
    funnelEvidence,
    funnelApproved,
    funnelReplan,
    submitTotal,
    approvedTotal,
    roleTokens: roleTokenTotals(windowEvents),
  };
}

function canvasCtx(id) {
  const canvas = $(id);
  if (!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: rect.width, h: rect.height };
}

function drawAxes(ctx, w, h) {
  ctx.strokeStyle = "rgba(211, 255, 245, 0.14)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = 34 + ((h - 58) / 4) * i;
    ctx.beginPath();
    ctx.moveTo(36, y);
    ctx.lineTo(w - 16, y);
    ctx.stroke();
  }
}

function drawLineChart(id, labels, seriesA, seriesB) {
  const c = canvasCtx(id);
  if (!c) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);
  drawAxes(ctx, w, h);
  const max = Math.max(...seriesA, ...seriesB, 1);
  const plot = (series, color, fill) => {
    ctx.beginPath();
    series.forEach((v, i) => {
      const x = 38 + (i / Math.max(1, series.length - 1)) * (w - 58);
      const y = h - 28 - (v / max) * (h - 62);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.stroke();
    if (fill) {
      ctx.lineTo(w - 20, h - 28);
      ctx.lineTo(38, h - 28);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    }
  };
  plot(seriesB, "#ffbe55", "rgba(255, 190, 85, 0.1)");
  plot(seriesA, "#25f2b4", "rgba(37, 242, 180, 0.18)");
  ctx.fillStyle = "rgba(220, 252, 244, 0.72)";
  ctx.font = "12px ui-monospace, Menlo, monospace";
  labels.filter((_, i) => i % Math.ceil(labels.length / 6) === 0).forEach((label, idx, arr) => {
    const x = 38 + (idx / Math.max(1, arr.length - 1)) * (w - 58);
    ctx.fillText(label, x - 12, h - 8);
  });
}

function drawDonutChart(id, items) {
  const c = canvasCtx(id);
  if (!c) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);
  const cx = w * 0.42;
  const cy = h * 0.52;
  const r = Math.min(w, h) * 0.3;
  const total = items.reduce((s, it) => s + it[1], 0);
  if (!total) {
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.lineWidth = 28;
    ctx.strokeStyle = "rgba(220, 235, 232, 0.3)";
    ctx.stroke();
    ctx.fillStyle = "rgba(237, 246, 244, 0.72)";
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillText("暂无任务", w * 0.68, 48);
    ctx.fillStyle = "#ffffff";
    ctx.font = "700 24px system-ui, sans-serif";
    ctx.fillText("0", cx - 8, cy + 8);
    return;
  }
  let start = -Math.PI / 2;
  items.forEach(([label, value, color], i) => {
    const angle = (value / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, r, start, start + angle);
    ctx.lineWidth = 28;
    ctx.strokeStyle = color;
    ctx.stroke();
    start += angle;
    ctx.fillStyle = "rgba(237, 246, 244, 0.86)";
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillText(`${label} ${Math.round((value / total) * 100)}%`, w * 0.68, 44 + i * 24);
  });
  ctx.fillStyle = "#ffffff";
  ctx.font = "700 24px system-ui, sans-serif";
  ctx.fillText(String(total), cx - 14, cy + 8);
}

function drawBarChart(id, labels, bars, line) {
  const c = canvasCtx(id);
  if (!c) return;
  const { ctx, w, h } = c;
  ctx.clearRect(0, 0, w, h);
  drawAxes(ctx, w, h);
  const max = Math.max(...bars, 100);
  const gap = 8;
  const bw = (w - 62) / bars.length - gap;
  bars.forEach((v, i) => {
    const x = 38 + i * (bw + gap);
    const bh = (v / max) * (h - 62);
    ctx.fillStyle = "rgba(37, 242, 180, 0.74)";
    ctx.fillRect(x, h - 28 - bh, bw, bh);
  });
  ctx.beginPath();
  line.forEach((v, i) => {
    const x = 38 + i * (bw + gap) + bw / 2;
    const y = h - 28 - (v / 100) * (h - 62);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#ff5c7a";
  ctx.lineWidth = 3;
  ctx.stroke();
}

function drawUsageArea(id, labels, tokens, sandboxes) {
  const scaledTokens = tokens.map((v) => Math.round(v / 1000));
  drawLineChart(id, labels, scaledTokens, sandboxes);
}

function dashboardWindowLabel() {
  return state.dashboardWindow === "day" ? "今日" : state.dashboardWindow === "month" ? "近 30 天" : "近 7 天";
}

function sum(values) {
  return values.reduce((a, b) => a + b, 0);
}

function avg(values) {
  return values.length ? Math.round(sum(values) / values.length) : 0;
}

function pct(part, total) {
  return `${Math.round((Number(part || 0) / Math.max(1, Number(total || 0))) * 100)}%`;
}

function maxPair(labels, values) {
  let idx = 0;
  values.forEach((v, i) => {
    if (v > values[idx]) idx = i;
  });
  return { label: labels[idx] || "-", value: values[idx] || 0 };
}

function dashboardTipFor(target) {
  const key = target.dataset.tipKey;
  const snap = state.dashboardSnapshot;
  if (!key || !snap) return target.dataset.tip || "";
  const label = snap.windowLabel;
  const peakAttempt = maxPair(snap.labels, snap.attempts);
  const peakSolved = maxPair(snap.labels, snap.solved);
  const peakSandbox = maxPair(snap.labels, snap.sandboxes);
  const topCategory = snap.categories.reduce((a, b) => (b[1] > a[1] ? b : a), snap.categories[0]);
  const lowCategory = snap.categories.reduce((a, b) => (b[1] < a[1] ? b : a), snap.categories[0]);
  const categoryTotal = snap.categories.reduce((total, item) => total + Number(item[1] || 0), 0);
  const tips = {
    running: `当前有 ${snap.running} 个 alive 任务，数据直接来自 GET /api/runs。`,
    queued: `当前有 ${snap.queued} 个 phase=queued 或 status=QUEUED 的任务。`,
    done: `${label} 状态为 DONE 的任务共 ${snap.doneCount} 个；其中 ${snap.solvedTotal} 个有 correct=true 平台提交。`,
    review: `待审任务 ${snap.review} 个：任务已 DONE，但事件账本里还没有 correct=true 提交。`,
    accuracy: `${label} 平台提交通过率 ${snap.accuracyRate}，通过 ${snap.approvedTotal} / 提交 ${snap.submitTotal}。`,
    throughput: `${label} 共创建 ${snap.attemptsTotal} 个任务、平台确认成功 ${snap.solvedTotal} 个；任务峰值 ${peakAttempt.label} 为 ${peakAttempt.value} 个。`,
    category: `${label} 场景占比最高为 ${topCategory[0]} ${pct(topCategory[1], categoryTotal)}，最低为 ${lowCategory[0]} ${pct(lowCategory[1], categoryTotal)}。可据此安排薄弱场景补测。`,
    "accuracy-chart": `提交峰值 ${peakAttempt.label} 为 ${peakAttempt.value} 次，平均正确率 ${avg(snap.accuracy)}%。提交量升高但正确率下降时，应收紧成果审核。`,
    usage: `${label} 模型用量 ${snap.tokenTotal} tokens，创建沙箱容器 ${sum(snap.sandboxes)} 次；峰值 ${peakSandbox.label} 为 ${peakSandbox.value} 次。`,
    funnel: `提交 ${snap.funnelCandidate} -> 证据完整 ${snap.funnelEvidence} -> 平台通过 ${snap.funnelApproved}，全部来自 events.jsonl。`,
    "funnel-candidate": `submission 事件共 ${snap.funnelCandidate} 条。`,
    "funnel-evidence": `证据完整 ${snap.funnelEvidence} 个，候选到证据完整率 ${pct(snap.funnelEvidence, snap.funnelCandidate)}。建议补齐步骤、命令和关键输出。`,
    "funnel-approved": `correct=true 的 submission 事件共 ${snap.funnelApproved} 条。`,
    "funnel-replan": `replan 事件共 ${snap.funnelReplan} 条。`,
  };
  return tips[key] || target.dataset.tip || "";
}

function setUsageBars(roleTokens, prefix) {
  const values = [roleTokens.planner, roleTokens.executor, roleTokens.evaluator, roleTokens.other];
  const max = Math.max(...values, 1);
  const keys = prefix === "quick" ? ["planner", "executor", "evaluator"] : ["planner", "executor", "evaluator", "other"];
  keys.forEach((key) => {
    const value = Number(roleTokens[key] || 0);
    const strong = $(`${prefix}-${key}-tokens`);
    const bar = $(`${prefix}-${key}-bar`);
    if (strong) strong.textContent = value.toLocaleString("zh-CN");
    if (bar) bar.style.setProperty("--w", `${Math.round((value / max) * 100)}%`);
  });
}

function formatCompactTokens(value) {
  const n = Number(value || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K`;
  return String(n);
}

function usageDailySeries(runs, days = 7) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const start = new Date(today);
  start.setDate(today.getDate() - (days - 1));
  const rows = Array.from({ length: days }, (_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    return {
      key: `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`,
      label: `${date.getMonth() + 1}/${date.getDate()}`,
      value: 0,
      runs: 0,
    };
  });
  const byDay = new Map(rows.map((row) => [row.key, row]));
  (runs || []).forEach((run) => {
    const at = timestamp(run.created_at);
    if (!at) return;
    const date = new Date(at);
    const key = `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
    const row = byDay.get(key);
    if (!row) return;
    row.value += Number(run.run_tokens || 0);
    row.runs += 1;
  });
  return rows;
}

function renderUsageDaily(runs) {
  const rows = usageDailySeries(runs);
  const max = Math.max(...rows.map((row) => row.value), 1);
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  $("usage-daily-total").textContent = total.toLocaleString("zh-CN");
  $("usage-daily-chart").innerHTML = rows.map((row) => {
    const height = row.value ? Math.max(8, Math.round((row.value / max) * 100)) : 0;
    const title = `${row.label}: ${row.value.toLocaleString("zh-CN")} tokens / ${row.runs} 个任务`;
    return `<div class="usage-daily-column" title="${escapeHtml(title)}">
      <strong>${escapeHtml(formatCompactTokens(row.value))}</strong>
      <div class="usage-daily-track"><i style="--h: ${height}%"></i></div>
      <span>${escapeHtml(row.label)}</span>
    </div>`;
  }).join("");
}

function renderUsageCategories(byCategory) {
  const rows = Array.from(byCategory.entries())
    .map(([label, item], index) => ({
      label: categoryLabel(label),
      value: Number(item.tokens || 0),
      runs: Number(item.count || 0),
      color: DASHBOARD_COLORS[index % DASHBOARD_COLORS.length],
    }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 7);
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  $("usage-category-total").textContent = formatCompactTokens(total);
  let cursor = 0;
  const stops = rows.filter((row) => row.value > 0).map((row) => {
    const start = cursor;
    cursor += (row.value / Math.max(1, total)) * 100;
    return `${row.color} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
  });
  $("usage-category-donut").style.background = stops.length
    ? `conic-gradient(${stops.join(", ")})`
    : "#e8eeee";
  $("usage-category-legend").innerHTML = rows.length ? rows.map((row) => `
    <div>
      <span><i style="--c: ${row.color}"></i>${escapeHtml(row.label)}</span>
      <strong>${row.value.toLocaleString("zh-CN")} <small>${row.runs} tasks</small></strong>
    </div>`).join("") : "<p class='meta'>暂无任务类型用量数据</p>";
}

function setUsageChartView(view) {
  const next = ["stage", "daily", "category"].includes(view) ? view : "stage";
  state.usageChartView = next;
  const copy = {
    stage: ["阶段分布", "按 Agent 阶段汇总 events.jsonl 中的真实模型 Tokens。"],
    daily: ["每日分布", "按任务创建日期汇总最近 7 日的真实模型 Tokens。"],
    category: ["任务类型分布", "按题目场景类型对真实模型 Tokens 进行占比统计。"],
  };
  $("usage-chart-title").textContent = copy[next][0];
  $("usage-chart-desc").textContent = copy[next][1];
  document.querySelectorAll(".usage-chart-tab").forEach((button) => {
    const active = button.dataset.usageChart === next;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  ["stage", "daily", "category"].forEach((name) => {
    $(`usage-${name}-view`).classList.toggle("hidden", name !== next);
  });
}

function formatRunDuration(milliseconds) {
  let seconds = Math.max(0, Math.floor(Number(milliseconds || 0) / 1000));
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  if (days) return `${days}天 ${String(hours).padStart(2, "0")}时 ${String(minutes).padStart(2, "0")}分`;
  if (hours) return `${hours}时 ${String(minutes).padStart(2, "0")}分 ${String(seconds).padStart(2, "0")}秒`;
  return `${minutes}分 ${String(seconds).padStart(2, "0")}秒`;
}

function updateRunDuration(snap, events = []) {
  const target = $("run-duration");
  if (!target || !snap) return;
  const eventTimes = events.map((event) => timestamp(event.ts)).filter(Boolean);
  const start = timestamp(snap.created_at) || (eventTimes.length ? Math.min(...eventTimes) : 0);
  if (!start) {
    target.textContent = "-";
    return;
  }
  const terminal = !snap.alive && ["DONE", "FAILED"].includes(String(snap.status || ""));
  const end = terminal && eventTimes.length ? Math.max(...eventTimes) : Date.now();
  target.textContent = formatRunDuration(Math.max(0, end - start));
}

function renderRuntimeOverview(data, runtime) {
  $("overview-active").textContent = String(data.running);
  $("overview-running").textContent = String(data.running);
  $("overview-queued").textContent = String(data.queued);
  $("overview-done").textContent = String(data.doneCount);
  $("overview-other").textContent = String(data.otherCount);
  const total = Math.max(1, data.running + data.queued + data.doneCount + data.otherCount);
  $("overview-progress").style.setProperty("--p", `${Math.round((data.running / total) * 100)}%`);
  const ready = runtime && runtime.ready === true;
  const host = runtime && runtime.host ? `${runtime.host}:${runtime.port || 22}` : "未配置 SSH";
  const image = runtime && runtime.image ? runtime.image : "未提供镜像";
  const error = runtime && runtime.error ? runtime.error : "";
  $("overview-runtime").innerHTML = [
    `<div><strong>${escapeHtml(host)}</strong><span class="state-pill ${ready ? "pass" : "neutral"}">${ready ? "ready" : "not ready"}</span><small>${escapeHtml(error || "SSH / Docker 运行时探测结果")}</small></div>`,
    `<div><strong>${escapeHtml(image)}</strong><span class="state-pill neutral">image</span><small>${escapeHtml((runtime && runtime.container_model) || "任务独立容器")}</small></div>`,
    `<div><strong>${data.running} 个活动容器任务</strong><span class="state-pill neutral">runs</span><small>容器创建事件 ${sum(data.sandboxes)} 条</small></div>`,
  ].join("");
}

function renderDashboard(data, runtime = {}) {
  const windowLabel = dashboardWindowLabel();
  $("chart-throughput-label").textContent = windowLabel;
  const solvedTotal = sum(data.solved);
  const attemptsTotal = sum(data.attempts);
  const accuracyRate = data.submitTotal ? pct(data.approvedTotal, data.submitTotal) : "-";
  $("dash-running").textContent = String(data.running);
  $("dash-queued").textContent = String(data.queued);
  $("dash-done").textContent = String(data.doneCount);
  $("dash-review").textContent = String(data.review);
  $("dash-accuracy").textContent = accuracyRate;
  const funnelValues = [data.funnelCandidate, data.funnelEvidence, data.funnelApproved, data.funnelReplan];
  const funnelMax = Math.max(...funnelValues, 1);
  ["candidate", "evidence", "approved", "replan"].forEach((key, i) => {
    const el = $(`funnel-${key}`);
    el.textContent = String(funnelValues[i]);
    el.parentElement.style.setProperty("--w", `${Math.round((funnelValues[i] / funnelMax) * 100)}%`);
  });
  state.dashboardSnapshot = {
    ...data,
    windowLabel,
    solvedTotal,
    attemptsTotal,
    tokenTotal: sum(data.tokens),
    accuracyRate,
  };
  drawLineChart("chart-throughput", data.labels, data.solved, data.attempts);
  drawDonutChart("chart-category", data.categories);
  drawBarChart("chart-accuracy", data.labels, data.attempts, data.accuracy);
  drawUsageArea("chart-usage", data.labels, data.tokens, data.sandboxes);
  renderRuntimeOverview(state.dashboardSnapshot, runtime);
  setUsageBars(data.roleTokens, "quick");
}

async function loadDashboard(reuse = false) {
  if (state.dashboardLoading) return;
  state.dashboardLoading = true;
  try {
    const telemetry = reuse && state.dashboardTelemetry ? state.dashboardTelemetry : await fetchTelemetry();
    state.dashboardTelemetry = telemetry;
    renderDashboard(buildDashboardData(telemetry, state.dashboardWindow), telemetry.runtime);
  } catch (e) {
    ["dash-running", "dash-queued", "dash-done", "dash-review", "dash-accuracy"].forEach((id) => { $(id).textContent = "!"; });
    $("overview-runtime").innerHTML = `<div><strong>运行数据读取失败</strong><span class="state-pill neutral">error</span><small>${escapeHtml(e.message)}</small></div>`;
  } finally {
    state.dashboardLoading = false;
  }
}

function startDashboard() {
  loadDashboard();
  if (state.dashboardTimer) clearInterval(state.dashboardTimer);
  state.dashboardTimer = setInterval(loadDashboard, 5000);
}

function bindDashboardTooltip() {
  const tip = $("dashboard-tooltip");
  const root = document.querySelector(".wizard-stage");
  if (!tip || !root) return;
  root.addEventListener("mousemove", (e) => {
    const target = e.target.closest("[data-tip]");
    if (!target) {
      tip.classList.add("hidden");
      return;
    }
    tip.textContent = dashboardTipFor(target);
    tip.classList.remove("hidden");
    tip.style.left = `${e.clientX + 16}px`;
    tip.style.top = `${e.clientY + 14}px`;
  });
  root.addEventListener("mouseleave", () => tip.classList.add("hidden"));
}

function categoryLabel(category) {
  return CTF_TYPE_LABEL[category] || category || "全部";
}

function renderSkillTabs(categories, active) {
  const tabs = ["", ...(categories || [])];
  $("skill-category-tabs").innerHTML = tabs.map((cat) => {
    const label = cat ? categoryLabel(cat) : "全部";
    const cls = cat === active ? " active" : "";
    return `<button type="button" class="skill-tab${cls}" data-category="${escapeHtml(cat)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(skillGlyph(cat))}</strong></button>`;
  }).join("");
}

function renderTypeTabs(targetId, active) {
  const tabs = ["", ...CTF_TYPE_NAV];
  $(targetId).innerHTML = tabs.map((cat) => {
    const label = cat ? categoryLabel(cat) : "全部";
    const cls = cat === active ? " active" : "";
    return `<button type="button" class="skill-tab${cls}" data-category="${escapeHtml(cat)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(skillGlyph(cat))}</strong></button>`;
  }).join("");
}

function renderValueTabs(targetId, values, active) {
  $(targetId).innerHTML = ["", ...values].map((value) => {
    const label = value || "全部";
    const cls = value === active ? " active" : "";
    const code = value ? value.slice(0, 3).toUpperCase() : "ALL";
    return `<button type="button" class="skill-tab${cls}" data-value="${escapeHtml(value)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(code)}</strong></button>`;
  }).join("");
}

function renderModuleCards({ items, filter = "", filterKey = "type", tabsId, activeId, listId, emptyText = "暂无数据" }) {
  const values = Array.from(new Set(items.map((it) => it[filterKey]).filter(Boolean)));
  renderValueTabs(tabsId, values, filter);
  $(activeId).textContent = filter || "全部";
  const shown = filter ? items.filter((it) => it[filterKey] === filter) : items;
  $(listId).innerHTML = shown.length ? shown.map((it) => {
    const badge = it.badge || "neutral";
    return `<article class="arsenal-card module-card" data-module-id="${escapeHtml(it.id)}" data-list-id="${escapeHtml(listId)}">
      <div class="skill-card-top">
        <span class="skill-type">${escapeHtml(it.type || "General")}</span>
        <span class="skill-kind">${escapeHtml(it.value || badge)}</span>
      </div>
      <div class="skill-card-main">
        <div class="skill-orbit">${escapeHtml((it.type || "MOD").slice(0, 3).toUpperCase())}</div>
        <div>
          <h3>${escapeHtml(it.title)}</h3>
          <p>${escapeHtml(it.desc)}</p>
        </div>
      </div>
      <div class="skill-card-meta">
        <span>状态</span>
        <strong>${escapeHtml(it.value || "ready")}</strong>
        <small>${escapeHtml(it.detail || it.desc || "")}</small>
      </div>
      <div class="skill-card-actions">
        <button type="button" class="skill-action primary" data-action="module-detail" data-id="${escapeHtml(it.id)}" data-list-id="${escapeHtml(listId)}">查看详情</button>
      </div>
    </article>`;
  }).join("") : `<div class="skill-card empty"><strong>${escapeHtml(emptyText)}</strong><span>切换其它分类查看。</span></div>`;
}

function moduleItemsByList(listId) {
  if (listId === "agent-role-cards") return state.agentRoleItems.length ? state.agentRoleItems : AGENT_ROLE_ITEMS;
  if (listId === "blackboard-cards") return state.blackboardItems.length ? state.blackboardItems : BLACKBOARD_FALLBACK;
  if (listId === "review-cards") return state.reviewItems.length ? state.reviewItems : REVIEW_ITEMS;
  if (listId === "usage-cards") return state.usageItems.length ? state.usageItems : USAGE_ITEMS;
  if (listId === "mcp-cards") return state.mcpItems.length ? state.mcpItems : MCP_FALLBACK;
  return [];
}

function openModuleDetail(listId, id) {
  const item = moduleItemsByList(listId).find((it) => it.id === id);
  if (!item) return;
  const prefix = {
    "agent-role-cards": "agent-role",
    "blackboard-cards": "blackboard",
    "review-cards": "review",
    "usage-cards": "usage",
    "mcp-cards": "mcp",
  }[listId];
  $(`${prefix}-detail`).classList.remove("hidden");
  $(`${prefix}-detail-title`).textContent = item.title;
  $(`${prefix}-detail-body`).textContent = JSON.stringify(item, null, 2);
}

function renderAgentRoles() {
  renderModuleCards({
    items: state.agentRoleItems.length ? state.agentRoleItems : AGENT_ROLE_ITEMS,
    filter: state.agentRole,
    tabsId: "agent-role-tabs",
    activeId: "agent-role-active",
    listId: "agent-role-cards",
  });
}

function renderBlackboard() {
  renderModuleCards({
    items: state.blackboardItems.length ? state.blackboardItems : BLACKBOARD_FALLBACK,
    filter: state.blackboardType,
    tabsId: "blackboard-tabs",
    activeId: "blackboard-active",
    listId: "blackboard-cards",
  });
}

function renderReviewCards() {
  renderModuleCards({
    items: state.reviewItems.length ? state.reviewItems : REVIEW_ITEMS,
    filter: state.reviewStatus,
    tabsId: "review-tabs",
    activeId: "review-active",
    listId: "review-cards",
  });
}

function renderUsageCards() {
  renderModuleCards({
    items: state.usageItems.length ? state.usageItems : USAGE_ITEMS,
    filter: state.usageView,
    tabsId: "usage-tabs",
    activeId: "usage-active",
    listId: "usage-cards",
  });
}

function renderMcpCards() {
  renderModuleCards({
    items: state.mcpItems.length ? state.mcpItems : MCP_FALLBACK,
    filter: state.mcpType,
    tabsId: "mcp-tabs",
    activeId: "mcp-active",
    listId: "mcp-cards",
  });
}

async function loadBlackboardStatus() {
  try {
    const data = await api("/api/experience");
    const items = Array.isArray(data.items) ? data.items : [];
    state.blackboardItems = items.length ? items.map((item, index) => ({
      id: String(item.id || item.key || `experience-${index + 1}`),
      type: String(item.type || item.topic || "经验"),
      title: String(item.title || item.summary || item.topic || `经验 ${index + 1}`),
      badge: "wired",
      value: String(item.outcome || item.status || "recorded"),
      desc: String(item.summary || item.description || "后端经验条目"),
      detail: JSON.stringify(item, null, 2),
    })) : [{
      ...BLACKBOARD_FALLBACK[0],
      detail: `endpoint: ${data.endpoint || "/api/experience"}\nwired: ${String(data.wired === true)}\nreserved: ${String(data.reserved === true)}\ncontract: ${data.contract || "未提供"}`,
    }];
  } catch (e) {
    state.blackboardItems = [{
      ...BLACKBOARD_FALLBACK[0],
      id: "experience-load-error",
      title: "经验状态读取失败",
      value: "error",
      desc: e.message,
    }];
  }
  renderBlackboard();
}

async function loadMcpStatus() {
  try {
    const data = await api("/api/capabilities");
    const layers = data.layers || [];
    const relevant = layers.filter((layer) => ["tools", "platform", "env_check"].includes(layer.id));
    state.mcpItems = [
      MCP_FALLBACK[0],
      ...relevant.map((layer) => ({
        id: `capability-${layer.id}`,
        type: "内部能力",
        title: layer.name,
        badge: layer.status || "neutral",
        value: CAP_LABEL[layer.status] || layer.status || "unknown",
        desc: layer.note || layer.contract || "后端能力声明",
        detail: `这是一项内部后端能力，不代表 MCP Server 已注册。\ncontract: ${layer.contract || "-"}\nimpl: ${layer.impl || "-"}`,
      })),
    ];
  } catch (e) {
    state.mcpItems = [{
      ...MCP_FALLBACK[0],
      id: "mcp-status-error",
      title: "能力状态读取失败",
      value: "error",
      desc: e.message,
    }];
  }
  renderMcpCards();
}

function runTitle(run) {
  return (run.task && (run.task.title || run.task.name)) || run.run_id;
}

function runCategory(run) {
  const type = run.task && (run.task.challenge_type_label || run.task.challenge_type);
  return type || "未识别场景";
}

function submissionForEvents(events = []) {
  const submissions = events.filter((event) => event.kind === "submission");
  return submissions.find((event) => event.detail && event.detail.correct === true)
    || [...submissions].reverse().find((event) => event.detail && event.detail.correct === false)
    || submissions[submissions.length - 1]
    || null;
}

async function loadReviewQueue() {
  try {
    const data = await api("/api/runs");
    const runs = (data.runs || []).slice(0, 12);
    const [products, eventResults] = await Promise.all([
      Promise.all(runs.map((run) =>
        api(`/api/runs/${encodeURIComponent(run.run_id)}/product`).catch(() => null)
      )),
      Promise.all(runs.map((run) =>
        fetchAllRunEvents(run.run_id).catch(() => [])
      )),
    ]);
    state.reviewItems = runs.map((run, i) => {
      const product = products[i] && products[i].product ? products[i].product : {};
      const productCount = Object.keys(product).length;
      const submissionEvent = submissionForEvents(eventResults[i] || []);
      const submission = (submissionEvent && submissionEvent.detail) || null;
      const platformPassed = Boolean(submission && submission.correct === true);
      let type = "待审核";
      let badge = "reserved";
      let title = `待复核：${runTitle(run)}`;
      let value = productCount ? `${productCount} 产物` : `${run.step_count || 0} steps`;
      let desc = "运行记录已生成，等待确认成果、证据链和复盘材料。";
      if (run.alive || !["DONE", "FAILED"].includes(run.status || "")) {
        type = "人工接管";
        badge = "neutral";
        title = `进行中：${runTitle(run)}`;
        desc = "任务仍在运行或未进入终态，可在 Agent 工作区继续观察工具调用与事件流。";
        value = run.status || "RUNNING";
      } else if (run.status === "DONE" && platformPassed) {
        type = "已通过";
        badge = "wired";
        title = `平台已通过：${runTitle(run)}`;
        desc = "事件账本存在 submission.correct = true；这是平台通过的唯一前端判定条件。";
        value = "correct: true";
      } else if (run.status === "DONE") {
        type = "待审核";
        badge = "reserved";
        desc = submission
          ? `任务已完成，但最近一次 submission.correct 为 ${String(submission.correct)}，不能标记平台通过。`
          : "任务已完成但没有 submission 事件，步骤产物不等于平台通过。";
      } else if (run.status === "FAILED") {
        type = "已驳回";
        badge = "neutral";
        title = `需重规划：${runTitle(run)}`;
        desc = run.fail_reason || "任务失败或证据不足，建议查看事件流、失败原因并决定是否续跑。";
      }
      return {
        id: run.run_id,
        type,
        title,
        badge,
        value,
        desc,
        detail: [
          `run_id: ${run.run_id}`,
          `状态: ${run.status || "-"}`,
          `场景: ${runCategory(run)}`,
          `tokens: ${run.run_tokens || 0}`,
          `步骤数: ${run.step_count || 0}`,
          `产物数: ${productCount}`,
          `submission: ${submission ? JSON.stringify({ ok: submission.ok, correct: submission.correct, message: submission.message }) : "无"}`,
          `创建时间: ${run.created_at || "-"}`,
          run.fail_reason ? `失败原因: ${run.fail_reason}` : "",
        ].filter(Boolean).join("\n"),
      };
    });
    renderReviewCards();
  } catch (e) {
    state.reviewItems = [{
      id: "review-load-error",
      type: "待审核",
      title: "审核队列加载失败",
      badge: "reserved",
      value: "error",
      desc: e.message,
      detail: "请确认本地前端服务仍在运行，且 /api/runs 可访问。",
    }];
    renderReviewCards();
  }
}

async function loadUsageSummary() {
  try {
    const telemetry = await fetchTelemetry();
    const runs = telemetry.runs || [];
    const events = Array.from(telemetry.eventMap.values()).flat();
    const roleTokens = roleTokenTotals(events);
    const totalTokens = runs.reduce((n, r) => n + Number(r.run_tokens || 0), 0);
    const done = runs.filter((r) => r.status === "DONE").length;
    const failed = runs.filter((r) => r.status === "FAILED").length;
    const active = runs.filter((r) => r.alive || !["DONE", "FAILED"].includes(r.status || "")).length;
    const byCategory = new Map();
    runs.forEach((run) => {
      const key = runCategory(run);
      const item = byCategory.get(key) || { count: 0, tokens: 0 };
      item.count += 1;
      item.tokens += Number(run.run_tokens || 0);
      byCategory.set(key, item);
    });
    const topCategory = Array.from(byCategory.entries()).sort((a, b) => b[1].tokens - a[1].tokens)[0];
    const llmEventCount = events.filter((event) => event.kind === "llm_usage").length;
    setUsageBars(roleTokens, "usage");
    renderUsageDaily(runs);
    renderUsageCategories(byCategory);
    setUsageChartView(state.usageChartView);
    state.usageItems = [
      {
        id: "overview",
        type: "总览",
        title: "真实运行总 Tokens",
        badge: "wired",
        value: String(totalTokens),
        desc: `${runs.length} 个任务，完成 ${done} 个，失败 ${failed} 个，运行中 ${active} 个。`,
        detail: `数据来源: GET /api/runs\n平均 tokens: ${runs.length ? Math.round(totalTokens / runs.length) : 0}\n最近任务: ${runs[0] ? runs[0].run_id : "无"}`,
      },
      {
        id: "stage",
        type: "阶段",
        title: "Agent 阶段 Token",
        badge: "wired",
        value: `${llmEventCount} usage events`,
        desc: "按 events.jsonl 的 llm_usage.detail.role 汇总 Planner、Executor、Evaluator 和其它角色。",
        detail: [
          `planner: ${roleTokens.planner}`,
          `executor: ${roleTokens.executor}`,
          `evaluator: ${roleTokens.evaluator}`,
          `other: ${roleTokens.other}`,
        ].join("\n"),
      },
      {
        id: "model",
        type: "模型",
        title: "模型策略成本",
        badge: "wired",
        value: runs.length ? "已接运行账本" : "无记录",
        desc: "运行账本提供角色 Token，但 llm_usage 未提供模型名称，因此不能伪造多模型成本拆分。",
        detail: `任务数: ${runs.length}\n总 tokens: ${totalTokens}\nllm_usage 事件: ${llmEventCount}\n完成率: ${pct(done, runs.length)}`,
      },
      {
        id: "challenge",
        type: "场景",
        title: "场景成本分布",
        badge: "wired",
        value: topCategory ? topCategory[0] : "无记录",
        desc: topCategory ? `${topCategory[0]} 累计 ${topCategory[1].tokens} tokens / ${topCategory[1].count} 个任务。` : "暂无可聚合的场景运行记录。",
        detail: Array.from(byCategory.entries()).map(([name, item]) => `${name}: ${item.count} runs / ${item.tokens} tokens`).join("\n") || "暂无运行记录",
      },
    ];
    renderUsageCards();
  } catch (e) {
    state.usageItems = [{
      id: "usage-load-error",
      type: "总览",
      title: "用量加载失败",
      badge: "reserved",
      value: "error",
      desc: e.message,
      detail: "请确认 /api/runs 可访问。",
    }];
    renderUsageCards();
  }
}

function inferToolCategory(tool) {
  const text = `${tool.tool_id || ""} ${tool.name || ""} ${tool.description || ""}`.toLowerCase();
  if (/(sqlmap|flask|requests|nmap|whois|dns|shodan|http|web|gobuster|ferox|dirsearch)/.test(text)) return "ctf-web";
  if (/(pwn|gdb|rop|pwntools|one_gadget|checksec|strace|ltrace|qemu|shellcode)/.test(text)) return "ctf-pwn";
  if (/(crypto|z3|sympy|gmpy|hashpump|fpylll|ecc|sage|rsatool)/.test(text)) return "ctf-crypto";
  if (/(reverse|angr|frida|qiling|radare|r2|ghidra|capstone|unicorn|lief|apk|jadx|binutils|objdump)/.test(text)) return "ctf-reverse";
  if (/(forensic|volatility|yara|pefile|oletools|binwalk|foremost|exif|tshark|sleuth|ffmpeg|steg|testdisk|pcap|pillow|scapy)/.test(text)) return "ctf-forensics";
  if (/(osint|whois|shodan|dns|geolocation)/.test(text)) return "ctf-osint";
  if (/(malware|cobalt|pefile|yara|dissect)/.test(text)) return "ctf-malware";
  if (/(ai|ml|model|numpy|matplotlib|adversarial)/.test(text)) return "ctf-ai-ml";
  return "ctf-misc";
}

function sandboxProfile(category, probe = {}) {
  const profiles = {
    "ctf-web": ["统一 SSH 沙箱", "HTTP 调试 / 扫描 / Web Exploit", "Web 工具视图"],
    "ctf-pwn": ["统一 SSH 沙箱", "GDB / Pwntools / QEMU / ROP", "二进制工具视图"],
    "ctf-crypto": ["统一 SSH 沙箱", "Sage / Z3 / PyCryptodome / LLL", "密码工具视图"],
    "ctf-reverse": ["统一 SSH 沙箱", "Ghidra / Radare2 / Frida / Emulation", "逆向工具视图"],
    "ctf-forensics": ["统一 SSH 沙箱", "Volatility / TShark / Binwalk / SleuthKit", "取证工具视图"],
    "ctf-misc": ["统一 SSH 沙箱", "PyJail / BashJail / Encoding / VM", "综合工具视图"],
    "ctf-osint": ["统一 SSH 沙箱", "DNS / Whois / Shodan / Media", "情报工具视图"],
    "ctf-malware": ["统一 SSH 沙箱", "YARA / PE / C2 / 动态样本", "恶意样本工具视图"],
    "ctf-ai-ml": ["统一 SSH 沙箱", "Notebook / Model Inspect / Adversarial", "AI 安全工具视图"],
  };
  const p = profiles[category] || profiles["ctf-misc"];
  return {
    category,
    image: p[0],
    stack: p[1],
    mode: p[2],
    needed: !!probe.needed,
    available: probe.available,
  };
}

function updatePinnedCount() {
  const count = document.querySelectorAll(".skill-card.pinned").length;
  if ($("skill-pinned-count")) $("skill-pinned-count").textContent = String(count);
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
  renderSkillTabs(data.categories || [], cat);
  state.skills = data.items;
  $("skill-active-category").textContent = cat || "全部";
  $("skill-total-count").textContent = String(data.items.length);
  $("skill-list").innerHTML = data.items.length ? data.items.map((it, index) => {
    const id = escapeHtml(it.doc_id);
    const kind = escapeHtml(it.kind || "doc");
    const category = escapeHtml(it.category || "General");
    const description = escapeHtml(it.description || "暂无描述，打开文档查看完整打法。");
    const chain = escapeHtml(skillChain(it.category, it.kind));
    return `<article class="skill-card" data-id="${id}">
      <div class="skill-card-top">
        <span class="skill-type">${category}</span>
        <span class="skill-kind">${kind}</span>
      </div>
      <div class="skill-card-main">
        <div class="skill-orbit">${escapeHtml(skillGlyph(it.category))}</div>
        <div>
          <h3>${id}</h3>
          <p>${description}</p>
        </div>
      </div>
      <div class="skill-card-meta">
        <span>打法链路</span>
        <strong>${chain}</strong>
        <small>优先级 ${String(index + 1).padStart(2, "0")} · 可挂载到单任务 Agent</small>
      </div>
      <div class="skill-card-actions">
        <button type="button" class="skill-action primary" data-action="open" data-id="${id}">查看打法</button>
        <button type="button" class="skill-action" data-action="agent" data-id="${id}">载入候选</button>
        <button type="button" class="skill-action" data-action="pin" data-id="${id}">常用</button>
        <button type="button" class="skill-action ghost" data-action="copy" data-id="${id}">复制 ID</button>
      </div>
    </article>`;
  }).join("") : `<div class="skill-card empty"><strong>没有匹配的技能</strong><span>换一个分类或关键词继续检索。</span></div>`;
  $("skill-status").textContent = q ? `检索「${q}」命中 ${data.items.length} 张战术卡片` : `${data.items.length} 张战术卡片已就绪`;
  const tools = await api("/api/tools");
  $("skill-tool-count").textContent = String((tools.manifest || []).length);
  state.tools = (tools.manifest || []).map((tool) => ({ ...tool, category: inferToolCategory(tool) }));
  $("tool-installer").textContent = `installer_path: ${tools.installer_path}`;
  renderToolMatrix();
  await loadSandboxMatrix();
  updatePinnedCount();
  if ($("btn-skill-pin-view").classList.contains("active")) {
    document.querySelectorAll(".skill-card").forEach((card) => {
      card.classList.toggle("dimmed", !card.classList.contains("pinned"));
    });
  }
}

function renderToolMatrix() {
  renderTypeTabs("tool-category-tabs", state.toolCategory);
  const items = state.toolCategory ? state.tools.filter((t) => t.category === state.toolCategory) : state.tools;
  $("tool-active-category").textContent = state.toolCategory ? categoryLabel(state.toolCategory) : "全部";
  $("tool-status").textContent = `${items.length} 个工具已按场景类型编组，真实执行时由 executor 从固定工具目录按需申请`;
  $("tool-list").innerHTML = items.length ? items.map((tool) => {
    const id = escapeHtml(tool.tool_id);
    const method = escapeHtml(tool.install_method || "manual");
    const check = escapeHtml(tool.verify_check || "manual verify");
    const category = escapeHtml(categoryLabel(tool.category));
    const desc = escapeHtml(tool.description || "暂无说明");
    return `<article class="arsenal-card tool-card" data-tool-id="${id}">
      <div class="skill-card-top">
        <span class="skill-type">${category}</span>
        <span class="skill-kind">${method}</span>
      </div>
      <div class="skill-card-main">
        <div class="skill-orbit">${escapeHtml(skillGlyph(tool.category))}</div>
        <div>
          <h3>${id}</h3>
          <p>${desc}</p>
        </div>
      </div>
      <div class="skill-card-meta">
        <span>校验项</span>
        <strong>${check}</strong>
        <small>${escapeHtml(skillChain(tool.category, method))}</small>
      </div>
      <div class="skill-card-actions">
        <button type="button" class="skill-action primary" data-action="tool-detail" data-id="${id}">查看详情</button>
        <button type="button" class="skill-action" data-action="tool-arm" data-id="${id}">加入工具链</button>
      </div>
    </article>`;
  }).join("") : `<div class="skill-card empty"><strong>该场景暂无工具</strong><span>切换到全部或其它场景查看。</span></div>`;
}

async function loadSandboxMatrix() {
  const r = await api("/api/sandbox");
  const runtime = await api("/api/sandbox/runtime").catch(() => ({}));
  const probes = r.categories || {};
  state.sandboxItems = CTF_TYPE_NAV.map((category) => ({
    ...sandboxProfile(category, probes[category] || {}),
    runtime,
  }));
  renderSandboxMatrix(r.note || "");
  updateSandboxRuntimeStatus(runtime);
}

function renderSandboxMatrix(note = "") {
  renderTypeTabs("sandbox-category-tabs", state.sandboxCategory);
  const items = state.sandboxCategory ? state.sandboxItems.filter((s) => s.category === state.sandboxCategory) : state.sandboxItems;
  $("sandbox-active-category").textContent = state.sandboxCategory ? categoryLabel(state.sandboxCategory) : "全部";
  $("sandbox-status").textContent = note || `${items.length} 类攻防场景工具视图已加载；真实执行共用 SSH 沙箱`;
  $("sandbox-report").innerHTML = items.map((s) => {
    const status = s.needed ? (s.available ? "运行时可用" : "运行时缺失") : "通用/待扩展";
    const cls = s.needed ? (s.available ? "ready" : "missing") : "planned";
    return `<article class="arsenal-card sandbox-card ${cls}" data-sandbox-category="${escapeHtml(s.category)}">
      <div class="skill-card-top">
        <span class="skill-type">${escapeHtml(categoryLabel(s.category))}</span>
        <span class="skill-kind">${escapeHtml(status)}</span>
      </div>
      <div class="skill-card-main">
        <div class="skill-orbit">${escapeHtml(skillGlyph(s.category))}</div>
        <div>
          <h3>${escapeHtml(s.image)}</h3>
          <p>${escapeHtml(s.stack)}</p>
        </div>
      </div>
      <div class="skill-card-meta">
        <span>沙箱策略</span>
        <strong>${escapeHtml(s.mode)}</strong>
        <small>统一 SSH 沙箱 · 独立工作目录 · 结束后可销毁</small>
      </div>
      <div class="skill-card-actions">
        <button type="button" class="skill-action primary" data-action="sandbox-detail" data-id="${escapeHtml(s.category)}">查看详情</button>
        <button type="button" class="skill-action" data-action="sandbox-probe" data-id="${escapeHtml(s.category)}">运行时状态</button>
      </div>
    </article>`;
  }).join("");
}

async function probeSandbox() {
  $("sandbox-status").textContent = "探测中…";
  try {
    await loadSandboxMatrix();
    const runtime = await api("/api/sandbox/runtime?probe=1");
    state.sandboxItems = state.sandboxItems.map((item) => ({ ...item, runtime }));
    updateSandboxRuntimeStatus(runtime);
    $("sandbox-status").textContent = "SSH 沙箱运行时探测完成";
  } catch (e) {
    $("sandbox-status").textContent = e.message;
  }
}

function updateSandboxRuntimeStatus(runtime = {}) {
  if ($("platform-sandbox-status")) {
    $("platform-sandbox-status").textContent = runtime.configured ? "已配置" : "未配置";
  }
  if ($("platform-sandbox-image")) {
    $("platform-sandbox-image").textContent = runtime.image || "SANDBOX_IMAGE";
  }
}

function openToolDetail(id) {
  const tool = state.tools.find((t) => t.tool_id === id);
  if (!tool) return;
  $("tool-detail").classList.remove("hidden");
  $("tool-detail-title").textContent = `${tool.tool_id} · ${categoryLabel(tool.category)}`;
  $("tool-detail-body").textContent = JSON.stringify({
    tool_id: tool.tool_id,
    name: tool.name,
    category: categoryLabel(tool.category),
    install_method: tool.install_method,
    install_command: tool.install_command,
    verify_check: tool.verify_check || "manual",
    description: tool.description,
    agent_usage: skillChain(tool.category, tool.install_method),
    alt_methods: tool.alt_methods || [],
  }, null, 2);
}

function openSandboxDetail(category) {
  const item = state.sandboxItems.find((s) => s.category === category);
  if (!item) return;
  $("sandbox-detail").classList.remove("hidden");
  $("sandbox-detail-title").textContent = `${categoryLabel(item.category)} 工具视图`;
  $("sandbox-detail-body").textContent = JSON.stringify({
    category: item.category,
    runtime_model: item.image,
    stack: item.stack,
    sandbox_mode: "统一 SSH 沙箱；场景类型仅用于工具筛选",
    declared_in_backend: item.needed,
    runtime_available: item.available,
    runtime: item.runtime || {},
    lifecycle: ["命令行指定 --executor real", "连接可 SSH 沙箱", "创建独立工作目录", "executor 按任务申请工具", "回收并销毁临时工作区"],
  }, null, 2);
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
    const layers = r.layers || [];
    $("cap-list").innerHTML = layers.map((L) =>
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
    const byId = new Map(layers.map((layer) => [layer.id, layer]));
    const radar = {
      understand: byId.get("understand"),
      planner: byId.get("planner"),
      executor: byId.get("executor"),
      evaluator: byId.get("evaluator_step"),
    };
    const score = { wired: 100, wired_declare: 75, stub: 35, reserved: 0, frontend_reserved: 0 };
    Object.entries(radar).forEach(([key, layer]) => {
      const bar = $(`radar-${key}`);
      const label = $(`radar-${key}-status`);
      if (bar) bar.style.setProperty("--v", `${score[layer && layer.status] || 0}%`);
      if (label) label.textContent = layer
        ? `${CAP_LABEL[layer.status] || layer.status} · ${layer.impl || "无实现"}`
        : "后端未声明";
    });
    const roleLayer = {
      intake: byId.get("platform"),
      understander: byId.get("understand"),
      planner: byId.get("planner"),
      executor: byId.get("executor"),
      evaluator: byId.get("evaluator_step"),
    };
    state.agentRoleItems = AGENT_ROLE_ITEMS.map((item) => {
      const layer = roleLayer[item.id];
      return layer ? {
        ...item,
        badge: layer.status,
        value: CAP_LABEL[layer.status] || layer.status,
        desc: layer.note || item.desc,
        detail: `${item.detail}\n\n后端声明\ncontract: ${layer.contract || "-"}\nimpl: ${layer.impl || "-"}\nstatus: ${layer.status || "-"}`,
      } : { ...item, badge: "reserved", value: "未声明" };
    });
    renderAgentRoles();
    $("cap-status").textContent = `${layers.length} 个能力层，状态来自 GET /api/capabilities`;
  } catch (e) {
    state.agentRoleItems = AGENT_ROLE_ITEMS.map((item) => ({
      ...item,
      badge: "reserved",
      value: "能力状态不可用",
      desc: "无法读取 /api/capabilities，未展示本地预设状态。",
      detail: e.message,
    }));
    renderAgentRoles();
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
        challenge_id: $("flag-challenge-id") ? $("flag-challenge-id").value.trim() : "",
        run_id: $("flag-run-id").value.trim() || state.runId || null,
        real_submit: $("flag-real-submit") ? $("flag-real-submit").checked : false,
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
  $("skill-status").textContent = `已打开 ${d.doc_id} 的完整打法文档`;
}

async function handleSkillAction(action, id, button) {
  if (action === "open") {
    await openSkill(id);
    return;
  }
  if (action === "copy") {
    await navigator.clipboard.writeText(id);
    $("skill-status").textContent = `已复制技能 ID：${id}`;
    return;
  }
  if (action === "pin") {
    const card = button.closest(".skill-card");
    card.classList.toggle("pinned");
    updatePinnedCount();
    $("skill-status").textContent = card.classList.contains("pinned") ? `已标记常用：${id}` : `已取消常用：${id}`;
    return;
  }
  if (action === "agent") {
    button.classList.add("armed");
    button.textContent = "已载入";
    $("skill-status").textContent = `已加入当前攻防任务候选能力：${id}`;
  }
}

async function exportSkillList() {
  const payload = state.skills.map((it) => ({
    doc_id: it.doc_id,
    category: it.category,
    kind: it.kind,
    description: it.description,
  }));
  await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
  $("skill-status").textContent = `已导出 ${payload.length} 张战术卡片到剪贴板`;
}

function togglePinnedView() {
  const btn = $("btn-skill-pin-view");
  const active = !btn.classList.contains("active");
  btn.classList.toggle("active", active);
  document.querySelectorAll(".skill-card").forEach((card) => {
    card.classList.toggle("dimmed", active && !card.classList.contains("pinned"));
  });
  $("skill-status").textContent = active ? "已高亮常用战术卡片" : "已显示全部战术卡片";
}

function focusRun(runId) {
  state.runId = runId;
  state.signalAfter = 0;
  state.eventAfter = 0;
  state.runSnapshot = null;
  state.runEvents = [];
  state.runLog = "";
  $("signal-log").textContent = "";
  $("event-timeline").innerHTML = "";
  $("tool-stream").textContent = "";
  $("run-log").textContent = "正在读取运行日志…";
  $("run-duration").textContent = "-";
  renderWorkspace(null, []);
  if (state.pollTimer) clearInterval(state.pollTimer);
  pollRun();
  state.pollTimer = setInterval(pollRun, 900);
}

async function pollRun() {
  if (!state.runId) return;
  try {
    const snap = await api("/api/runs/" + encodeURIComponent(state.runId));
    state.runSnapshot = snap;
    $("run-id").textContent = snap.run_id;
    $("run-status").textContent = snap.status + (snap.alive ? " · live" : "");
    $("run-mode").textContent = snap.execution_mode === "real"
      ? `真实 · ${snap.actors || 1} Agent`
      : "演示 · Mock";
    $("run-phase").textContent = snap.phase || "-";
    $("run-runtime").textContent = snap.runtime
      ? `${snap.runtime.host}:${snap.runtime.port} · ${snap.runtime.image}`
      : (snap.execution_mode === "real" ? "等待 VM" : "不使用 VM");
    $("run-step").textContent = snap.current_step || "-";
    $("run-tokens").textContent = snap.run_tokens || 0;
    $("run-message").textContent = snap.task && snap.task.title
      ? `${snap.task.title} — ${snap.task.description || ""}`
      : "运行中";
    $("run-fail").textContent = snap.fail_reason || snap.error || "";
    $("run-controls").classList.toggle("hidden", !snap.alive);
    renderPills(snap.status);
    renderDag($("dag-list"), snap.blueprint, snap.steps);
    const task = snap.task || {};
    const taskTitle = task.title || task.name || snap.run_id;
    const taskType = task.challenge_type || "ctf-misc";
    $("workspace-title").textContent = taskTitle;
    $("workspace-subtitle").textContent = `${categoryLabel(taskType)} · run_id ${snap.run_id} · 数据来自 state.json 与 events.jsonl`;
    $("workspace-type-badge").textContent = categoryLabel(taskType);
    const taskTab = $(`task-tab-${snap.run_id}`);
    if (taskTab) taskTab.querySelector("span").textContent = taskTitle;

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
      if (!state.runEvents.some((known) => known._i === e._i)) state.runEvents.push(e);
      const li = document.createElement("li");
      li.textContent = `${e.ts || ""} ${e.agent || ""} ${e.kind}${e.step_id ? " " + e.step_id : ""}${e.verdict ? " → " + e.verdict : ""}`;
      $("event-timeline").appendChild(li);
      const detail = e.detail && Object.keys(e.detail).length
        ? `\n${JSON.stringify(e.detail, null, 2)}`
        : "";
      const executionLine = `[${e.ts || ""}] ${e.kind}${e.step_id ? ` · ${e.step_id}` : ""}${detail}\n\n`;
      $("tool-stream").textContent = ($("tool-stream").textContent + executionLine).slice(-200000);
    });
    $("tool-stream").scrollTop = $("tool-stream").scrollHeight;

    const log = await api(`/api/runs/${encodeURIComponent(state.runId)}/log?tail=1000`);
    state.runLog = log.log || "";
    $("run-log").textContent = state.runLog;
    $("run-log").scrollTop = $("run-log").scrollHeight;
    updateRunDuration(snap, state.runEvents);
    renderWorkspace(snap, state.runEvents);

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
  if ($("task-console-total")) $("task-console-total").textContent = String(runs.length);
  if ($("task-console-running")) $("task-console-running").textContent = String(runs.filter((run) => run.alive).length);
  if ($("task-console-queued")) $("task-console-queued").textContent = String(runs.filter((run) => run.phase === "queued" || run.status === "QUEUED").length);
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

function applySampleTask(key) {
  const sample = SAMPLE_TASKS[key];
  if (!sample) return;
  setSource("text");
  $("task-title").value = sample.title;
  $("task-desc").value = sample.description;
  $("task-cid").value = sample.id;
  $("task-goals").value = "";
  $("task-type-override").value = sample.type;
  invalidateParse();
  $("start-hint").textContent = `已载入${categoryLabel(sample.type)}样例，请点击「识别攻防任务」。`;
}

function invalidateParse() {
  state.parsedTask = null;
  $("btn-start").disabled = true;
  $("type-box").classList.add("hidden");
  $("start-hint").textContent = "来源已变更，请重新「解析场景」。";
}

function challengeVisualProfile(type) {
  const key = String(type || "ctf-misc");
  const profiles = {
    "ctf-web": { accent: "WEB", scene: "Web 安全任务" },
    "ctf-pwn": { accent: "PWN", scene: "二进制攻防任务" },
    "ctf-crypto": { accent: "CRY", scene: "密码安全任务" },
    "ctf-reverse": { accent: "REV", scene: "逆向分析任务" },
    "ctf-forensics": { accent: "FOR", scene: "取证分析任务" },
  };
  return profiles[key] || { accent: skillGlyph(key), scene: categoryLabel(key) };
}

function displayField(value) {
  if (value === undefined || value === null || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderChallengeDossier(data) {
  const task = data.task || {};
  const c = data.classification || {};
  const type = task.challenge_type || c.primary || "ctf-misc";
  const profile = challengeVisualProfile(type);
  const title = task.title || task.name || $("task-title").value.trim() || "未命名攻防任务";
  const desc = task.description || $("task-desc").value.trim() || "暂无任务情报描述";
  const attachments = task.files || task.attachments || state.attachments || [];
  const fields = [
    ["平台任务 ID", task.friendly_id || task.id || task.challenge_id],
    ["分类", task.category || task.challenge_type_label || categoryLabel(type)],
    ["难度", task.difficulty],
    ["分值", task.points],
    ["本地目录", task.challenge_dir || task.workdir],
    ["远程目标", task.target_url || task.target || task.access],
    ["动态靶机", task.has_container === undefined ? null : (task.has_container ? "需要" : "不需要")],
    ["成果判定", task.flag_format || "由平台 submission 返回值判定"],
    ["识别置信度", c.confidence ?? task.type_confidence],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");
  const goals = Array.isArray(task.goals) ? task.goals
    : Array.isArray(task.goal_list) ? task.goal_list
      : task.task_goal ? [task.task_goal] : [];
  $("challenge-dossier").innerHTML = `<div class="challenge-dossier-hero ${escapeHtml(type)}">
    <div class="dossier-mark">${escapeHtml(profile.accent)}</div>
    <div><span>${escapeHtml(profile.scene)}</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(desc)}</p></div>
  </div>
  <div class="dossier-grid">
    ${fields.map(([k, v]) => `<div><span>${escapeHtml(k)}</span><strong>${escapeHtml(displayField(v))}</strong></div>`).join("")}
  </div>
  <div class="dossier-process">${goals.length
    ? goals.map((goal, i) => `<div><span>${i + 1}</span><strong>${escapeHtml(displayField(goal.description || goal.id || goal))}</strong></div>`).join("")
    : "<div><span>·</span><strong>派发后由真实 Planner 生成 DAG</strong></div>"}</div>
  <div class="dossier-files">${(attachments.length ? attachments : ["无附件"]).map((a) => {
    const name = typeof a === "string" ? a : (a.name || a.filename || a.path || "attachment");
    const size = typeof a === "object" && a.size !== undefined ? `${a.size} B` : "平台任务输入";
    return `<div><strong>${escapeHtml(name)}</strong><span>${escapeHtml(size)}</span></div>`;
  }).join("")}</div>`;
}

function setTaskPage(page, runId = state.runId) {
  state.taskPage = page;
  document.querySelectorAll(".challenge-tab").forEach((b) => {
    const active = page === "workspace"
      ? b.dataset.taskPage === "workspace" && b.dataset.runId === runId
      : b.dataset.taskPage === page;
    b.classList.toggle("active", active);
  });
  document.querySelectorAll(".challenge-page").forEach((p) => {
    p.classList.toggle("hidden", p.dataset.taskPage !== page);
    p.classList.toggle("active", p.dataset.taskPage === page);
  });
}

function compactText(value, max = 1600) {
  if (value === undefined || value === null || value === "") return "";
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.length > max ? `${text.slice(0, max)}\n…（完整内容见上方 events.jsonl / 命令输出）` : text;
}

function workspaceDecisionSummary(event) {
  const detail = event.detail || {};
  if (event.kind === "replan") return detail.reason || detail.changes || "DAG 已更新";
  if (event.kind === "plan_note") return detail.opinion || detail.observation || "Planner 已记录计划意见";
  if (event.kind.startsWith("plan_review")) return detail.reason || detail.opinion || event.kind;
  if (event.kind === "step_record") return detail.observation || detail.result || `步骤结果 ${event.verdict || "已记录"}`;
  if (event.kind === "goal_eval") return `${detail.goal_id || "目标"}: ${detail.complete === true ? "完成" : "未完成"}${detail.reasoning ? `\n${detail.reasoning}` : ""}`;
  if (event.kind === "submission") return `平台提交: ok=${String(detail.ok)} · correct=${String(detail.correct)}${detail.message ? `\n${detail.message}` : ""}`;
  if (event.kind === "llm_usage") return `${detail.role || event.agent || "LLM"}: ${detail.total_tokens || 0} tokens · ${detail.latency_ms || 0} ms`;
  return detail.reasoning || detail.reason || detail.opinion || detail.observation || detail.error || detail.message || detail.result || event.kind;
}

function activeStepFromEvents(snap, events = []) {
  if (!snap || !snap.alive) return null;
  const steps = (snap.blueprint && snap.blueprint.steps) || {};
  const candidate = snap.current_step;
  const candidateResult = candidate && (snap.steps || {})[candidate];
  const candidateStatus = String((candidateResult && (candidateResult.verdict || candidateResult.status)) || steps[candidate]?.status || "PENDING").toLowerCase();
  if (candidate && steps[candidate] && !["pass", "passed", "done", "fail", "failed", "escalate", "skipped"].includes(candidateStatus)) return candidate;
  const completed = new Set();
  const activityKinds = new Set(["use_tool", "tool_result", "sandbox.exec", "sandbox.run_python", "sandbox.sync", "step_eval"]);
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.kind === "step_record" && event.step_id) {
      completed.add(event.step_id);
      continue;
    }
    if (event.step_id && steps[event.step_id] && activityKinds.has(event.kind) && !completed.has(event.step_id)) {
      return event.step_id;
    }
  }
  return null;
}

function renderWorkspaceProcess(snap, events = []) {
  const steps = (snap && snap.blueprint && snap.blueprint.steps) || {};
  const results = (snap && snap.steps) || {};
  const ids = Object.keys(steps);
  if (!ids.length) {
    $("workspace-process-map").innerHTML = "<div class='process-node pending'><span>·</span><strong>等待真实 DAG</strong><small>Planner 写入 blueprint 后自动展示。</small></div>";
    return;
  }
  $("workspace-process-map").innerHTML = ids.map((id, i) => {
    const step = steps[id] || {};
    const result = results[id] || {};
    const status = String(result.verdict || step.status || "PENDING").toUpperCase();
    const normalized = status.toLowerCase();
    const activeId = activeStepFromEvents(snap, events);
    const isActive = id === activeId && !["pass", "passed", "done", "fail", "failed", "escalate", "skipped"].includes(normalized);
    const cls = isActive ? "active"
      : ["pass", "passed", "done"].includes(normalized) ? "passed"
        : ["fail", "failed", "escalate", "retry"].includes(normalized) ? "failed" : "pending";
    const label = isActive ? `${status} · 当前执行` : status;
    return `<div class="process-node ${cls}"${isActive ? ' aria-current="step"' : ""}><span>${i + 1}</span><strong>${escapeHtml(id)} · ${escapeHtml(label)}</strong><small>${escapeHtml(step.instruction || "未提供步骤说明")}</small></div>`;
  }).join("");
}

function renderWorkspaceDecisions(events) {
  const kinds = new Set([
    "engine.run_started", "replan", "plan_note", "plan_review", "plan_review_pass", "plan_review_fail",
    "step_eval", "step_record", "goal_eval", "reflect", "submission", "runtime_failed", "llm_usage",
  ]);
  const decisions = events.filter((event) => kinds.has(event.kind)).slice(-80);
  $("workspace-decisions").innerHTML = decisions.length ? decisions.map((event) => {
    const verdict = event.verdict ? ` · ${event.verdict}` : "";
    return `<li class="workspace-decision">
      <div><span>${escapeHtml(event.ts || "")}</span><code>${escapeHtml(event.agent || "system")} · ${escapeHtml(event.kind)}${escapeHtml(verdict)}</code></div>
      <strong>${escapeHtml(event.step_id || event.node_id || "run")}</strong>
      <p>${escapeHtml(compactText(workspaceDecisionSummary(event)))}</p>
    </li>`;
  }).join("") : "<li class='workspace-empty'><span>暂无决策事件</span><strong>等待 Planner、Executor 或 Evaluator 写入事件账本。</strong></li>";
}

function renderWorkspaceTools(events) {
  const calls = new Map();
  events.filter((event) => ["use_tool", "tool_result"].includes(event.kind)).forEach((event) => {
    const detail = event.detail || {};
    const name = String(detail.tool || detail.tool_id || "unknown_tool");
    const item = calls.get(name) || { name, callCount: 0, resultCount: 0, failures: 0, latest: "" };
    if (event.kind === "use_tool") item.callCount += 1;
    if (event.kind === "tool_result") {
      item.resultCount += 1;
      const output = detail.output !== undefined ? detail.output : detail.result;
      item.latest = compactText(output, 800);
      if ((output && typeof output === "object" && output.ok === false) || /error|failed|not found/i.test(item.latest)) item.failures += 1;
    }
    calls.set(name, item);
  });
  const runtimeEvents = events.filter((event) => event.kind.startsWith("sandbox."));
  const items = Array.from(calls.values());
  $("workspace-tools-empty").classList.toggle("hidden", Boolean(items.length || runtimeEvents.length));
  $("workspace-tools").innerHTML = [
    ...items.map((item) => `<div class="workspace-tool ${item.failures ? "has-failure" : ""}">
      <strong>${escapeHtml(item.name)}</strong><span class="state-pill ${item.failures ? "neutral" : "pass"}">${item.resultCount}/${item.callCount} results</span>
      <small>${item.failures} failed · latest output</small><pre>${escapeHtml(item.latest || "工具尚未返回结果")}</pre>
    </div>`),
    ...(runtimeEvents.length ? (() => {
      const latest = runtimeEvents[runtimeEvents.length - 1];
      return [`<div class="workspace-tool"><strong>SSH / Sandbox</strong><span class="state-pill pass">${runtimeEvents.length} events</span><small>${escapeHtml(latest.kind)}</small><pre>${escapeHtml(compactText(latest.detail, 800))}</pre></div>`];
    })() : []),
  ].join("");
}

function renderWorkspaceOutputs(snap, events) {
  if (!snap) {
    $("workspace-outputs").textContent = "正在读取 state.json 与 events.jsonl…";
    return;
  }
  const task = snap.task || {};
  const blueprintSteps = (snap.blueprint && snap.blueprint.steps) || {};
  const stepOutputs = Object.fromEntries(Object.entries(blueprintSteps).map(([id, step]) => {
    const result = (snap.steps || {})[id] || {};
    return [id, {
      status: result.verdict || step.status || null,
      attempts: result.attempts ?? step.attempts ?? 0,
      instruction: step.instruction || null,
      criterion: step.criterion || null,
      observation: result.observation || null,
      result: result.result ?? step.result ?? null,
    }];
  }));
  const submissionEvent = submissionForEvents(events);
  const counts = events.reduce((acc, event) => {
    acc[event.kind] = (acc[event.kind] || 0) + 1;
    return acc;
  }, {});
  $("workspace-outputs").textContent = JSON.stringify({
    run: {
      run_id: snap.run_id,
      status: snap.status,
      execution_mode: snap.execution_mode,
      phase: snap.phase,
      tokens: snap.run_tokens || 0,
    },
    task: {
      id: task.id || task.challenge_id || null,
      friendly_id: task.friendly_id || null,
      title: task.title || task.name || null,
      description: task.description || null,
      challenge_type: task.challenge_type || null,
      challenge_dir: task.challenge_dir || null,
      target: task.target || task.target_url || task.access || null,
      files: task.files || task.attachments || [],
      artifacts: task.artifacts || [],
    },
    goals: snap.goal_list || [],
    steps: stepOutputs,
    submission: submissionEvent ? submissionEvent.detail : null,
    event_counts: counts,
    backend_limitations: ["当前接口未提供 VM 工作目录文件枚举或文件下载。", "LLM 隐藏推理不会返回；这里只展示模型显式写入事件账本的计划理由、验收意见和结果。"],
  }, null, 2);
}

function renderWorkspace(snap, events) {
  renderWorkspaceProcess(snap, events || []);
  renderWorkspaceDecisions(events || []);
  renderWorkspaceTools(events || []);
  renderWorkspaceOutputs(snap, events || []);
}

function openTaskWorkspaceTab(runId, task) {
  const label = (task && task.title) || runId;
  if (!$(`task-tab-${runId}`)) {
    $("challenge-browser-tabs").insertAdjacentHTML("beforeend", `<button type="button" class="challenge-tab" id="task-tab-${escapeHtml(runId)}" data-task-page="workspace" data-run-id="${escapeHtml(runId)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(skillGlyph(task && task.challenge_type))}</strong></button>`);
  }
  $("workspace-title").textContent = label;
  $("workspace-subtitle").textContent = `${categoryLabel(task && task.challenge_type)} · run_id ${runId} · 正在读取真实运行账本`;
  $("workspace-type-badge").textContent = categoryLabel(task && task.challenge_type);
  renderWorkspace(null, []);
  setTaskPage("workspace", runId);
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
    body.title = $("task-file-title").value.trim() || "附件任务";
    body.description = $("task-file-desc").value.trim();
    if (!body.attachments.length) throw new Error("请选择附件");
  }
  return body;
}

function updatePlatformCards(data = {}) {
  const cache = data.cache || {};
  if ($("platform-config-status")) {
    $("platform-config-status").textContent = data.configured ? "会话已配置" : "缺少登录态";
  }
  if ($("platform-base-url")) $("platform-base-url").textContent = data.base_url || "PLATFORM_SESSION_BASE";
  if ($("platform-challenge-count")) $("platform-challenge-count").textContent = String(data.challenge_count ?? 0);
  if ($("platform-store-dir")) $("platform-store-dir").textContent = data.store_dir || "本地任务库";
  if ($("platform-cache-size")) $("platform-cache-size").textContent = formatBytes(cache.total_bytes || cache.size_bytes || 0);
  if ($("platform-cache-count")) $("platform-cache-count").textContent = `${cache.file_count || cache.count || cache.files || 0} files`;
}

async function loadPlatformStatus() {
  if (!$("platform-status")) return;
  $("platform-status").textContent = "正在检测平台与沙箱运行时…";
  try {
    const [platform, sandbox] = await Promise.all([
      api("/api/platform/status"),
      api("/api/sandbox/runtime").catch((e) => ({ error: e.message })),
    ]);
    updatePlatformCards(platform);
    updateSandboxRuntimeStatus(sandbox);
    $("platform-out").classList.remove("hidden");
    $("platform-out").textContent = JSON.stringify({ platform, sandbox }, null, 2);
    $("platform-status").textContent =
      `平台 ${platform.configured ? "已配置" : "未配置"} · 索引 ${platform.challenge_count || 0} 个任务 · 缓存 ${formatBytes((platform.cache || {}).total_bytes || 0)}`;
  } catch (e) {
    $("platform-status").textContent = e.message;
  }
}

async function syncPlatformChallenges() {
  $("platform-status").textContent = "正在同步平台索引…";
  try {
    const r = await api("/api/platform/sync", { method: "POST", body: JSON.stringify({}) });
    updatePlatformCards(r.status || {});
    $("platform-out").classList.remove("hidden");
    $("platform-out").textContent = JSON.stringify(r, null, 2);
    $("platform-status").textContent = "平台索引同步完成。";
  } catch (e) {
    $("platform-status").textContent = e.message;
  }
}

async function fetchPlatformChallenge() {
  const source = ($("platform-source").value || "").trim();
  const dest = ($("platform-dest").value || "").trim();
  if (!source) {
    $("platform-status").textContent = "请填写平台任务 URL / friendly_id / JSON / 本地 JSON 路径";
    return;
  }
  $("platform-status").textContent = "正在拉取、物化并识别攻防任务…";
  try {
    const r = await api("/api/platform/fetch", {
      method: "POST",
      body: JSON.stringify({ source, dest_dir: dest || undefined }),
    });
    updatePlatformCards(r.status || {});
    $("platform-out").classList.remove("hidden");
    $("platform-out").textContent = JSON.stringify(r, null, 2);
    showClassification(r.understood);
    $("platform-status").textContent = `已物化到本地任务目录：${r.challenge_dir}`;
  } catch (e) {
    $("platform-status").textContent = e.message;
  }
}

async function understandLocalChallenge() {
  const value = ($("local-challenge-dir").value || "").trim();
  if (!value) {
    $("platform-status").textContent = "请填写本地任务目录或 metadata.yml 路径";
    return;
  }
  $("platform-status").textContent = "正在用真实任务理解器分析本地任务…";
  try {
    const key = value.endsWith(".yml") || value.endsWith(".yaml") || value.endsWith(".json") ? "metadata_path" : "challenge_dir";
    const r = await api("/api/challenge/understand", {
      method: "POST",
      body: JSON.stringify({ [key]: value }),
    });
    $("platform-out").classList.remove("hidden");
    $("platform-out").textContent = JSON.stringify(r, null, 2);
    showClassification(r);
    $("platform-status").textContent = `真实理解完成 · ${r.classification && (r.classification.label || r.classification.primary) || "OPS"}`;
  } catch (e) {
    $("platform-status").textContent = e.message;
  }
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
  renderChallengeDossier(data);
  $("type-preview").textContent = JSON.stringify({
    challenge_type: data.task && data.task.challenge_type,
    attachments: data.task && data.task.attachments,
    description: data.task && data.task.description,
  }, null, 2);
  $("btn-start").disabled = false;
  $("start-hint").textContent = "场景已确认。点击启动后进入任务理解与 Agent 执行链路。";
}

async function parseChallenge() {
  $("start-hint").textContent = "正在解析攻防场景…";
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
    $("start-hint").textContent = "请先解析攻防场景";
    return;
  }
  const executionMode = $("task-execution-mode").value;
  const actors = Math.max(1, Math.min(8, Number.parseInt($("task-actors").value, 10) || 1));
  $("start-hint").textContent = executionMode === "real"
    ? "正在检查模型配置并启动 Match 专用 VM…"
    : "正在启动演示执行…";
  $("btn-start").disabled = true;
  try {
    const r = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ task: state.parsedTask, execution_mode: executionMode, actors }),
    });
    $("start-hint").textContent =
      `已启动 ${r.run_id} · ${r.execution_mode === "real" ? "真实沙箱" : "演示"} · 场景 ${r.challenge_type_label || r.challenge_type || "-"}`;
    if ($("audit-run-id")) $("audit-run-id").value = r.run_id;
    if ($("flag-run-id")) $("flag-run-id").value = r.run_id;
    if ($("flag-challenge-id") && state.parsedTask) {
      $("flag-challenge-id").value = state.parsedTask.challenge_id || state.parsedTask.id || state.parsedTask.task_id || "";
    }
    openTaskWorkspaceTab(r.run_id, state.parsedTask);
    focusRun(r.run_id);
    refreshActive();
    loadReviewQueue();
    loadUsageSummary();
  } catch (e) {
    $("start-hint").textContent = e.message;
    $("btn-start").disabled = false;
  }
}

async function loadHistory() {
  $("history-status").textContent = "加载中…";
  const data = await api("/api/runs");
  state.historyRuns = data.runs || [];
  renderHistoryCards();
  $("history-status").textContent = `${state.historyRuns.length} 条`;
}

function renderHistoryCards() {
  const statuses = Array.from(new Set(state.historyRuns.map((r) => r.status).filter(Boolean)));
  renderValueTabs("history-tabs", statuses, state.historyStatus);
  $("history-active").textContent = state.historyStatus || "全部";
  const runs = state.historyStatus ? state.historyRuns.filter((r) => r.status === state.historyStatus) : state.historyRuns;
  $("history-list").innerHTML = runs.length ? runs.map((r) => {
    const type = (r.task && (r.task.challenge_type_label || r.task.challenge_type)) || "未识别场景";
    const title = (r.task && r.task.title) || r.run_id;
    return `<article class="arsenal-card history-run-card" data-id="${escapeHtml(r.run_id)}">
      <div class="skill-card-top">
        <span class="skill-type">${escapeHtml(type)}</span>
        <span class="skill-kind">${escapeHtml(r.status || "-")}</span>
      </div>
      <div class="skill-card-main">
        <div class="skill-orbit">${escapeHtml(String(r.status || "RUN").slice(0, 3).toUpperCase())}</div>
        <div>
          <h3>${escapeHtml(title)}</h3>
          <p>${escapeHtml(r.run_id)}</p>
        </div>
      </div>
      <div class="skill-card-meta">
        <span>复盘摘要</span>
        <strong>${escapeHtml(r.step_count || 0)} steps</strong>
        <small>${escapeHtml(r.created_at || "无创建时间")} · 点击查看事件、DAG、日志和续跑入口</small>
      </div>
      <div class="skill-card-actions">
        <button type="button" class="skill-action primary" data-action="history-detail" data-id="${escapeHtml(r.run_id)}">查看复盘</button>
      </div>
    </article>`;
  }).join("") : `<div class="skill-card empty"><strong>暂无战斗档案</strong><span>完成或启动任务后会出现在这里。</span></div>`;
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
  const topbarRefresh = $("topbar-refresh");
  if (topbarRefresh) topbarRefresh.addEventListener("click", () => window.location.reload());
  let gateMode = "login";
  const enterApp = (mode) => {
    const teamName = $("gate-team-name") && $("gate-team-name").value.trim();
    const name = teamName || ($("gate-user") && $("gate-user").value.trim()) || "demo-team";
    $("auth-gate").classList.add("hidden");
    $("app-shell").classList.remove("hidden");
    const badge = document.querySelector(".signed-in-user");
    if (badge) badge.innerHTML = `${name} <small>${mode}</small>`;
    if ($("profile-team")) $("profile-team").textContent = name;
    if ($("gate-status")) $("gate-status").textContent = `${mode}成功：${name}`;
  };
  if ($("btn-gate-login")) $("btn-gate-login").addEventListener("click", () => enterApp("比赛队伍"));
  if ($("btn-gate-register")) $("btn-gate-register").addEventListener("click", () => {
    const card = document.querySelector(".auth-card");
    if (gateMode !== "register") {
      gateMode = "register";
      card.classList.add("register-mode");
      $("btn-gate-register").textContent = "创建并进入";
      if ($("gate-status")) $("gate-status").textContent = "请补充队伍信息后创建队伍。";
      return;
    }
    const pass = ($("gate-pass") && $("gate-pass").value) || "";
    const confirm = ($("gate-pass-confirm") && $("gate-pass-confirm").value) || "";
    if (confirm && pass !== confirm) {
      if ($("gate-status")) $("gate-status").textContent = "两次密码不一致。";
      return;
    }
    enterApp("新注册队伍");
  });
  if ($("btn-logout")) $("btn-logout").addEventListener("click", () => {
    $("app-shell").classList.add("hidden");
    $("auth-gate").classList.remove("hidden");
    if ($("gate-status")) $("gate-status").textContent = "已退出。";
  });
  document.querySelectorAll(".theme-card[data-theme]").forEach((b) => b.addEventListener("click", () => {
    const theme = b.dataset.theme;
    document.body.classList.toggle("theme-dark", theme === "dark");
    document.body.classList.toggle("density-compact", theme === "dense");
    document.querySelectorAll(".theme-card[data-theme]").forEach((x) => x.classList.toggle("active", x === b));
    if ($("theme-status")) $("theme-status").textContent = `当前：${b.querySelector("strong").textContent}`;
  }));
  document.querySelectorAll(".wizard-nav-item").forEach((b) => b.addEventListener("click", () => setStep(b.dataset.step)));
  $("btn-step-prev").addEventListener("click", () => setStep(STEPS[Math.max(0, STEPS.indexOf(state.step) - 1)]));
  $("btn-step-next").addEventListener("click", () => {
    const i = STEPS.indexOf(state.step);
    if (i < STEPS.length - 1) setStep(STEPS[i + 1]);
  });
  $("btn-save-model").addEventListener("click", saveConfig);
  $("btn-reload-model").addEventListener("click", loadConfig);
  $("btn-env-check").addEventListener("click", envCheck);
  $("dashboard-window-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-window]");
    if (!b) return;
    state.dashboardWindow = b.dataset.window;
    document.querySelectorAll("#dashboard-window-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    loadDashboard(true);
  });
  $("usage-chart-tabs").addEventListener("click", (e) => {
    const button = e.target.closest("[data-usage-chart]");
    if (button) setUsageChartView(button.dataset.usageChart);
  });
  $("challenge-browser-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".challenge-tab");
    if (!tab) return;
    if (tab.dataset.runId) focusRun(tab.dataset.runId);
    setTaskPage(tab.dataset.taskPage);
  });
  $("btn-sandbox").addEventListener("click", probeSandbox);
  if ($("btn-platform-status")) $("btn-platform-status").addEventListener("click", loadPlatformStatus);
  if ($("btn-platform-sync")) $("btn-platform-sync").addEventListener("click", syncPlatformChallenges);
  if ($("btn-platform-fetch")) $("btn-platform-fetch").addEventListener("click", fetchPlatformChallenge);
  if ($("btn-local-understand")) $("btn-local-understand").addEventListener("click", understandLocalChallenge);
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
  $("skill-category-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".skill-tab");
    if (!tab) return;
    $("skill-category").value = tab.dataset.category;
    loadSkills();
  });
  $("skill-search").addEventListener("input", () => { clearTimeout(state._sk); state._sk = setTimeout(loadSkills, 200); });
  $("skill-list").addEventListener("click", (e) => {
    const action = e.target.closest(".skill-action");
    if (action) {
      handleSkillAction(action.dataset.action, action.dataset.id, action).catch((err) => {
        $("skill-status").textContent = err.message;
      });
      return;
    }
    const card = e.target.closest("[data-id]");
    if (card && !card.classList.contains("empty")) openSkill(card.dataset.id);
  });
  $("btn-skill-refresh").addEventListener("click", loadSkills);
  $("btn-skill-export").addEventListener("click", () => exportSkillList().catch((e) => { $("skill-status").textContent = e.message; }));
  $("btn-skill-pin-view").addEventListener("click", togglePinnedView);
  $("btn-skill-close").addEventListener("click", () => $("skill-detail").classList.add("hidden"));
  $("tool-category-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".skill-tab");
    if (!tab) return;
    state.toolCategory = tab.dataset.category;
    renderToolMatrix();
  });
  $("sandbox-category-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".skill-tab");
    if (!tab) return;
    state.sandboxCategory = tab.dataset.category;
    renderSandboxMatrix();
  });
  $("tool-list").addEventListener("click", (e) => {
    const action = e.target.closest(".skill-action");
    if (!action) return;
    if (action.dataset.action === "tool-detail") openToolDetail(action.dataset.id);
    if (action.dataset.action === "tool-arm") {
      action.classList.add("armed");
      action.textContent = "已加入";
      $("tool-status").textContent = `已加入当前 Agent 工具链：${action.dataset.id}`;
    }
  });
  $("sandbox-report").addEventListener("click", (e) => {
    const action = e.target.closest(".skill-action");
    if (!action) return;
    if (action.dataset.action === "sandbox-detail") openSandboxDetail(action.dataset.id);
    if (action.dataset.action === "sandbox-probe") {
      openSandboxDetail(action.dataset.id);
      $("sandbox-status").textContent = `已打开 ${categoryLabel(action.dataset.id)} 沙箱运行时状态`;
    }
  });
  $("btn-tool-refresh").addEventListener("click", loadSkills);
  $("btn-tool-close").addEventListener("click", () => $("tool-detail").classList.add("hidden"));
  $("btn-sandbox-close").addEventListener("click", () => $("sandbox-detail").classList.add("hidden"));
  [
    ["agent-role-tabs", "agentRole", renderAgentRoles],
    ["blackboard-tabs", "blackboardType", renderBlackboard],
    ["review-tabs", "reviewStatus", renderReviewCards],
    ["usage-tabs", "usageView", renderUsageCards],
    ["mcp-tabs", "mcpType", renderMcpCards],
    ["history-tabs", "historyStatus", renderHistoryCards],
  ].forEach(([tabsId, stateKey, render]) => {
    $(tabsId).addEventListener("click", (e) => {
      const tab = e.target.closest(".skill-tab");
      if (!tab) return;
      state[stateKey] = tab.dataset.value;
      render();
    });
  });
  ["agent-role-cards", "blackboard-cards", "review-cards", "usage-cards", "mcp-cards"].forEach((listId) => {
    $(listId).addEventListener("click", (e) => {
      const action = e.target.closest("[data-action='module-detail']");
      if (action) openModuleDetail(action.dataset.listId, action.dataset.id);
    });
  });
  $("history-list").addEventListener("click", (e) => {
    const action = e.target.closest("[data-action='history-detail']");
    const card = e.target.closest("[data-id]");
    const id = action ? action.dataset.id : card && card.dataset.id;
    if (id) openHistory(id);
  });
  $("btn-agent-role-close").addEventListener("click", () => $("agent-role-detail").classList.add("hidden"));
  $("btn-blackboard-close").addEventListener("click", () => $("blackboard-detail").classList.add("hidden"));
  $("btn-review-close").addEventListener("click", () => $("review-detail").classList.add("hidden"));
  $("btn-usage-close").addEventListener("click", () => $("usage-detail").classList.add("hidden"));
  $("btn-mcp-close").addEventListener("click", () => $("mcp-detail").classList.add("hidden"));
  $("btn-start").addEventListener("click", () => startTask().catch((e) => { $("start-hint").textContent = e.message; }));
  $("btn-parse").addEventListener("click", () => parseChallenge());
  $("sample-task-bar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-sample]");
    if (b) applySampleTask(b.dataset.sample);
  });
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
    if (b) {
      openTaskWorkspaceTab(b.dataset.id, { title: b.dataset.id, challenge_type: "ctf-misc" });
      focusRun(b.dataset.id);
    }
  });
  $("btn-history-reload").addEventListener("click", loadHistory);
  $("btn-hist-close").addEventListener("click", () => $("history-detail").classList.add("hidden"));
  $("btn-hist-focus").addEventListener("click", () => {
    if (!state.historyFocus) return;
    setStep("run");
    openTaskWorkspaceTab(state.historyFocus, { title: state.historyFocus, challenge_type: "ctf-misc" });
    focusRun(state.historyFocus);
  });
  $("btn-hist-resume").addEventListener("click", async () => {
    if (!state.historyFocus) return;
    await api("/api/runs/" + encodeURIComponent(state.historyFocus) + "/resume", { method: "POST" });
    setStep("run");
    openTaskWorkspaceTab(state.historyFocus, { title: state.historyFocus, challenge_type: "ctf-misc" });
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
bindDashboardTooltip();
loadConfig();
startDashboard();
renderPills("");
refreshActive().catch(() => {});
