"""ctf-skills 技能库加载器:Agent Skills 目录树 → ws.docs 注册表 + 检索路由。

ctf-skills(https://github.com/ljagiello/ctf-skills)是标准 Agent Skills 库:每个分类目录
一个 SKILL.md(带 YAML frontmatter description,含 quick reference)+ 若干无 frontmatter
的技术 md。vendored 在 skills/ctf-skills/。

本模块做三层适配,把该库喂进 ③ 的文档注册表契约:
- SkillLibrary:静态加载器。扫描目录树 → 目录 {doc_id: SkillMeta(description, path, kind)},
  load_doc(doc_id) 按 id 读全文。doc_id 拍平目录层级且受 schema.ID_PATTERN 约束(禁 '/',
  ≤32 字符):SKILL.md → 分类名(ctf-crypto);子文档 → 分类.stem(ctf-crypto.rsa-attacks)。
- CtfSkillsDocStore(SkillLibrary):实现 planner.DocStore 契约。search(task) 按题面关键词
  路由到分类,只返回命中分类的 SKILL.md(含 quick reference);子文档正文不预灌,经继承的
  load_doc 按需取(planner 的 get_doc 兜底)。
- 文档变换:剥 frontmatter;SKILL.md 首行前置 description(复用 DocsComponent 的 id+首行
  渲染);相对链接 [x.md](x.md) 改写为 [x](doc_id),让 LLM 知道可用 get_doc 取子文档。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from agent.schema import ID_PATTERN

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "ctf-skills"

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([A-Za-z0-9_\-]+\.md)(#[^)\s]*)?\)")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """轻量 YAML frontmatter 解析:只要顶层 `key: value`,续行并入前值,不引 PyYAML。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}
    result: dict[str, str] = {}
    current: str | None = None
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            if key:
                result[key] = value.strip().strip('"')
                current = key
        elif current is not None:
            result[current] += "\n" + stripped
    return result


def _strip_frontmatter(text: str) -> str:
    """剥掉开头的 frontmatter 围栏块,返回正文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return text.strip()
    return "\n".join(lines[end + 1:]).strip()


def _first_heading(path: Path) -> str:
    """子文档无 frontmatter,取首个 # 标题作一句话描述。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return path.stem.replace("-", " ")


def _rewrite_links(text: str, category: str) -> str:
    """相对链接 [label](file.md) → [label](category.file),锚点丢弃(整文件经 get_doc 取)。

    label 若本身带 .md([encodings.md](encodings.md))一并剥掉,只留可读名。
    """

    def _repl(m: re.Match) -> str:
        label, fname = m.group(1), m.group(2)
        if label.endswith(".md"):
            label = label[:-3]
        return f"[{label}]({category}.{Path(fname).stem})"

    return _LINK_RE.sub(_repl, text)


def _task_text(task) -> str:
    """把任务 dict 的字符串字段拼成检索文本(title/description/其余字符串值)。"""
    if not task:
        return ""
    if isinstance(task, str):
        return task
    return " ".join(v for v in task.values() if isinstance(v, str))


@dataclass
class SkillMeta:
    """目录条目:doc_id + 一句话描述 + 源文件。kind: skill(SKILL.md)|tech(子文档)。"""

    doc_id: str
    description: str
    category: str
    path: Path
    kind: str


class SkillLibrary:
    """静态技能库:扫描 Agent Skills 目录树 → doc 目录 + 按 id 读全文。"""

    def __init__(self, root=SKILLS_DIR):
        self.root = Path(root)
        self._catalog: dict[str, SkillMeta] = {}
        self._scan()

    def _scan(self):
        for skill_md in sorted(self.root.glob("*/SKILL.md")):
            category = skill_md.parent.name
            fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            if re.fullmatch(ID_PATTERN, category):
                self._catalog[category] = SkillMeta(
                    category, fm.get("description") or category, category, skill_md, "skill")
            for tech in sorted(skill_md.parent.glob("*.md")):
                if tech.name == "SKILL.md":
                    continue
                doc_id = f"{category}.{tech.stem}"
                if not re.fullmatch(ID_PATTERN, doc_id):
                    continue
                self._catalog[doc_id] = SkillMeta(
                    doc_id, _first_heading(tech), category, tech, "tech")

    def load_doc(self, doc_id: str) -> str | None:
        """按 doc_id 取变换后的全文;不存在返回 None。"""
        meta = self._catalog.get(doc_id)
        if meta is None:
            return None
        return self._transform(meta)

    def _transform(self, meta: SkillMeta) -> str:
        raw = meta.path.read_text(encoding="utf-8")
        if meta.kind == "skill":
            body = _strip_frontmatter(raw)
            body = _rewrite_links(body, meta.category)
            return f"{meta.description}\n\n{body}"
        return _rewrite_links(raw, meta.category)

    @property
    def catalog(self) -> dict[str, SkillMeta]:
        return self._catalog

    def categories(self) -> list[str]:
        """有 SKILL.md 的分类 id 列表(字母序)。"""
        return sorted(m.doc_id for m in self._catalog.values() if m.kind == "skill")

    def descriptions(self) -> dict[str, str]:
        return {doc_id: m.description for doc_id, m in self._catalog.items()}


CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "ctf-web": ["web", "http", "sql", "injection", "sqli", "xss", "ssti", "template",
                "ssrf", "csrf", "jwt", "oauth", "upload", "webshell", "php", "xml", "xxe",
                "deserialization", "pickle", "cors", "cookie", "session", "cve", "rce",
                "lfi", "path traversal", "graphql", "nginx", "apache", "header",
                "web", "http request"],
    "ctf-pwn": ["pwn", "buffer overflow", "stack overflow", "rop", "ret2", "shellcode",
                "format string", "heap", "use after free", "uaf", "canary", "pie",
                "seccomp", "kernel exploit", "binary exploitation", "libc", "got",
                "plt", "gadget", "exploit", "pwn"],
    "ctf-crypto": ["crypto", "cryptography", "rsa", "aes", "cipher", "hash", "sha", "md5",
                   "encrypt", "decrypt", "xor", "ecc", "ecdsa", "dsa", "prng", "random",
                   "lattice", "lwe", "caesar", "vigenere", "signature", "oracle",
                   "padding", "otp", "one-time pad", "cbc", "ecb", "ctr", "gcm",
                   "crypto"],
    "ctf-reverse": ["reverse", "crackme", "disassem", "decompil", "binary analysis",
                    "ghidra", "ida", "angr", "radare", "r2", "unpack", "packer",
                    "obfusc", "anti-debug", "vm", "virtual machine", "frida", "gdb",
                    "symbolic execution", "reverse"],
    "ctf-forensics": ["forensic", "pcap", "wireshark", "tshark", "memory", "volatility",
                      "disk", "file recovery", "stego", "steganography", "lsb", "exif",
                      "audio", "spectrogram", "qr", "hidden file", "ntfs", "image",
                      "forensics"],
    "ctf-osint": ["osint", "recon", "reconnaissance", "geolocation", "social media",
                  "username", "reverse image", "dns", "whois", "shodan", "metadata",
                  "osint"],
    "ctf-malware": ["malware", "c2", "command and control", "rat", "trojan", "beacon",
                    "cobalt strike", "yara", "obfuscated script", "pe", "portable",
                    "api hashing", "sandbox evasion", "malware"],
    "ctf-misc": ["misc", "encoding", "base64", "base32", "hex", "rot13", "qr code",
                 "jail", "pyjail", "bash jail", "sandbox", "privesc", "privilege",
                 "z3", "constraint", "esolang", "brainfuck", "linux", "unix", "ctfd",
                 "dns tunnel", "sdr", "rf", "signal", "misc"],
    "ctf-ai-ml": ["ai", "machine learning", "ml", "model", "neural", "adversarial",
                  "prompt injection", "llm", "jailbreak", "gradient", "backdoor",
                  "poison", "dataset", "ai"],
    "ctf-writeup": [],
    "solve-challenge": [],
}


class CtfSkillsDocStore(SkillLibrary):
    """实现 planner.DocStore 契约:search 按题面关键词路由到分类,返回命中分类的 SKILL.md。

    search 返回 [(doc_id, text)],planner 原样 set_doc(doc_id) 保留可绑定的 id;
    get_doc 兜底走继承的 load_doc(未注册的子文档按需取)。无命中返回 []。
    """

    def __init__(self, root=SKILLS_DIR, top_n=3):
        super().__init__(root)
        self.top_n = top_n

    def search(self, task) -> list[tuple[str, str]]:
        text = _task_text(task).lower()
        scored = []
        for cat, kws in CATEGORY_KEYWORDS.items():
            hits = sum(1 for kw in kws if kw in text)
            if hits and cat in self._catalog:
                scored.append((hits, cat))
        scored.sort(key=lambda t: (-t[0], t[1]))
        top = [cat for _, cat in scored[: self.top_n]]
        return [(cat, self.load_doc(cat)) for cat in top]
