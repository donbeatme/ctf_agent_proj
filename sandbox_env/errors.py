"""沙箱环境错误类型。base 与 ssh_backend/tools 共用,避免循环依赖。"""


class SandboxError(Exception):
    """沙箱环境通用错误基类。"""


class SandboxUnavailableError(SandboxError):
    """后端不可用(SSH 未配置/连接失败/容器运行时缺失)。"""


class SandboxExecError(SandboxError):
    """沙箱内命令执行失败(非零退出/超时/通道异常)。"""


class ToolInstallError(SandboxError):
    """工具安装失败(依赖装不上/与 OS 不兼容/校验仍缺失)。"""
