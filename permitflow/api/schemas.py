"""Request and response models."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CreateApplication(BaseModel):
    applicant_id: UUID
    parcel_id: UUID
    permit_type_code: str
    scope_narrative: str = Field(min_length=10)
    declared_valuation: Decimal = Field(ge=0)
    contractor_id: UUID | None = None
    square_feet: int | None = Field(default=None, gt=0)
    occupancy_class: str | None = None


class ApplicationCreated(BaseModel):
    application_id: UUID
    application_number: str
    status: str


class ReturnIncomplete(BaseModel):
    reasons: list[str] = Field(min_length=1, description="Itemized, one per deficiency (FR-05)")


class AcceptIntake(BaseModel):
    confirmed_additional_disciplines: list[str] = Field(
        default_factory=list,
        description="Discipline codes the clerk confirmed on top of the standing rules",
    )


class DocumentReceived(BaseModel):
    filename: str


class WaiveDocument(BaseModel):
    reason: str = Field(min_length=3)


class Deny(BaseModel):
    reason: str = Field(min_length=3, description="Required by FR-19")


class Withdraw(BaseModel):
    reason: str | None = None


class Reassign(BaseModel):
    reason: str = Field(min_length=3, description="Required by FR-13")


class ApproveWithConditions(BaseModel):
    conditions: list[str] = Field(min_length=1)


class DeficiencyIn(BaseModel):
    code_reference: str = Field(min_length=1, description="The provision the deficiency cites")
    description: str = Field(min_length=1)
    severity: str = Field(default="MAJOR", pattern="^(MAJOR|MINOR)$")


class RecordDeficiencies(BaseModel):
    deficiencies: list[DeficiencyIn] = Field(min_length=1)


class TaskNote(BaseModel):
    note: str | None = None


class AiDecision(BaseModel):
    accepted: bool
    override_payload: dict[str, Any] | None = None


class ApplicationView(BaseModel):
    """Read model for the application detail screen and for US-02."""

    application_id: UUID
    application_number: str
    status: str
    waiting_on: str
    clock_paused: bool
    permit_type_name: str
    applicant_name: str
    situs_address: str
    zoning_code: str | None
    declared_valuation: Decimal
    parcel_verified: bool
    license_verified: bool
    open_task_count: int
    deficient_task_count: int
    missing_document_count: int
    sla_state: str
    net_business_days_elapsed: int | None
    allowance_days: int
    days_remaining: int | None


class ProblemDetail(BaseModel):
    """Error body. `reasons` carries every failed guard, not just the first."""

    detail: str
    reasons: list[str] = Field(default_factory=list)
