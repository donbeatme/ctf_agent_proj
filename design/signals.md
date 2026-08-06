# SignalBus + 日志层

事件总线与双通道日志。实现：`agent/signals.py`、`agent/logging.py`。

---

## 1. SignalBus — 发布/订阅

```python
class SignalBus:
    def subscribe(self, obj)      # 注册订阅者
    def unsubscribe(self, obj)    # 移除订阅者
    def emit(self, signal: Signal, **kw)  # 发射信号
```

### 订阅协议

订阅者对象需实现 `on_<signal>(self, **kw)` 方法。方法名由 Signal 枚举值小写转换，如 `Signal.CTX_ASSEMBLED` → `on_ctx_assembled`。

```python
class MySubscriber:
    def on_ctx_assembled(self, role=None, total=0, budget=0, over=0, comps=None, **kw):
        ...
```

### 异常隔离

`emit` 遍历所有订阅者，per-subscriber try/except。单个订阅者抛异常不影响其他订阅者和调用方。

---

## 2. Signal 枚举（`agent/schema.py`）

### 生命周期
| Signal | 参数 |
|---|---|
| `RUN_STARTED` | task, max_cycles, max_replans, max_stalls, max_deadlock_attempts |
| `RUN_END` | state, fail_reason, total_cycles |
| `STATE_TRANSITION` | from_state, to_state, reason |
| `FAILED` | reason, replans, stalls, deadlock_attempts |

### LLM 调用
| Signal | 参数 |
|---|---|
| `LLM_CALL_START` | role, ctx_size |
| `LLM_CALL_END` | role, latency_ms, prompt_tokens, completion_tokens（失败时带 error） |
| `LLM_RESPONSE` | role, result |

### 上下文
| Signal | 参数 |
|---|---|
| `CTX_ASSEMBLED` | role, total_tokens, budget, overflow, components, system_tokens |
| `CTX_OVERFLOW` | role, overflow, method（engine 侧另发 `detail` 文本形式） |
| `CTX_COMPRESSED` | role, method, compressed ([{key, from_level, to_level, delta}]), total_after, overflow_after |
| `CTX_INGEST` | role, detail |

### 步骤
| Signal | 参数 |
|---|---|
| `STEP_STARTED` | step_id, attempt, max_attempts |
| `STEP_ENDED` | step_id, verdict, observation, attempts |

### 重规划 / 调度
| Signal | 参数 |
|---|---|
| `REPLAN_START` | source, turn_count, dag_step_count |
| `REPLAN` | —（中间事件，replan_end 收尾） |
| `REPLAN_END` | reason, changes, stalls, new_step_count, replans |
| `DEADLOCK_DETECTED` | report, deadlock_attempts, max_deadlock_attempts |
| `OSCILLATION_RISK` | replans, stalls, max_replans, max_stalls |
| `PLAN_REVIEW_PASS` | —（无参数） |

### 超时 / 预算
| Signal | 参数 |
|---|---|
| `PHASE_TIMEOUT` | phase, elapsed_ms, step_id |
| `RUN_TIMEOUT` | elapsed_ms |
| `TOKEN_BUDGET_EXCEEDED` | tokens, budget |

---

## 3. EngineLogger — run.log 格式

```python
class EngineLogger:
    """订阅 SignalBus，生成人类可读的 run.log。"""
```

### 输出文件

`runs/<run_id>/run.log`

### Tick 结构

```
━━━━━━━━━━━━━━━━━━━━━━━━━━ tick N  STATE (extra) ━━━━━━━━━━━━━━━━━━━━━━━━━━
2026-08-06 HH:MM:SS [engine] engine-level message
2026-08-06 HH:MM:SS [role]
    [tag] content
    [tag] content
        sub-line (double indent)
```

### Agent 标签

| 标签 | 含义 | 缩进 |
|---|---|---|
| `[engine]` | 引擎操作（状态迁移、dag 变更、调度决策） | 0 |
| `[ctx_asm]` | 上下文组装（total_tok/budget/overflow + 组件档位） | 1 |
| `[llm]` | LLM 调用（#N ctx=S sys=S tok=P+C=T latency=Sms） | 1 |
| `[tool]` | 执行器工具轨迹（use_tool / tool_result） | 1 |
| `[ctx_ing]` | 上下文装填 | 1 |
| `[dag]` | DAG 变更（+s1 +s2 -s3 ~s4） | 1 |
| `[verdict]` | 评估判定（PASS/FAIL/RETRY/...） | 1 |
| `[compress]` | 上下文压缩记录（overflow 触发值 + 降档明细） | 1 |

LLM response 文本以双缩进子行显示。

### 汇总表（RUN_END 时输出）

```
──────────────────────────────────────────────────────────────
  汇总
──────────────────────────────────────────────────────────────
  ticks=N  llm_calls=N  total_latency=Nms

  role              calls   ctx(avg)   latency(avg)   tokens(prompt+compl=total)   verdicts
  ────────────────  ─────   ────────   ────────────   ──────────────────────────   ────────
  evaluator_plan        1   1,987 tok           0 ms                          —   PASS
  evaluator_step        3     642 tok           0 ms         1,200+300=1,500   PASS  PASS  PASS
  evaluator_task        1   2,475 tok           0 ms                            —   DONE
  executor              3     287 tok           0 ms         2,100+450=2,550   —
  planner               2      79 tok       6,470 ms         8,000+1,000=9,000   —

  ctx 溢出 (tick N, role 超 K tok  method)
  phase 超时 phase=... elapsed=Nms [step=...]

  终态: DONE  fail_reason=None
```

ctx 溢出 / phase 超时行仅在有事件时列出；汇总表 `tokens` 列与每 tick 的 `[llm] tok=` 一起反映 token 用量追踪。

---

## 4. events.jsonl — 结构化审计流

与 `run.log` 分离：`events.jsonl` 是机器可读的结构化事件，每条一行 JSON，由 `Workspace.add_event` 即时追加。

```
{"uuid":"...","agent":"planner","kind":"replan","step_id":null,"verdict":null,"detail":{...},"ts":"..."}
{"uuid":"...","agent":"evaluator_step","kind":"step_eval","step_id":"s1","verdict":"pass","detail":{...},"ts":"..."}
```

- **run.log** = 人类调试追溯
- **events.jsonl** = 结构化决策证据
