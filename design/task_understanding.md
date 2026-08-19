# Task Understanding / Multimodal Input Layer

## Scope

本层将本地 CTF challenge 输入转换为现有 Engine 契约：

```text
raw challenge reference
  -> metadata
  -> attachments Artifact[]
  -> JSON-safe artifacts
  -> structured goal / constraints / target info
  -> TaskInput(raw_content, goal_list)
```

它不负责解题，不推断漏洞，不选工具，不连靶机，不执行附件。

## 目录结构

```text
task_understanding/
├── __init__.py
├── real_understander.py      # RealTaskUnderstander
├── classify.py               # 题型判定:关键词(CATEGORY_KEYWORDS)+ 附件扩展名启发 → challenge_type
├── goals.py                  # 目标生成策略:按用户输入生成 goal_list(仅 id)
├── normalize.py              # 多源任务输入归一化:字段归一成 engine 可消费的 raw dict
├── artifact_adapter.py       # attachments Artifact -> JSON-safe
├── image_understanding.py    # ImageUnderstander / OllamaImageUnderstander
└── loaders/
    ├── __init__.py
    └── local.py              # 本地 challenge_dir / metadata.yml / distfiles
```

`classify.py` / `goals.py` / `normalize.py` 收编自远程 `challenge_intake`
（`classify_challenge` / `default_goals` / `normalize_sources` 逻辑），
远程 ingestion 已不在本仓库。

## 输入

```python
{"challenge_dir": "/path/to/challenge"}
```

或：

```python
{
    "metadata_path": "/path/to/metadata.yml",
    "files_dir": "/path/to/distfiles",
}
```

## 输出

```python
TaskInput(
    raw_content={
        **metadata,
        "task_goal": {...},
        "constraints": [...],
        "target_info": optional_target_info,
        "artifacts": safe_artifacts,
    },
    goal_list=[Goal(id="obtain_flag")],
)
```

## 数据流

```text
raw
  -> normalize_sources()       # 多源输入字段归一化
  -> LocalChallengeLoader.resolve_paths()
  -> LocalChallengeLoader.load_metadata()
  -> attachments.att(files_dir)
  -> artifacts_to_json_safe()
  -> classify_challenge()      # 题型判定 → challenge_type / type_scores
  -> binary metadata fallback
  -> optional image semantics
  -> default_goals()           # goal_list(obtain_flag)
  -> task_goal / constraints / target_info
  -> TaskInput
```

## 当前能力

- 读取本地 YAML metadata。
- 保留未知 metadata 字段。
- 解析 `distfiles/` 目录。
- 文本 / 源码 / HTML / PDF / ZIP / 图片等附件归一化。
- 题型判定：`classify_challenge` 关键词 + 附件扩展名启发，回填
  `challenge_type` / `type_scores`（`parse_challenge` 摄入后即判定）。
- 二进制附件 fallback：记录 `source`、`size_bytes`、`sha256`、`mime`、`binary_format`。
- ELF 基础识别：`elf_class`、`endianness`、常见架构。
- 可选用 Ollama VLM 为图片补充 `image_semantics`。
- 输出保证 JSON 可序列化。

## 安全边界

- 不 import / 不执行附件代码。
- 不运行二进制。
- 不连接 target。
- 不做 DNS / 端口扫描。
- 默认不调用 LLM / VLM。
- 不选择技能，不生成计划。
