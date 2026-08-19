# Flag 验证分层(动态 flag 的本地判定)

相关: `design/contracts.md` §2(submit 契约)、`design/ctx.md` §4(ExperienceComponent)、`design/workspace.md` §2/§8(`ws.experience`)。

## 问题:动态 flag 不能存静态答案

带靶机容器的题,flag 随**容器实例**变化(Hack World 实测:3 个实例 3 个 uuid)。旧的
`challenge_flags` 一题一行一个字符串(单 PK),`_local_verify` 拿提交值与存串比对——
存的是第一个实例的过期 flag,后续实例的正确 flag 全被拒;且平台对已解题不再判分
(ALREADY_SOLVED),形成死锁。

**核心原则**:只有平台提交成功验证过的方法才可信。动态题的本地判定存"可重跑的验证
过程(procedure)"而非静态 flag——由执行 agent(EE)把关,用已验证 procedure 对**当前实例**
重新推导即可本地判,无需平台往返。

## 信任分层

| 层 | 含义 | 判定方式 |
|---|---|---|
| **T0 literal** | 静态题 | `challenge_flags` 里 platform-verified 的权威串,本地比对(`LOCAL_VERIFIED`)。行为不变 |
| **T1 procedure 已验证** | 动态题,该 procedure 输出曾被平台接受(`platform_verified=1`) | 对当前实例重跑 verifier → 本地比对(`LOCAL_PROCEDURE`)。**解掉 ALREADY_SOLVED 死锁** |
| **T2 临时(首次解题)** | 动态题,EE 提供 provenance(trace + 脚本路径) | 提交一次闭环;平台成功 → 升 T1 |
| 以下 | 无 trace / 纯格式猜 | 不自动提交,交平台/人工 |

## procedure 存储

`ctf_platform/storage.py` 新表 `challenge_procedures`(一题多行 = 多条解题路径):

```sql
CREATE TABLE challenge_procedures (
  procedure_id      TEXT PRIMARY KEY,
  challenge_id      TEXT NOT NULL REFERENCES challenges(challenge_id) ON DELETE CASCADE,
  friendly_id       TEXT,                 -- 精确匹配键(denormalize)
  template_id       TEXT,                 -- 精确匹配键(跨场地同题;来自 extra_json)
  method            TEXT NOT NULL,        -- 'literal' | 'procedure'
  flag              TEXT,                 -- literal: 权威串; procedure: 上次推导值(仅 hint,不入 ctx)
  flag_format       TEXT,
  verifier_path     TEXT,                 -- procedure: 相对 challenge 目录的提取脚本
  target            TEXT,                 -- 验证时实例
  trace_json        TEXT,                 -- provenance: 逐字符断言 / 脚本 hash+stdout
  platform_verified INTEGER DEFAULT 0,
  last_ok_at        TEXT,
  used_count        INTEGER DEFAULT 0,
  created_at        TEXT NOT NULL
);
```

方法:`upsert_procedure`(friendly_id/template_id 从 challenges 行/extra_json 自动填)、
`get_procedures`、`get_validated_procedures`(platform_verified=1 且 method='procedure')、
`match_procedures`(仅精确,见下)、`promote_procedure`(升 T1)、`mark_procedure_ok`(本地命中)。

### 匹配键(仅精确)

用户选定:不跨题分类/题型召回。`friendly_id` **或** `template_id` 完全一致才算经验:

```
match_procedures(friendly_id, template_id)
  WHERE platform_verified=1 AND method='procedure'
    AND (friendly_id=? OR template_id=?)
  ORDER BY last_ok_at IS NULL, last_ok_at DESC LIMIT 10
```

- `template_id` 是平台给的题模板 id(跨场地重部署同题仍命中),存 `challenges.extra_json`。
- 非精确(前缀/分类相似)一律不命中。

## provenance 契约(EE 把关)

`submit_flag` 工具支持可选的 `provenance`:

```json
{
  "verifier": "solve_extract.py",      // 相对 challenge 目录的提取脚本
  "trace": "逐字符 ascii 二进制搜索...",  // 提取过程摘要(可审计)
  "flag_format": "CTF2{uuid}"
}
```

提交回环 `adapter.submit(challenge_id, flag, provenance=...)`:

- 平台 **success** 分支:有 provenance → 落一条 `platform_verified=1` 的 procedure(T0→T1);
  给不出 provenance 但 has_container → 只记 method='procedure、verifier_path=None、
  platform_verified=1 的占位行(标记"该题曾被平台确认可解",无脚本不自动推导)。
- 静态题仍 `persist_flag`(不变)。
- **ALREADY_SOLVED** 分支:动态题走 `_local_verify` 分层,不再拿过期串比对。

## `_local_verify` 分层判定

`ctf_platform/base.py`,注入缝 `set_procedure_runner(fn)`(`fn(verifier_path, target) ->
derived_flag | None`,由 executor 注入,内部走沙箱):

```
if ch.has_container:
    if procedure_runner 注入且 get_validated_procedures 非空:
        target = _current_target(challenge_id)      # start_target 取当前实例,回退存库
        for proc in validated:
            derived = runner(proc.verifier_path, target)
            if derived is None: continue
            ok = derived == submitted
            if ok: mark_procedure_ok(proc)          # 累加 used_count + 刷 last_ok_at
            return LOCAL_PROCEDURE {ok, correct=ok}
    return None                                     # 动态无 procedure/runner → 交回平台
else:
    row = get_flag(challenge_id)
    if not row or not row.verified: return None
    return LOCAL_VERIFIED {ok, correct=row.flag == submitted}   # T0 静态不变
```

verifier 脚本约定(executor `_run_verifier`):读 `argv[1]`(或 metadata.yml `target` 字段)
取靶机地址,推导出的 flag 打印在 **stdout 末行**;失败不打印。`runpy.run_path(run_name='__main__')`
保证脚本 `if __name__=='__main__'` 守卫生效。

## ctx 经验组件

`ExperienceComponent`(`agent/ctx.py`,key=`experience`,priority=4,raw/ref)投影
`ws.experience`——engine `_init_run` 时经 `executor.match_experience()`(即
`adapter.match_procedures(challenge_id)`)装填,run 内不清理。只渲染紧凑索引
(题号/方法/是否已验证/上次成功/脚本路径),**不渲染过期 hint flag**;完整 trace 不在 ctx,
EE 直接跑 verifier 脚本即可。动态题闭环:

```
ctx 看到"该题已有已验证 procedure 脚本 V" → 对当前 target 重跑 verifier 推导 flag
  → 提交(adapter._local_verify 走 T1 本地判,无需平台往返)
无 procedure → 提取 + 附 provenance 提交 → 平台成功 → 升 T1,下次实例复用
```

## Hack World 端到端(登记样例)

- challenge_id `e9baf08f-5f6e-40b8-953f-2c30689f6c05`,friendly_id `PCHAL-2026-1223`,
  template_id `a4e799b7-8baf-4add-8b83-14f70b4a77e2`(存 `extra_json`)。
- 受管 verifier `data/challenges/PCHAL-2026-1223/solve_extract.py`(布尔盲注逐字符推导)。
- 手动登记 T1:procedure_id `proc-hackworld-0001`,`platform_verified=1`(依据:盲注方法在
  实例 1 产出平台接受的 `CTF2{6d9b...}`,即"方法被平台验证过")。
- 换新实例:engine 装填经验 → executor 重跑 verifier 得当前实例 flag → 本地判
  `LOCAL_PROCEDURE correct=true`,不再误拒、不再 ALREADY_SOLVED 死锁。
