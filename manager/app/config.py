"""Manager 配置：全部来自环境变量，方便在 Docker 里覆盖。"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


class Settings:
    """运行时配置。"""

    def __init__(self) -> None:
        # 存储
        self.db_path = os.getenv("DB_PATH", "./data/proxypool.db")

        # 服务监听
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = _int("PORT", 8000)
        self.log_level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()

        # 节点生命周期
        self.heartbeat_interval = _int("HEARTBEAT_INTERVAL", 10)   # 下发给节点的心跳周期（秒）
        self.node_ttl = _float("NODE_TTL", 45.0)                   # 超过该时长没心跳即视为掉线（秒）
        self.fail_threshold = _int("FAIL_THRESHOLD", 2)            # 连续探测失败多少次判定为死亡

        # 主动巡检
        self.check_interval = _float("CHECK_INTERVAL", 15.0)       # 巡检周期（秒）
        self.check_timeout = _float("CHECK_TIMEOUT", 4.0)          # 单次探测超时（秒）
        self.check_concurrency = _int("CHECK_CONCURRENCY", 32)     # 巡检并发度
        self.check_url = os.getenv("CHECK_URL", "http://httpbin.org/ip")  # 校验上游出口用的目标地址
        self.check_upstreams = _bool("CHECK_UPSTREAMS", True)      # 是否主动校验上游代理
        self.max_upstream_check = _int("MAX_UPSTREAM_CHECK", 200)  # 每轮最多校验多少个上游

        # 安全
        self.admin_token = os.getenv("ADMIN_TOKEN", "").strip()    # 为空表示不校验管理接口


settings = Settings()
