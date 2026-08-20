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
  mcpType: "",
  historyRuns: [],
  dashboardWindow: "week",
  dashboardTimer: null,
  taskPage: "intake",
  taskTabs: [],
};

const $ = (id) => document.getElementById(id);

const CTF_TYPE_NAV = [
  "ctf-web", "ctf-pwn", "ctf-crypto", "ctf-reverse",
  "ctf-forensics", "ctf-misc", "ctf-osint", "ctf-malware", "ctf-ai-ml",
];

const CTF_TYPE_LABEL = {
  "ctf-web": "Web",
  "ctf-pwn": "Pwn",
  "ctf-crypto": "Crypto",
  "ctf-reverse": "Reverse",
  "ctf-forensics": "Forensics",
  "ctf-misc": "Misc",
  "ctf-osint": "OSINT",
  "ctf-malware": "Malware",
  "ctf-ai-ml": "AI/ML",
};

const AGENT_ROLE_ITEMS = [
  { id: "intake", type: "输入", title: "任务情报感知", badge: "wired", value: "Text / URL / JSON / Files", desc: "统一接入任务情报、附件、远程地址和平台 JSON。", detail: "输入层负责把用户投递的多源信息规范成行动任务，保留附件、目标 URL、成果口令格式和平台元数据。" },
  { id: "understander", type: "理解", title: "RealTaskUnderstander", badge: "wired", value: "真实任务解析", desc: "读取本地任务目录、metadata、附件并生成目标列表。", detail: "输出场景类型、置信度、目标列表、artifacts、target_info，为能力检索、沙箱匹配和模型提示词提供结构化上下文。" },
  { id: "planner", type: "规划", title: "Planner", badge: "wired", value: "真实 LLM", desc: "召回能力库，生成可解释 DAG。", detail: "Planner 将行动目标拆成步骤，标注依赖、验收标准、能力引用和重试策略。" },
  { id: "executor", type: "执行", title: "RealExecutor", badge: "wired", value: "可接沙箱", desc: "CommandRunner、工具调用、沙箱执行、文件产物通道。", detail: "执行层已具备 RealExecutor / CommandRunner；Web 默认保守走 mock，可扩展为显式选择 Pi/SSH 沙箱执行。" },
  { id: "evaluator", type: "验收", title: "Evaluator + Platform Submit", badge: "wired", value: "本地核验/平台提交", desc: "验证证据、候选成果口令、失败反思。", detail: "Evaluator 负责 step_eval、review、reflect、eval_goals；成果审核入口已接平台适配器的本地核验与提交能力。" },
];

const BLACKBOARD_ITEMS = [
  { id: "clue", type: "线索", title: "任务情报与附件线索", value: "0", desc: "关键字、服务指纹、文件类型、成果口令格式。", detail: "适合沉淀端口、版本、附件 hash、任务暗示、异常报错、源码路径等可复用线索。" },
  { id: "failure", type: "失败路径", title: "失败尝试归档", value: "0", desc: "避免重复爆破和错误方向。", detail: "记录失败命令、失败原因、环境限制、误判场景类型和被证伪的假设。" },
  { id: "tactic", type: "可复用打法", title: "战术沉淀", value: "0", desc: "可写回经验库/RAG。", detail: "沉淀成可搜索的打法卡，包括适用条件、工具链、关键命令和验收标准。" },
  { id: "hint", type: "人工提示", title: "人工接管提示", value: "0", desc: "暂停时追加给 Agent。", detail: "队员可以把现场经验、比赛提示、外部观察写入黑板，供 Planner 重规划时引用。" },
];

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
  { id: "challenge", type: "场景", title: "场景成本", value: "0", desc: "Web/Pwn/Crypto 等攻防场景成本对比。", detail: "场景维度适合赞助展示：展示不同攻防场景的自动化覆盖、平均 token 和研判时长。" },
];

const MCP_ITEMS = [
  { id: "terminal.exec", type: "核心", title: "terminal.exec", badge: "synced", value: "ready", desc: "容器内命令执行。", detail: "核心执行通道，供 Agent 在沙箱或工作区内运行命令并回收 stdout/stderr。" },
  { id: "file.workspace", type: "核心", title: "file.workspace", badge: "synced", value: "ready", desc: "attachments / artifacts / report。", detail: "文件工作区负责任务附件、生成脚本、证据产物和 REPORT.md。" },
  { id: "ctf.platform", type: "平台", title: "platform.bridge", badge: "synced", value: "ingest/submit", desc: "平台任务拉取、开靶、提交成果口令。", detail: "对应平台适配器：sync、ingest、start_target、stop_target、submit 与本地缓存索引。" },
  { id: "sandbox.manager", type: "沙箱", title: "sandbox.manager", badge: "synced", value: "runtime", desc: "Pi/SSH 沙箱运行时与工具冲突检测。", detail: "对应 sandbox_env.SandboxManager / ToolManager：检查 SSH 配置、镜像、工作目录、工具依赖与冲突。" },
  { id: "understander.real", type: "理解", title: "understander.real", badge: "synced", value: "task_dir", desc: "真实任务目录理解。", detail: "对应真实任务理解器：读取 metadata.yml、distfiles、target、access、artifacts，生成结构化任务输入。" },
  { id: "browser.web", type: "Web", title: "browser.web", badge: "neutral", value: "optional", desc: "Web 场景交互与截图。", detail: "用于登录、点击、截图、表单测试、XSS/SSRF 交互确认等 Web 攻防场景。" },
  { id: "crypto.sage", type: "Crypto", title: "crypto.sage", badge: "neutral", value: "image", desc: "Crypto 专项镜像工具。", detail: "对应 SageMath、Z3、LLL、PyCryptodome 等数论和约束求解能力。" },
  { id: "pwn.gdb", type: "Pwn", title: "pwn.gdb", badge: "neutral", value: "image", desc: "Pwn 调试工具链。", detail: "对应 GDB、pwndbg、pwntools、ROPgadget、QEMU 和 seccomp-tools。" },
  { id: "reverse.ghidra", type: "Reverse", title: "reverse.ghidra", badge: "reserved", value: "planned", desc: "反编译/静态分析。", detail: "后续接入 Ghidra headless、radare2、Frida、angr、apktool 等逆向工具。" },
];

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
    renderAgentRoles();
    loadCapabilities();
  }
  if (id === "experience") renderBlackboard();
  if (id === "history") loadHistory();
  if (id === "usage") renderUsageCards();
  if (id === "mcp") renderMcpCards();
  if (id === "run") refreshActive();
  if (id === "audit") renderReviewCards();
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
  if (c.includes("pwn")) return "Checksec -> Debug -> Exploit";
  if (c.includes("crypto")) return "Math -> Script -> Verify";
  if (c.includes("reverse") || c.includes("rev")) return "Static -> Trace -> Patch";
  if (c.includes("forensic")) return "Extract -> Carve -> Recover";
  if (c.includes("osint")) return "Source -> Correlate -> Proof";
  return `${kind || "Skill"} -> Tool -> Evidence`;
}

function seededSeries(n, base, spread, phase = 0) {
  return Array.from({ length: n }, (_, i) => {
    const wave = Math.sin((i + phase) * 0.86) * spread;
    const pulse = ((i * 17 + phase * 11) % 9) - 4;
    return Math.max(0, Math.round(base + wave + pulse));
  });
}

function dashboardMockData(windowKey) {
  const size = windowKey === "day" ? 12 : windowKey === "month" ? 30 : 7;
  const tick = Math.floor(Date.now() / 4500) % 17;
  const labels = Array.from({ length: size }, (_, i) => windowKey === "day" ? `${i * 2}:00` : `D${i + 1}`);
  const solved = seededSeries(size, windowKey === "month" ? 6 : 4, 3, size + tick);
  const attempts = solved.map((v, i) => v + 2 + ((i * 3) % 5));
  const tokens = seededSeries(size, windowKey === "month" ? 38 : 24, 12, 3 + tick).map((v) => v * 1000);
  const sandboxes = seededSeries(size, windowKey === "day" ? 3 : 5, 2, 8 + tick);
  return {
    labels,
    solved,
    attempts,
    accuracy: attempts.map((v, i) => Math.round((solved[i] / Math.max(1, v)) * 100)),
    tokens,
    sandboxes,
    categories: [
      ["Web", 34, "#25f2b4"],
      ["Pwn", 18, "#ffbe55"],
      ["Crypto", 22, "#9b8cff"],
      ["Reverse", 14, "#37d6ff"],
      ["Forensics", 12, "#ff5c7a"],
    ],
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
    ctx.fillText(`${label} ${value}%`, w * 0.72, 44 + i * 24);
  });
  ctx.fillStyle = "#ffffff";
  ctx.font = "700 24px system-ui, sans-serif";
  ctx.fillText(`${total}%`, cx - 28, cy + 8);
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

function renderDashboard() {
  const data = dashboardMockData(state.dashboardWindow);
  $("chart-throughput-label").textContent = state.dashboardWindow === "day" ? "今日" : state.dashboardWindow === "month" ? "近 30 天" : "近 7 天";
  const solvedTotal = data.solved.reduce((a, b) => a + b, 0);
  const attemptsTotal = data.attempts.reduce((a, b) => a + b, 0);
  $("dash-running").textContent = `${Math.min(5, 2 + (solvedTotal % 4))} / 5`;
  $("dash-queued").textContent = String(2 + (attemptsTotal % 5));
  $("dash-done").textContent = String(solvedTotal);
  $("dash-review").textContent = String(1 + (attemptsTotal % 4));
  $("dash-accuracy").textContent = `${Math.round((solvedTotal / Math.max(1, attemptsTotal)) * 100)}%`;
  $("funnel-candidate").textContent = String(Math.round(attemptsTotal * 0.45));
  $("funnel-evidence").textContent = String(Math.round(solvedTotal * 0.62));
  $("funnel-approved").textContent = String(Math.round(solvedTotal * 0.52));
  $("funnel-replan").textContent = String(Math.max(1, Math.round((attemptsTotal - solvedTotal) * 0.22)));
  drawLineChart("chart-throughput", data.labels, data.solved, data.attempts);
  drawDonutChart("chart-category", data.categories);
  drawBarChart("chart-accuracy", data.labels, data.attempts, data.accuracy);
  drawUsageArea("chart-usage", data.labels, data.tokens, data.sandboxes);
}

function startDashboard() {
  renderDashboard();
  if (state.dashboardTimer) clearInterval(state.dashboardTimer);
  state.dashboardTimer = setInterval(renderDashboard, 4500);
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
    tip.textContent = target.dataset.tip;
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
  if (listId === "agent-role-cards") return AGENT_ROLE_ITEMS;
  if (listId === "blackboard-cards") return BLACKBOARD_ITEMS;
  if (listId === "review-cards") return REVIEW_ITEMS;
  if (listId === "usage-cards") return USAGE_ITEMS;
  if (listId === "mcp-cards") return MCP_ITEMS;
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
    items: AGENT_ROLE_ITEMS,
    filter: state.agentRole,
    tabsId: "agent-role-tabs",
    activeId: "agent-role-active",
    listId: "agent-role-cards",
  });
}

function renderBlackboard() {
  renderModuleCards({
    items: BLACKBOARD_ITEMS,
    filter: state.blackboardType,
    tabsId: "blackboard-tabs",
    activeId: "blackboard-active",
    listId: "blackboard-cards",
  });
}

function renderReviewCards() {
  renderModuleCards({
    items: REVIEW_ITEMS,
    filter: state.reviewStatus,
    tabsId: "review-tabs",
    activeId: "review-active",
    listId: "review-cards",
  });
}

function renderUsageCards() {
  renderModuleCards({
    items: USAGE_ITEMS,
    filter: state.usageView,
    tabsId: "usage-tabs",
    activeId: "usage-active",
    listId: "usage-cards",
  });
}

function renderMcpCards() {
  renderModuleCards({
    items: MCP_ITEMS,
    filter: state.mcpType,
    tabsId: "mcp-tabs",
    activeId: "mcp-active",
    listId: "mcp-cards",
  });
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
    "ctf-web": ["ops-pi-web:0.1.0", "HTTP 调试 / 扫描 / Web Exploit", "通用网络隔离"],
    "ctf-pwn": ["ops-pi-pwn:0.1.0", "GDB / Pwntools / QEMU / ROP", "已有专项沙箱"],
    "ctf-crypto": ["ops-pi-crypto:0.1.0", "Sage / Z3 / PyCryptodome / LLL", "通用计算隔离"],
    "ctf-reverse": ["ops-pi-reverse:0.1.0", "Ghidra / Radare2 / Frida / Emulation", "已有专项沙箱"],
    "ctf-forensics": ["ops-pi-forensics:0.1.0", "Volatility / TShark / Binwalk / SleuthKit", "证据只读挂载"],
    "ctf-misc": ["ops-pi-misc:0.1.0", "PyJail / BashJail / Encoding / VM", "通用攻防沙箱"],
    "ctf-osint": ["ops-pi-osint:0.1.0", "DNS / Whois / Shodan / Media", "网络访问受控"],
    "ctf-malware": ["ops-pi-malware:0.1.0", "YARA / PE / C2 / 动态样本", "已有专项沙箱"],
    "ctf-ai-ml": ["ops-pi-ai:0.1.0", "Notebook / Model Inspect / Adversarial", "GPU 可选"],
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
  $("tool-status").textContent = `${items.length} 个工具已按场景类型编组`;
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
  $("sandbox-status").textContent = note || `${items.length} 类攻防场景沙箱能力已加载`;
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
        <small>独立工作目录 · 证据挂载 · 结束后可销毁</small>
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
    $("sandbox-status").textContent = "沙箱运行时探测完成";
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
  $("sandbox-detail-title").textContent = `${categoryLabel(item.category)} 沙箱`;
  $("sandbox-detail-body").textContent = JSON.stringify({
    category: item.category,
    image: item.image,
    stack: item.stack,
    sandbox_mode: item.mode,
    declared_in_backend: item.needed,
    runtime_available: item.available,
    runtime: item.runtime || {},
    lifecycle: ["创建独立工作目录", "挂载任务附件与证据目录", "Agent 调用工具执行", "回收并销毁临时沙箱"],
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
  $("start-hint").textContent = "来源已变更，请重新「解析场景」。";
}

function challengeVisualProfile(type) {
  const key = String(type || "ctf-misc");
  const profiles = {
    "ctf-web": { accent: "WEB", scene: "目标服务", fields: [["Target", "http://challenge.local:8080"], ["入口", "/login · /upload · /api"], ["风险点", "SQLi / SSRF / File Upload"], ["推荐镜像", "ops-pi-web:0.1.0"]], process: ["指纹识别", "目录/参数发现", "漏洞验证", "构造利用", "成果审核"] },
    "ctf-pwn": { accent: "PWN", scene: "二进制靶机", fields: [["Arch", "amd64"], ["保护", "NX on · Canary ? · PIE ?"], ["远程", "nc challenge.local 31337"], ["推荐镜像", "ops-pi-pwn:0.1.0"]], process: ["checksec", "逆向输入点", "调试崩溃", "ROP/堆利用", "远程打通"] },
    "ctf-crypto": { accent: "CRY", scene: "密码参数", fields: [["Primitive", "RSA / Lattice / Stream"], ["已知量", "n, e, c / samples"], ["攻击路径", "低指数 / LLL / Oracle"], ["推荐镜像", "ops-pi-crypto:0.1.0"]], process: ["抽取参数", "识别原语", "推导攻击", "脚本求解", "校验成果"] },
    "ctf-reverse": { accent: "REV", scene: "逆向样本", fields: [["Format", "ELF / PE / APK"], ["保护", "混淆 / 反调试 / VM"], ["工具", "Ghidra · r2 · Frida"], ["推荐镜像", "ops-pi-reverse:0.1.0"]], process: ["文件识别", "静态反编译", "动态跟踪", "算法还原", "生成解密器"] },
    "ctf-forensics": { accent: "FOR", scene: "证据包", fields: [["Artifacts", "pcap / image / memory / disk"], ["线索", "metadata · strings · timeline"], ["工具", "tshark · volatility · binwalk"], ["推荐镜像", "ops-pi-forensics:0.1.0"]], process: ["证据登记", "元数据扫描", "时间线还原", "隐藏数据提取", "证据归档"] },
  };
  return profiles[key] || { accent: skillGlyph(key), scene: "综合攻防任务", fields: [["类型", categoryLabel(key)], ["线索", "任务情报 / 附件 / 远程"], ["策略", "先识别再派发"], ["推荐镜像", "自动匹配场景镜像"]], process: ["归一化输入", "场景判定", "能力召回", "工具验证", "结果审核"] };
}

function renderChallengeDossier(data) {
  const task = data.task || {};
  const c = data.classification || {};
  const type = task.challenge_type || c.primary || "ctf-misc";
  const profile = challengeVisualProfile(type);
  const title = task.title || $("task-title").value.trim() || "未命名攻防任务";
  const desc = task.description || $("task-desc").value.trim() || "暂无任务情报描述";
  const attachments = task.attachments || state.attachments || [];
  $("challenge-dossier").innerHTML = `<div class="challenge-dossier-hero ${escapeHtml(type)}">
    <div class="dossier-mark">${escapeHtml(profile.accent)}</div>
    <div><span>${escapeHtml(profile.scene)}</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(desc)}</p></div>
  </div>
  <div class="dossier-grid">
    ${profile.fields.map(([k, v]) => `<div><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join("")}
    <div><span>成果口令格式</span><strong>proof{...}</strong></div>
    <div><span>置信度</span><strong>${escapeHtml(String(c.confidence ?? "-"))}</strong></div>
  </div>
  <div class="dossier-process">${profile.process.map((step, i) => `<div><span>${i + 1}</span><strong>${escapeHtml(step)}</strong></div>`).join("")}</div>
  <div class="dossier-files">${(attachments.length ? attachments : [{ name: "无附件 / 或待上传", size: 0 }]).map((a) => `<div><strong>${escapeHtml(a.name || "attachment")}</strong><span>${escapeHtml(String(a.size || 0))} B</span></div>`).join("")}</div>`;
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

function renderWorkspaceProcess(type) {
  const profile = challengeVisualProfile(type);
  $("workspace-process-map").innerHTML = profile.process.map((step, i) =>
    `<div class="process-node ${i === 0 ? "active" : ""}"><span>${i + 1}</span><strong>${escapeHtml(step)}</strong><small>${escapeHtml(profile.scene)}</small></div>`
  ).join("");
}

function openTaskWorkspaceTab(runId, task) {
  const label = (task && task.title) || runId;
  if (!$(`task-tab-${runId}`)) {
    $("challenge-browser-tabs").insertAdjacentHTML("beforeend", `<button type="button" class="challenge-tab" id="task-tab-${escapeHtml(runId)}" data-task-page="workspace" data-run-id="${escapeHtml(runId)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(skillGlyph(task && task.challenge_type))}</strong></button>`);
  }
  $("workspace-title").textContent = label;
  $("workspace-subtitle").textContent = `${categoryLabel(task && task.challenge_type)} · run_id ${runId} · 实时展示 Agent 思考、工具、证据和状态机`;
  $("workspace-type-badge").textContent = categoryLabel(task && task.challenge_type);
  renderWorkspaceProcess(task && task.challenge_type);
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
  if ($("platform-store-dir")) $("platform-store-dir").textContent = data.store_dir || "ChallengeStore";
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
    $("platform-status").textContent = `已物化到 ${r.challenge_dir}`;
  } catch (e) {
    $("platform-status").textContent = e.message;
  }
}

async function understandLocalChallenge() {
  const value = ($("local-challenge-dir").value || "").trim();
  if (!value) {
    $("platform-status").textContent = "请填写本地 challenge_dir 或 metadata.yml 路径";
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
  $("start-hint").textContent = "正在 Engine.run…";
  $("btn-start").disabled = true;
  try {
    const r = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ task: state.parsedTask }),
    });
    $("start-hint").textContent =
      `已启动 ${r.run_id} · 场景 ${r.challenge_type_label || r.challenge_type || "-"}`;
    openTaskWorkspaceTab(r.run_id, state.parsedTask);
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
    renderDashboard();
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
