from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.runtimes.contracts import AgentRuntimeKind

AgentResourceType = Literal["skill", "general_skill", "knowledge_base", "tool"]
AgentWorkRecordEventKind = Literal["chat", "task", "sop", "tool", "knowledge", "skill"]
AgentWorkRecordEventPhase = Literal["reply", "last_run", "next_run", "assigned"]


class AgentProfileCreateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool = False
    source_mode: Literal["copy", "blank"] = "copy"
    copy_from_agent_id: Optional[str] = None
    runtime: AgentRuntimeKind = AgentRuntimeKind.NATIVE
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    runtime: AgentRuntimeKind | None = None
    runtime_config: dict[str, Any] | None = None
    metadata: Optional[dict[str, Any]] = None


class AgentResourceBindingRead(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    resource_type: AgentResourceType
    resource_id: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentProfileRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool
    status: str
    runtime: str = "native"
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[AgentResourceBindingRead] = Field(default_factory=list)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentScopeRead(BaseModel):
    tenant_id: str
    agents: list[AgentProfileRead] = Field(default_factory=list)


class AgentWorkRecordReplyStatsRead(BaseModel):
    total: int = 0
    today: int = 0
    by_day: dict[str, int] = Field(default_factory=dict)


class AgentWorkRecordEventRead(BaseModel):
    id: str
    kind: AgentWorkRecordEventKind
    phase: AgentWorkRecordEventPhase
    timestamp: str
    label: str = ""


class AgentWorkRecordRead(BaseModel):
    agent_id: str
    timezone: str
    generated_at: str
    reply_stats: AgentWorkRecordReplyStatsRead
    events: list[AgentWorkRecordEventRead] = Field(default_factory=list)


class AgentResourceBindingInput(BaseModel):
    resource_type: AgentResourceType
    resource_id: str
    status: Literal["active", "inactive"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResourcesUpdateRequest(BaseModel):
    tenant_id: str
    resources: list[AgentResourceBindingInput] = Field(default_factory=list)


class AgentResourceImportRequest(BaseModel):
    tenant_id: str
    source_agent_id: str
    resource_type: AgentResourceType
    resource_ids: list[str] = Field(default_factory=list)


class AgentModelBindingInput(BaseModel):
    role: Literal["default", "router", "step", "response", "general_skill"]
    model_config_id: str


class AgentModelsUpdateRequest(BaseModel):
    tenant_id: str
    bindings: list[AgentModelBindingInput] = Field(default_factory=list)


class AgentSkillRollbackRequest(BaseModel):
    tenant_id: str
    version: str
