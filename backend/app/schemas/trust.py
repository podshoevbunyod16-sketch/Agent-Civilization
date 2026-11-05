"""Схемы API доверия и верификации (этап 5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.trust import (
    AnomalyStatus,
    AnomalyType,
    AppealStatus,
    ReputationSource,
    Verdict,
    VerificationMethod,
    VerificationStatus,
)


class VerificationCheckConfig(BaseModel):
    """Конфигурация автоматических проверок результата."""

    output_schema: dict[str, Any] = Field(default_factory=dict)
    assertions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    test_code: str = Field(default="", max_length=20000)
    resource_limits: dict[str, Any] | None = None

    @field_validator("assertions")
    @classmethod
    def _limit_assertions(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > 100:
            raise ValueError("Слишком много утверждений (максимум 100)")
        return value


class RunChecksRequest(VerificationCheckConfig):
    pass


class PeerVerificationRequest(BaseModel):
    verifier_agent_id: uuid.UUID
    check_config: VerificationCheckConfig | None = None


class HumanVerificationRequest(BaseModel):
    passed: bool
    comment: str = Field(default="", max_length=1000)


class VerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    agent_id: uuid.UUID
    method: VerificationMethod
    status: VerificationStatus
    weight: float
    passed: bool | None
    score: float | None
    details: dict[str, Any]
    error: dict[str, Any] | None
    verifier_id: uuid.UUID | None
    verifier_agent_id: uuid.UUID | None
    duration_ms: int | None
    created_at: datetime
    finished_at: datetime | None


class VerdictRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: uuid.UUID
    agent_id: uuid.UUID
    verdict: Verdict
    score: float | None
    checks_total: int
    checks_passed: int
    independent_checks: int
    decided_at: datetime | None
    details: dict[str, Any]


class VerificationResult(BaseModel):
    """Результат запуска проверок: что создано + итоговый вердикт."""

    checks: list[VerificationRead]
    verdict: VerdictRead | None


class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=4000)
    confirm_result: bool = False


class ReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    agent_id: uuid.UUID
    reviewer_id: uuid.UUID
    rating: int
    comment: str
    confirmed_result: bool
    created_at: datetime


class RatingSummary(BaseModel):
    agent_id: uuid.UUID
    reviews: int
    average_rating: float | None
    confirmed_results: int
    distribution: dict[str, int]


class ReputationEventRead(BaseModel):
    id: uuid.UUID
    source: str
    delta: float
    weight: float
    reason: str
    task_id: uuid.UUID | None
    reversed_at: datetime | None
    created_at: datetime


class ReputationBreakdown(BaseModel):
    agent_id: uuid.UUID
    reputation_score: float
    formula_score: float
    positive: float
    negative: float
    by_source: dict[str, dict[str, float]]
    events: list[ReputationEventRead]
    verified_results: int


class CapabilityTrust(BaseModel):
    """Заявленные против проверенных возможностей."""

    id: uuid.UUID
    key: str
    name: str
    declared_level: int
    verified_level: int | None
    verified_at: datetime | None
    trust_level: Literal["verified", "declared", "unconfirmed"]


class AgentTrustProfile(BaseModel):
    agent_id: uuid.UUID
    agent_name: str
    reputation_score: float
    is_verified: bool
    completed_tasks: int
    failed_tasks: int
    rating: RatingSummary
    verdicts: dict[str, int]
    capabilities: list[CapabilityTrust]
    open_anomalies: int
    open_appeals: int
    reputation: ReputationBreakdown | None = None


class AppealCreate(BaseModel):
    agent_id: uuid.UUID
    reason: str = Field(min_length=5, max_length=8000)
    task_id: uuid.UUID | None = None
    verification_id: uuid.UUID | None = None
    anomaly_id: uuid.UUID | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class AppealResolve(BaseModel):
    accept: bool
    resolution: str = Field(default="", max_length=8000)


class AppealRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    opened_by_id: uuid.UUID
    task_id: uuid.UUID | None
    verification_id: uuid.UUID | None
    anomaly_id: uuid.UUID | None
    reason: str
    evidence: dict[str, Any]
    status: AppealStatus
    resolution: str
    resolved_by_id: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime


class AnomalyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    anomaly_type: AnomalyType
    status: AnomalyStatus
    severity: float
    details: dict[str, Any]
    task_ids: list[str]
    detected_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class AnomalyResolve(BaseModel):
    confirm: bool


class AnomalyScanResult(BaseModel):
    agent_id: uuid.UUID
    found: list[AnomalyRead]


class TrustOverview(BaseModel):
    verdicts: dict[str, int]
    checks: dict[str, dict[str, int]]
    open_appeals: int
    open_anomalies: int
    verified_agents: int
    total_agents: int
    verified_capabilities: int


class ReputationSourceInfo(BaseModel):
    source: ReputationSource
    weight: float
    description: str


__all__ = [
    "AgentTrustProfile",
    "AnomalyRead",
    "AnomalyResolve",
    "AnomalyScanResult",
    "AppealCreate",
    "AppealRead",
    "AppealResolve",
    "CapabilityTrust",
    "HumanVerificationRequest",
    "PeerVerificationRequest",
    "RatingSummary",
    "ReputationBreakdown",
    "ReputationSourceInfo",
    "ReviewCreate",
    "ReviewRead",
    "RunChecksRequest",
    "TrustOverview",
    "VerdictRead",
    "VerificationRead",
    "VerificationResult",
]
