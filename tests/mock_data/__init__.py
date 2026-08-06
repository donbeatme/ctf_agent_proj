"""示例数据(任务理解层下放 / 规划 LLM 响应 / 评估意见)。

这些只是默认形状,**测试时请传入自己的内容**——mock 组件均接受覆盖:
- task:PlannerInput 直接传任意 dict
- 规划响应:MockPlannerLLM(response=...) 传任意 PlanPatch JSON
- 评估意见:MockEvaluator(responses={...}) 按角色传 EvalResult
"""

from agent.evaluator import EvalResult, Verdict
from agent.executor import ExecResult, MockExecutor

MOCK_TASK = {
    "task_id": "mock-0001",
    "ground_id": "g-mock",
    "challenge_id": "c-mock",
    "title": "base64 编码",
    "description": "给定一段文本,base64 编码后作为 flag 提交。",
}

# 规划 LLM 预置响应(示例)
MOCK_PLAN_INITIAL = (
    '{"add":['
    '{"id":"s1","instruction":"读取题目描述,提取待编码内容",'
    '"criterion":"拿到原始待编码文本","depends_on":[]},'
    '{"id":"s2","instruction":"对原始文本做 base64 编码",'
    '"criterion":"编码结果用 base64 -d 还原后与原文本一致","depends_on":["s1"]},'
    '{"id":"s3","instruction":"提交编码后的 flag 并等待平台判定",'
    '"criterion":"平台返回正确判定","depends_on":["s2"]}'
    '],"reason":"mock 初始规划"}'
)

MOCK_PLAN_REVISE = (
    '{"update":[{"id":"s2","criterion":"编码结果可逆:base64 -d 还原后与原文本逐字节一致"}],'
    '"reason":"按 et 意见收紧 s2 验收标准"}'
)

# 评估意见(示例,按角色)
MOCK_EVAL_EP_FAIL = EvalResult(
    verdict=Verdict.FAIL,
    opinion="计划缺少提交前的校验步骤:编码后应先本地验证一次再提交",
)
MOCK_EVAL_EE_ESCALATE = EvalResult(
    verdict=Verdict.ESCALATE,
    opinion="s2: 编码结果与预期不符",
    observation="base64 输出 = aGVsbG8=",
)
MOCK_EVAL_ET_REPLAN = EvalResult(
    verdict=Verdict.REPLAN,
    opinion="整体方案缺少目标环境的访问步骤,需重规划",
)

# 执行结果(示例)
MOCK_EXEC_OK = ExecResult(observation="执行完成,产物已记录", result={"artifact": "base64.txt"})
MOCK_EXEC_FAIL = ExecResult(observation="执行失败:目标不可达")


# ===== 技能库文档(示例,上游交付前占位) =====
# 这些模拟"技能检索"返回的参考文档片段;ctx 只渲染 id + 一句话描述,
# 全文经 planner 的 get_doc 原生工具按需取。

MOCK_SKILL_DOCS = [
    (
        "base64 编码/解码",
        "使用场景:题目要求对文本做 base64 编码或解码时。\n"
        "Linux: echo -n '<文本>' | base64 (编码); echo '<编码>' | base64 -d (解码)。\n"
        "Python: import base64; base64.b64encode(b'<文本>').decode()\n"
        "注意:base64 不是加密,可逆;编码结果不含 key/iv,别误当 AES 提交。\n"
        "常见坑:echo 不带 -n 会多一个换行符;Windows 和 Linux 换行符不同。"
    ),
    (
        "nmap 端口扫描",
        "使用场景:目标开放端口探测、服务版本识别、OS 指纹识别。\n"
        "基础: nmap -sV -sC <target> (服务版本+默认脚本); nmap -p- <target> (全端口)。\n"
        "进阶: -sS (SYN 半开,快且隐身); -O (OS 检测); --script vuln (漏洞扫描)。\n"
        "注意:CTF 中目标通常在内网,防火墙宽松;限速 -T2 避免触发 IDS。\n"
        "输出解读:open(端口开放) / filtered(被过滤) / closed(关闭)。"
    ),
    (
        "SQL 注入基础",
        "使用场景:Web 题目中用户输入拼接到 SQL 查询,未做参数化处理。\n"
        "检测:输入单引号 ' 看报错;1' OR '1'='1 万能密码;1 UNION SELECT 1,2,3 联合查询。\n"
        "常用: UNION SELECT @@version,database(),user() 收集信息;\n"
        "  information_schema.tables/columns 爆库名/表名/列名;\n"
        "  GROUP_CONCAT() 拼接多行; LIMIT 遍历。\n"
        "注意:WAF 可能过滤关键字,尝试 /**/ 注释绕过、大小写混写、双重编码。"
    ),
    (
        "文件上传漏洞",
        "使用场景:Web 题目允许用户上传文件,服务端校验不严格。\n"
        "常见绕过:改 Content-Type (image/png → application/x-php);\n"
        "  双扩展名 (shell.php.jpg); 用 .php5/.phtml/.pht 等替代扩展名;\n"
        "  图片马 (GIF89a; <?php system($_GET['c']); ?> 插入图片二进制中)。\n"
        "webshell 一句话: <?php @eval($_POST['cmd']); ?>\n"
        "注意:检查 upload 目录是否可执行;.htaccess 可以指定任意扩展名当 php 解析。"
    ),
    (
        "目录爆破",
        "使用场景:发现隐藏路径、备份文件、git 泄露、robots.txt 等。\n"
        "工具: dirb <url> <wordlist>; gobuster dir -u <url> -w <wordlist>。\n"
        "常见目标: /.git/ (源码泄露); /robots.txt (爬虫规则); /admin/ (管理后台);\n"
        "  /.env (环境变量); /backup/ (.bak/.swp/.zip 备份); /api/ (API 文档)。\n"
        "注意:注意 robots.txt 里 Disallow 的路径可能是考点;git 泄露用 GitHack 还原。"
    ),
    (
        "JWT 攻击",
        "使用场景:Web 认证用 JWT,需伪造 token 提权或绕过认证。\n"
        '常见手法: alg=none (不签名,header 改 "alg":"none" 后删掉 signature);\n'
        "  弱密钥爆破 (hashcat -m 16500 jwt.txt wordlist.txt);\n"
        "  kid 注入 (kid 指向文件/URL,服务端可能读入作为密钥)。\n"
        "工具: jwt.io 在线解析; jwt_tool (python3 jwt_tool.py <jwt> -C -d <wordlist>)。\n"
        "注意:RS256→HS256 算法混淆攻击(公钥当 HMAC 密钥,需服务端用公钥验 HS256)。"
    ),
]


def mock_exec_by_step(results: dict) -> MockExecutor:
    """按 step.id 返回不同执行结果;未列出的步骤默认执行完成。"""
    def run(step, ctx):
        return results.get(step.id, ExecResult(observation="执行完成"))
    return MockExecutor(fn=run)
