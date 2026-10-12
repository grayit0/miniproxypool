"""请求体模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    node_id: Optional[str] = Field(default=None, description="重复注册时带上；首次留空由 manager 分配")
    host: str = Field(description="manager 能直连的地址（容器名或 IP），用于健康探测")
    port: int = Field(description="manager 能直连的代理端口，用于健康探测")
    egress_host: Optional[str] = Field(default=None, description="客户端应该使用的地址，默认同 host")
    egress_port: Optional[int] = Field(default=None, description="客户端应该使用的端口，默认同 port")
    scheme: str = "http"
    region: str = ""
    tags: List[str] = Field(default_factory=list)
    version: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class HeartbeatRequest(BaseModel):
    active_conns: Optional[int] = None
    served: Optional[int] = None
    latency_ms: Optional[float] = None


class ReportBadRequest(BaseModel):
    reason: str = ""


class UpstreamBulkRequest(BaseModel):
    urls: List[str]
    source: str = "manual"
