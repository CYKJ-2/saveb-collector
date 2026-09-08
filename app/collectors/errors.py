# 统一采集异常类型，调用方用错误码决定任务失败或重试。
class CollectionError(Exception):
    """Safe code only; never expose upstream bodies, credentials or cookies."""
