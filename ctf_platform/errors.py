"""适配器错误类型。base 与 storage 共用,避免循环依赖。"""


class AdapterError(Exception):
    """适配器通用错误基类。"""


class AuthError(AdapterError):
    """鉴权失败(401/403 或缺少凭证)。"""


class DownloadError(AdapterError):
    """附件下载失败(全部 URL 模板耗尽)。"""


class CacheIntegrityError(AdapterError):
    """附件缓存完整性校验失败(如 md5 不符)。"""


class ParseError(AdapterError):
    """输入(URL/JSON)解析失败。"""
