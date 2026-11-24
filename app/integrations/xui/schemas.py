"""Pydantic schemas for 3x-ui v3.11 interactions."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ClientSpec(BaseModel):
    id: str # UUID
    email: str
    flow: str = "xtls-rprx-vision"
    limitIp: int = 2
    totalGB: int = 0 # in bytes: totalGB = quota_bytes (3x-ui uses totalGB as bytes in raw json)
    expiryTime: int = 0 # Unix timestamp in milliseconds
    enable: bool = True
    tgId: int = 0
    subId: str = "" # Subscription ID
    password: Optional[str] = None
    alterId: Optional[int] = 0
    method: Optional[str] = None

    model_config = {"extra": "allow"}

    def model_dump(self, *args, **kwargs) -> Dict[str, Any]:
        d = super().model_dump(*args, **kwargs)
        return {k: v for k, v in d.items() if v is not None}


class InboundClientData(BaseModel):
    id: str
    email: str
    flow: Optional[str] = ""
    limit_ip: Optional[int] = 2
    total_bytes: int = 0
    up: int = 0
    down: int = 0
    expiry_time: int = 0
    enable: bool = True
    sub_id: Optional[str] = ""


class InboundData(BaseModel):
    id: int
    up: int = 0
    down: int = 0
    total: int = 0
    remark: str
    enable: bool
    port: int
    protocol: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    stream_settings: Dict[str, Any] = Field(default_factory=dict)
    tag: str = ""
    listen: str = "0.0.0.0"
    clients: List[InboundClientData] = Field(default_factory=list)


class PanelServerStatus(BaseModel):
    cpu: float = 0.0
    mem_current: int = 0
    mem_total: int = 0
    disk_current: int = 0
    disk_total: int = 0
    uptime: int = 0
    xray_state: str = "running"
    xray_version: str = "1.8.x"
    raw: Dict[str, Any] = Field(default_factory=dict)


class ClientTrafficStats(BaseModel):
    email: str
    up: int = 0
    down: int = 0
    total: int = 0 # Quota
    expiry_time: int = 0
    enable: bool = True
