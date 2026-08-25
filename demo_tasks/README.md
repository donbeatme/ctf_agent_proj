# Demo Tasks

本目录用于前端演示和联调记录，覆盖不同类型攻防任务的输入样例、推荐工具链和验收点。

## 目录说明

- `web-login-review/`: Web 登录接口风险研判
- `crypto-rsa-review/`: RSA 参数安全分析
- `binary-service-risk/`: 二进制服务风险复现
- `reverse-sample-restore/`: 逆向样本算法还原
- `forensics-traffic-review/`: 流量证据包分析

## 前端验证链路

每个样例用于验证以下链路是否可展示和闭环：

1. 攻防任务接入
2. 场景类型识别
3. Agent 规划与执行过程展示
4. 成果产物生成
5. 成果审核
6. 赛后复盘
7. 模型用量统计

## 沙箱与真实执行说明

当前前端可展示各类型任务的推荐工具链、执行过程、成果审核和复盘结果。真实工具执行链路不在 Web 页面直接触发，当前以后端命令行为准：

```bash
python main.py sandbox-probe

python main.py run-local-challenge \
  --challenge-dir <题目目录> \
  --planner-mode real \
  --executor real \
  --evaluator audit \
  --run-id <运行名称>
```

真实执行前置条件：

1. 需要一台可 SSH 访问的沙箱机器。
2. 当前项目实现的是 SSH 沙箱通信；本地 Docker / WSL / subprocess 沙箱管理器暂未接入。
3. 不同类型任务不会自动分配不同镜像；真实执行共用统一沙箱环境。
4. 工具来自固定工具目录，由 executor 根据任务步骤按需申请和调用。
5. Web 页面当前展示的是 MockExecutor 演示链路；拿到 SSH 沙箱配置后，可按命令行流程验证 `--executor real`。

执行后可检查：

```bash
cat runs/<run-id>/audit.json
tail runs/<run-id>/events.jsonl
cat runs/<run-id>/run.log
```
