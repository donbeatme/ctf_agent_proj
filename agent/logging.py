"""引擎日志:订阅 SignalBus,写人类可读 run.log(调试/回放)。

与 History(events.jsonl)职责分离:
- History: agent 上下文的结构化决策证据链
- Log: 人的调试器——tick 驱动的状态机轨迹 + agent 行为 + ctx 组件状态 + DAG 变更

格式:tick N · STATE 为节头;系统行为 [engine] 前缀;agent 行为以 role 名为头、
tab 缩进的标签([ctx_asm]/[llm]/[tool]/[dag]/[ctx_ing]/[verdict]/[compress])。
"""

import time
from pathlib import Path


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ts():
    return _now()


class EngineLogger:
    """日志订阅者:写 runs/<run_id>/run.log(人类可读格式)。"""

    def __init__(self, log_dir: str | Path | None = None):
        self._log_dir = Path(log_dir) if log_dir else None
        self._fh = None
        self._run_id = ""

        # ── tick 累积状态 ──
        self._tick = 0
        self._tick_state = "PLANNING"
        self._tick_extra = ""                # 节头注释(initial/replan #N/retry)
        self._tick_lines: list[str] = []     # 当前 tick 行缓冲

        # ── agent 块上下文 ──
        self._cur_agent: str | None = None   # 当前 agent role(有则处于 agent 内部)
        self._cur_llm: dict | None = None    # 当前 LLM 调用暂存(ctx_size/latency)

        # ── 计数器 ──
        self._llm_count: dict[str, int] = {}  # role → 累计 LLM 调用次数

        # ── 汇总统计 ──
        self._total_ticks = 0
        self._total_llm = 0
        self._total_latency = 0
        self._overflow_events: list[dict] = []
        self._role_stats: dict[str, dict] = {}  # role → {calls, total_ctx, total_lat, verdicts}
        self._replan_count = 0

        # ── 环境检查累计 ──
        self._env_missing: dict | None = None  # run_start 快照统计(on_run_end 汇总)

    # ═══════════════════════════════════════════════════════════════════
    # 文件管理
    # ═══════════════════════════════════════════════════════════════════

    def _write(self, line: str):
        if self._fh is None:
            return
        self._fh.write(line + "\n")

    def _ensure_fh(self):
        if self._fh is not None:
            return
        if self._log_dir is None:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._fh = (self._log_dir / "run.log").open("w", encoding="utf-8", buffering=1)

    # ═══════════════════════════════════════════════════════════════════
    # tick 生命周期
    # ═══════════════════════════════════════════════════════════════════

    def _start_tick(self, state: str, extra: str = ""):
        """开始新 tick:在有内容写入前不输出节头(延迟到首次写入或 flush)。"""
        self._tick_state = state
        self._tick_extra = extra
        self._tick_lines = []
        self._cur_agent = None
        self._cur_llm = None

    def _flush_tick(self):
        """输出当前 tick(含节头 + 内容行 + 尾空行)。"""
        if not self._tick_lines:
            return
        self._close_llm()
        self._close_agent()

        extra = f" ({self._tick_extra})" if self._tick_extra else ""
        header = f" tick {self._tick}  {self._tick_state}{extra} "
        bar_len = max(60, 80 - len(header))
        half = bar_len // 2
        left = "━" * half
        right = "━" * (bar_len - half)
        self._write("")
        self._write(f"{left}{header}{right}")

        for line in self._tick_lines:
            self._write(line)

        self._total_ticks += 1

    # ═══════════════════════════════════════════════════════════════════
    # agent / engine 行管理
    # ═══════════════════════════════════════════════════════════════════

    def _engine(self, text: str):
        """追加一条 [engine] 根级行(自动关闭当前 agent)。"""
        self._close_llm()
        self._close_agent()
        self._tick_lines.append(f"{_ts()} [engine] {text}")

    def _to_agent(self, role: str):
        """切换到 agent 块;角色变化时关闭旧 agent 开新的。"""
        role_v = str(role) if not isinstance(role, str) else role
        if self._cur_agent != role_v:
            self._close_llm()
            self._close_agent()
            self._cur_agent = role_v
            self._tick_lines.append(f"{_ts()} [{role_v}]")

    def _agent_line(self, tag: str, content: str = ""):
        """在 agent 块内追加一行 tab 缩进。[tag] content 格式。"""
        if content:
            self._tick_lines.append(f"\t[{tag}] {content}")
        else:
            self._tick_lines.append(f"\t[{tag}]")

    def _agent_sub(self, text: str):
        """agent 内二级缩进(双 tab),不带 tag。"""
        self._tick_lines.append(f"\t\t{text}")

    def _close_agent(self):
        self._cur_agent = None

    def _close_llm(self):
        self._cur_llm = None

    # ═══════════════════════════════════════════════════════════════════
    # 信号处理
    # ═══════════════════════════════════════════════════════════════════

    # ── 引擎生命周期 ──

    def on_run_started(self, task=None, max_cycles=None, max_replans=None,
                       max_stalls=None, max_deadlock_attempts=None, **kw):
        self._ensure_fh()
        task_desc = ""
        if isinstance(task, dict):
            task_desc = task.get("description", task.get("name", ""))
            if not task_desc:
                task_desc = str(task)[:80]
        self._run_id = kw.get("run_id", "")
        # 写入前置行(不在任何 tick 内)
        self._write(f"{_ts()} [engine] run started  run_id={self._run_id} task=\"{task_desc}\" "
                     f"max_cycles={max_cycles} max_replans={max_replans} "
                     f"max_stalls={max_stalls} max_deadlock_attempts={max_deadlock_attempts}")
        self._write("")
        # 开始 tick 0
        self._start_tick("PLANNING", "initial")

    def on_run_end(self, state=None, fail_reason=None, total_cycles=None, **kw):
        self._flush_tick()
        self._write("")
        self._write("─" * 82)
        self._write("  汇总")
        self._write("─" * 82)
        self._write(f"  ticks={self._total_ticks}  llm_calls={self._total_llm}  "
                     f"total_latency={self._total_latency:,}ms")
        if self._overflow_events:
            for ov in self._overflow_events:
                self._write(f"  ctx 溢出 (tick {ov['tick']}, {ov['role']} "
                            f"超 {ov['overflow']} tok  {ov['method']})")
        if getattr(self, '_timeout_events', None):
            for te in self._timeout_events:
                sid = f" step={te['step_id']}" if te.get('step_id') else ""
                self._write(f"  phase 超时 phase={te['phase']} "
                            f"elapsed={te['elapsed_ms']:.0f}ms{sid}")
        if getattr(self, '_run_timed_out', False):
            self._write("  run 全局超时")
        self._write("")
        self._write("  role              calls   ctx(avg)   latency(avg)   tokens(prompt+compl=total)   verdicts")
        self._write("  ────────────────  ─────   ────────   ────────────   ──────────────────────────   ────────")
        for role, st in sorted(self._role_stats.items()):
            calls = st["calls"]
            ctx_avg = f"{st['total_ctx'] // calls:,} tok" if calls else "—"
            lat_avg = f"{st['total_lat'] // calls:,} ms" if calls else "—"
            pt = st.get("total_prompt_tokens", 0)
            ct = st.get("total_completion_tokens", 0)
            tok_str = f"{pt:,}+{ct:,}={pt+ct:,}" if (pt or ct) else "—"
            verdicts = "  ".join(st["verdicts"]) if st["verdicts"] else "—"
            self._write(f"  {role:<18} {calls:>5}   {ctx_avg:>8}   {lat_avg:>12}   {tok_str:>26}   {verdicts}")
        self._write("")
        self._write(f"  终态: {state or '?'}  fail_reason={fail_reason or 'None'}")
        if self._log_dir and (self._log_dir / "audit.json").is_file():
            self._write(f"  audit: {self._log_dir / 'audit.json'}")
        if self._env_missing:
            m = self._env_missing
            self._write(f"  环境检查: 缺工具 {m['missing']}/{m['total']}  "
                        f"manual {m['manual']}  "
                        f"sandbox={'有' if m['sandbox_available'] else '无'}")
        self.close()

    def close(self):
        """关闭日志文件句柄(幂等),供 finally 块安全调用。

        崩溃兜底:on_run_end 未触发(异常路径)时,先把当前 tick 缓冲排空再关句柄,
        避免 `_flush_tick` 只随 on_run_end/on_state_transition 触发的日志丢失。
        """
        if self._fh is None:
            return
        if self._tick_lines:
            self._flush_tick()
        self._fh.close()
        self._fh = None

    def engine_error(self, text: str):
        """引擎级错误行([engine] 前缀,根级,不归属 agent),供外部降级路径打点。"""
        self._engine(text)

    # ── 状态迁移 ──

    def on_state_transition(self, from_state=None, to_state=None, reason="", **kw):
        # 迁移行属于当前 tick,先写再 flush
        self._engine(f"state {from_state}  {to_state} ({reason})" if reason
                     else f"state {from_state}  {to_state}")
        self._flush_tick()
        self._tick += 1
        extra = self._pop_pending_extra()
        self._start_tick(str(to_state) if to_state else str(from_state), extra)

    # ── 上下文组装 ──

    def on_ctx_assembled(self, role=None, total_tokens=0, budget=0, overflow=0,
                         components=None, system_tokens=0, **kw):
        role_v = str(role)
        self._to_agent(role_v)
        self._agent_line("ctx_asm",
                         f"total_tok={total_tokens} budget={budget} overflow={overflow}")
        if components:
            comp_strs = []
            for c in components:
                key = c.get("key", "?")
                level = c.get("level", "raw")
                target = c.get("target", "ctx")
                if target == "system":
                    comp_strs.append(f"{key}[{level}](sys)")
                else:
                    comp_strs.append(f"{key}[{level}]")
            self._agent_sub("  ".join(comp_strs))
        # 统计
        self._llm_count[role_v] = self._llm_count.get(role_v, 0)
        rs = self._role_stats.setdefault(role_v, {
            "calls": 0, "total_ctx": 0, "total_lat": 0, "verdicts": [],
            "total_prompt_tokens": 0, "total_completion_tokens": 0})

    def on_ctx_overflow(self, role=None, overflow=0, method="", **kw):
        self._overflow_events.append({
            "tick": self._tick, "role": str(role), "overflow": overflow, "method": method})

    def on_ctx_compressed(self, role=None, method="", compressed=None,
                          total_after=0, overflow_after=0, **kw):
        role_v = str(role)
        self._to_agent(role_v)
        # 确定 overflow 触发值(从最近的 overflow 事件取)
        ov = 0
        if self._overflow_events:
            ov = self._overflow_events[-1]["overflow"]
        self._agent_line("compress",
                         f"overflow={ov}  {method}降级" if method == "mechanical"
                         else f"overflow={ov}  LLM 压缩")
        if compressed:
            for c in compressed:
                key = c["key"]
                from_lv = c["from_level"]
                to_lv = c["to_level"]
                delta = c.get("delta", 0)
                self._agent_sub(f"{key} {from_lv}={to_lv} ({delta:+d} tok)")
        self._agent_line("compress",
                         f"done total={total_after} overflow={overflow_after}"
                         f" ({len(compressed or [])} 组件降档)")

    # ── LLM 调用 ──

    def on_llm_call_start(self, role=None, ctx_size=0, system_size=0, **kw):
        role_v = str(role)
        self._to_agent(role_v)
        self._llm_count[role_v] = self._llm_count.get(role_v, 0) + 1
        call_num = self._llm_count[role_v]
        self._cur_llm = {
            "role": role_v,
            "call_num": call_num,
            "ctx_size": ctx_size,
            "sys_size": system_size,
        }
        rs = self._role_stats.setdefault(role_v, {
            "calls": 0, "total_ctx": 0, "total_lat": 0, "verdicts": [],
            "total_prompt_tokens": 0, "total_completion_tokens": 0})
        rs["calls"] += 1
        rs["total_ctx"] += ctx_size
        self._total_llm += 1

    def on_llm_call_end(self, role=None, latency_ms=0, error=None,
                         prompt_tokens=0, completion_tokens=0, **kw):
        if self._cur_llm is None:
            return
        self._cur_llm["latency_ms"] = latency_ms
        self._cur_llm["error"] = str(error)[:200] if error else None
        self._cur_llm["prompt_tokens"] = prompt_tokens
        self._cur_llm["completion_tokens"] = completion_tokens
        self._total_latency += latency_ms
        role_v = str(role)
        rs = self._role_stats.get(role_v)
        if rs:
            rs["total_lat"] += latency_ms
            rs["total_prompt_tokens"] += prompt_tokens
            rs["total_completion_tokens"] += completion_tokens

    def on_llm_response(self, role=None, result=None, **kw):
        """LLM 响应:渲染完整 [llm] 块(含 header + response + tool_calls)。"""
        if self._cur_llm is None:
            return
        role_v = str(role)
        self._to_agent(role_v)
        llm = self._cur_llm
        call_num = llm["call_num"]
        ctx_size = llm["ctx_size"]
        sys_size = llm.get("sys_size", 0)
        latency_ms = llm.get("latency_ms", 0)
        error = llm.get("error")
        pt = llm.get("prompt_tokens", 0)
        ct = llm.get("completion_tokens", 0)

        tok_info = f" tok={pt}+{ct}={pt+ct}" if pt or ct else ""
        if error:
            self._agent_line("llm",
                             f"#{call_num} ctx={ctx_size} sys={sys_size}{tok_info}"
                             f" latency={latency_ms}ms  ERROR: {error}")
        else:
            self._agent_line("llm",
                             f"#{call_num} ctx={ctx_size} sys={sys_size}{tok_info}"
                             f" latency={latency_ms}ms")

        # ── 按 role 类型提取响应内容 ──
        if result is None:
            pass
        elif role_v == "planner":
            self._render_planner_response(result)
        elif role_v == "executor":
            self._render_executor_response(result)
        elif role_v in ("evaluator_plan", "evaluator_step", "evaluator_task"):
            self._render_evaluator_response(role_v, result)

        self._cur_llm = None

    def _render_planner_response(self, bp):
        """planner 响应:原始 JSON 文本。"""
        raw = None
        if hasattr(bp, 'meta') and isinstance(bp.meta, dict):
            raw = bp.meta.get("_response")
        if raw:
            # 截断过长响应
            display = raw[:500] if len(raw) > 500 else raw
            if len(raw) > 500:
                display = display + "..."
            self._agent_sub(f"response: {display}")

    def _render_executor_response(self, res):
        """executor 响应:ReAct 工具轨迹 + 执行报告。"""
        tool_calls = getattr(res, 'tool_calls', None) or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tool = tc.get("tool", "?")
                args = tc.get("args", {})
            elif hasattr(tc, 'tool'):
                tool = tc.tool
                args = getattr(tc, 'args', {})
            else:
                tool = str(tc)
                args = {}
            args_str = self._fmt_args(args)
            self._agent_line("tool", f"use_tool: {tool}({args_str})")
            # tool_result
            output = ""
            if isinstance(tc, dict):
                output = tc.get("result", tc.get("output", ""))
            elif hasattr(tc, 'result'):
                output = getattr(tc, 'result', '')
            elif hasattr(tc, 'output'):
                output = getattr(tc, 'output', '')
            if output:
                output_str = str(output)[:200]
                if len(str(output)) > 200:
                    output_str = output_str + f" (truncated {len(str(output))}B)"
                self._agent_sub(f"tool_result: \"{output_str}\"")
        # 执行报告
        obs = getattr(res, 'observation', '') or ''
        r = getattr(res, 'result', None) or {}
        if obs:
            result_str = str(r)[:120] if r else "null"
            obs_short = obs[:150]
            self._agent_sub(f"report: {obs_short}")
            self._agent_sub(f"result={result_str}  observation=\"{obs_short}\"")

    def _render_evaluator_response(self, role_v, res):
        """评估器响应:verdict + 全量 opinion + observation(不再截断 200)。"""
        verdict = getattr(res, 'verdict', None)
        opinion = getattr(res, 'opinion', '') or ''
        is_completed = getattr(res, 'is_completed', False)
        observation = getattr(res, 'observation', None) or ''
        verdict_str = str(verdict).upper() if verdict else "?"
        if is_completed:
            verdict_str += " is_completed=True"
        content = verdict_str
        if opinion:
            content += f"  opinion=\"{self._cap(opinion)}\""
        self._agent_line("verdict", content)
        if observation:
            self._agent_sub(f"observation: {self._cap(observation)}")
        # 统计
        rs = self._role_stats.get(role_v)
        if rs and verdict_str:
            rs["verdicts"].append(verdict_str.split()[0])

    @staticmethod
    def _cap(text: str, limit: int = 2000) -> str:
        text = str(text)
        if len(text) <= limit:
            return text
        return text[:limit] + f"...(truncated {len(text)}B)"

    @staticmethod
    def _fmt_args(args: dict) -> str:
        if not args:
            return ""
        parts = []
        for k, v in args.items():
            if isinstance(v, str) and len(v) > 60:
                v = v[:57] + "..."
            parts.append(f"{k}={v!r}")
        return ", ".join(parts)

    # ── 步骤生命周期 ──

    def on_step_started(self, step_id=None, attempt=0, max_attempts=0, **kw):
        self._engine(f"step [{step_id}] attempt {attempt}/{max_attempts} started"
                     if attempt <= 1 or attempt == max_attempts
                     else f"step [{step_id}] attempt {attempt}/{max_attempts} retry")

    def on_step_ended(self, step_id=None, verdict=None, observation="",
                      attempts=0, **kw):
        result_str = ""
        verdict_v = str(verdict) if verdict else "?"
        self._engine(f"dag [{step_id}] status  {verdict_v.upper()} attempts={attempts}")

    # ── 重规划 ──

    def on_replan_start(self, source=None, turn_count=0, dag_step_count=0, **kw):
        self._replan_count += 1

    def on_replan(self, **kw):
        pass  # 中间事件,replan_end 处理

    def on_replan_end(self, reason="", changes="", stalls=0, new_step_count=0,
                      replans=0, **kw):
        if stalls > 0:
            self._engine(f"stalls={stalls}  DAG 签名连续 {stalls} 次无变化")

    # ── 调度 / 异常 ──

    def on_plan_review_pass(self, **kw):
        self._engine("clear_revise  REVISE=PENDING")
        self._engine("dispatch plan_review_pass  docs 注册表清空")

    def on_deadlock_detected(self, report="", deadlock_attempts=0,
                             max_deadlock_attempts=0, **kw):
        short = report[:200] if report else ""
        self._engine(f"deadlock  attempts={deadlock_attempts}/{max_deadlock_attempts}  {short}")

    def on_oscillation_risk(self, replans=0, stalls=0, max_replans=0,
                            max_stalls=0, **kw):
        self._engine(f"oscillation risk  replans={replans}/{max_replans}  stalls={stalls}/{max_stalls}")

    def on_failed(self, reason=None, replans=0, stalls=0, deadlock_attempts=0, **kw):
        self._engine(f"FAILED  reason=\"{reason or '?'}\"  replans={replans}  "
                     f"stalls={stalls}  deadlock_attempts={deadlock_attempts}")

    # ── 超时 ──

    def on_phase_timeout(self, phase=None, elapsed_ms=0, step_id=None, **kw):
        sid = f" step={step_id}" if step_id else ""
        self._engine(f"TIMEOUT phase={phase} elapsed={elapsed_ms:.0f}ms{sid}")
        # 计入汇总表的 timeout 列
        if not hasattr(self, '_timeout_events'):
            self._timeout_events: list[dict] = []
        self._timeout_events.append(
            {"phase": phase, "elapsed_ms": elapsed_ms, "step_id": step_id})

    def on_run_timeout(self, elapsed_ms=0, **kw):
        self._engine(f"RUN_TIMEOUT elapsed={elapsed_ms:.0f}ms")
        self._run_timed_out = True

    # ── Ctx 注入 ──

    def on_ctx_ingest(self, role=None, detail="", **kw):
        role_v = str(role)
        self._to_agent(role_v)
        self._agent_line("ctx_ing", detail)

    # ── 环境检查 ──

    def on_env_check(self, scope="", step_id=None, report=None, **kw):
        """环境检查(工具/沙箱/分类就绪度)写根级行:run_start 全量快照 + step 分类就绪度。"""
        if report is None:
            return
        if scope == "run_start":
            self._env_snapshot(report)
        elif scope == "step":
            self._env_step(step_id, report)

    def _env_snapshot(self, report):
        """run 起始全量清单快照:可用/缺失/manual + 沙箱运行时 + 缺失明细(截断前 15)。"""
        total = report.get("total", 0)
        avail = report.get("available", 0)
        missing = report.get("missing", 0)
        manual = report.get("manual", 0)
        unknown = report.get("unknown", 0)
        sb = report.get("sandbox") or {}
        runtime = "docker/podman(有)" if sb.get("available") else "docker/podman(无)"
        self._engine(f"check[run_start] 环境快照 工具可用 {avail}/{total} 缺失 {missing} "
                     f"manual {manual} unknown {unknown}  沙箱运行时: {runtime}")
        missing_list = report.get("missing_list") or []
        if missing_list:
            shown = missing_list[:15]
            more = f" ...(共 {len(missing_list)} 条)" if len(missing_list) > 15 else ""
            self._engine(f"check[run_start] 缺工具: {', '.join(shown)}{more}")
        cat = report.get("category") or {}
        if cat:
            self._env_snapshot_category(cat)
        self._env_missing = {"missing": missing, "manual": manual, "total": total,
                             "sandbox_available": bool(sb.get("available"))}

    def _env_snapshot_category(self, cat):
        """run_start 分类就绪度(任务理解层已判定题型时):compat/工具/安装/沙箱。"""
        sb = cat.get("sandbox") or {}
        cparts = [f"category={cat.get('category', '?')}"]
        compat = cat.get("compatibility", "")
        if compat:
            cparts.append(f"compat=\"{compat[:60]}\"")
        n_cmds = len(cat.get("install_cmds") or [])
        if n_cmds:
            cparts.append(f"install_cmds={n_cmds}")
        n_tools = len(cat.get("allowed_tools") or [])
        if n_tools:
            cparts.append(f"allowed_tools={n_tools}")
        if sb.get("needed"):
            cparts.append(f"沙箱 needed=True available={bool(sb.get('available'))}")
        else:
            cparts.append("沙箱 无需隔离")
        self._engine(f"check[run_start] 题型分类就绪度: {'; '.join(cparts)}")

    def _env_step(self, step_id, report):
        """每步执行前按 skill 分类查就绪度 + 当前活动集缺工具。"""
        parts = []
        cat = report.get("category") or {}
        if cat:
            sb = cat.get("sandbox") or {}
            cparts = [f"category={cat.get('category', '?')}"]
            compat = cat.get("compatibility", "")
            if compat:
                cparts.append(f"compat=\"{compat[:60]}\"")
            n_cmds = len(cat.get("install_cmds") or [])
            if n_cmds:
                cparts.append(f"install_cmds={n_cmds}")
            if sb.get("needed"):
                cparts.append(f"沙箱 needed=True available={bool(sb.get('available'))}")
            else:
                cparts.append("沙箱 无需隔离")
            if not cat.get("exists"):
                cparts.append("分类不存在")
            parts.append("  ".join(cparts))
        missing_tools = [t for t in (report.get("tools") or [])
                         if t.get("status") == "missing"]
        if missing_tools:
            names = ", ".join(
                f"{t['tool_id']}({t.get('check', '')})" for t in missing_tools)
            parts.append(f"工具缺失 {len(missing_tools)}: {names}")
        if parts:
            self._engine(f"check[step {step_id}] " + "  ".join(parts))

    # ── 内部辅助 ──

    _next_tick_extra: str = ""

    def hint_tick_extra(self, text: str):
        """引擎在 state transition 前调用,设置下个 tick 节头的注释文本。"""
        self._next_tick_extra = text

    def engine_action(self, text: str):
        """引擎直接写入一条 [engine] 行,用于没有对应信号的结构性操作(mark_revise 等)。"""
        self._engine(text)

    def agent_line(self, role: str, tag: str, content: str = ""):
        """引擎直接写入一条 agent 块内行,用于没有对应信号的 agent 行为(dag 变更等)。"""
        self._to_agent(role)
        self._agent_line(tag, content)

    def agent_sub(self, role: str, text: str):
        """引擎直接写入一条 agent 块内二级缩进行。"""
        self._to_agent(role)
        self._agent_sub(text)

    def _pop_pending_extra(self) -> str:
        v = getattr(self, '_next_tick_extra', '')
        self._next_tick_extra = ''
        return v
