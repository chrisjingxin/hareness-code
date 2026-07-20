"""由 packages/protocol/scripts/generate.ts 生成的 v2 协议模型，请勿手工修改。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_MAJOR = 2
PROTOCOL_MINOR = 2
MAX_FRAME_BYTES = 8388608
MAX_TOOL_PAYLOAD_BYTES = 1048576
CLIENT_METHODS = ["initialize","run.start","run.cancel","config.show","config.path","threads.list","threads.open","skills.list","skills.inspect","skills.set_enabled","skills.install","skills.update","skills.remove","skills.market.list","shutdown"]
SERVER_METHODS = ["event","request"]
SERVER_CAPABILITIES = ["run.cancel","run.multithread","interactive.approval","interactive.question","config.read","threads.read","skills.read","skills.manage"]
EVENT_TYPES = ["run.started","skill.loaded","content.delta","thinking.delta","tool.started","tool.delta","tool.completed","interaction.resolved","run.completed","run.cancelled","run.failed"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ProtocolRange(StrictModel):
    major: Literal[2]
    min_minor: int = Field(ge=0)
    max_minor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> "ProtocolRange":
        if self.min_minor > self.max_minor:
            raise ValueError("min_minor must be <= max_minor")
        return self

class ClientInfo(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)

class InitializeParams(StrictModel):
    protocol: ProtocolRange
    client: ClientInfo
    capabilities: list[str]
    cwd: str | None = None
    config_path: str | None = None

class RequestedSkill(StrictModel):
    id: str = Field(min_length=1)
    args: str = ""

class RunStartParams(StrictModel):
    message: str = Field(min_length=1)
    thread_id: str | None = None
    run_id: str | None = None
    requested_skill: RequestedSkill | None = None

class RunCancelParams(StrictModel):
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)

class ThreadSummary(StrictModel):
    thread_id: str = Field(min_length=1)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    first_message: str
    latest_message: str
    message_count: int = Field(ge=0)

class ThreadMessage(StrictModel):
    kind: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None

class ThreadsListParams(StrictModel):
    limit: int = Field(default=80, ge=1, le=200)

class ThreadsOpenParams(StrictModel):
    thread_id: str = Field(min_length=1)

class EventEnvelope(StrictModel):
    event_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp_ms: int = Field(ge=0)
    source: dict[str, Any] | None = None
    payload: dict[str, Any]
    extensions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_known_payload(self) -> "EventEnvelope":
        fields = {
            "run.started": {"resumed", "skills_snapshot_id"}, "skill.loaded": {"skill_id", "source", "version", "snapshot_id"}, "content.delta": {"text"}, "thinking.delta": {"text"},
            "tool.started": {"tool_call_id", "name"},
            "tool.delta": {"tool_call_id", "arguments_delta", "output_delta", "truncated", "original_bytes"},
            "tool.completed": {"tool_call_id", "result"},
            "interaction.resolved": {"request_id", "type"},
            "run.completed": {"usage", "duration_ms", "finish_reason"},
            "run.cancelled": {"reason"}, "run.failed": {"error"},
        }
        allowed = fields.get(self.type)
        if allowed is not None and set(self.payload) - allowed:
            raise ValueError(f"unexpected payload fields for {self.type}")
        if self.type == "skill.loaded" and (
            not isinstance(self.payload.get("skill_id"), str)
            or not isinstance(self.payload.get("source"), str)
            or not isinstance(self.payload.get("snapshot_id"), str)
        ):
            raise ValueError("invalid skill.loaded payload")
        return self

class InteractionRequestEnvelope(StrictModel):
    request_id: str = Field(min_length=1)
    type: Literal["approval", "question"]
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timeout_ms: int = Field(ge=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self) -> "InteractionRequestEnvelope":
        allowed = ({"interrupt_id", "description", "requests", "decisions"}
                   if self.type == "approval" else {"interrupt_id", "questions"})
        if set(self.payload) - allowed or "interrupt_id" not in self.payload:
            raise ValueError(f"invalid {self.type} payload")
        return self

class ApprovalResponse(StrictModel):
    type: Literal["approval"]
    request_id: str
    decision: Literal["approve_once", "approve_thread", "reject"]
    feedback: str = ""

class QuestionResponse(StrictModel):
    type: Literal["question"]
    request_id: str
    answers: dict[str, list[str]]
