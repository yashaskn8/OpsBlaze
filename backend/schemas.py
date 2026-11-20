from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_id: str = Field(min_length=1, max_length=256)


class IncidentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_id: str
    status_projection: str
    status_version: int
    created_at: datetime
