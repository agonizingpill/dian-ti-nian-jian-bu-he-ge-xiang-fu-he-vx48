from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field

class LoginIn(BaseModel):
    email: str
    password: str
class LiftIn(BaseModel):
    asset_code: str = Field(min_length=3)
    building: str
    address: str
    manufacturer: str
    model: str
    rated_load: int = Field(gt=0)
    installed_year: int = Field(ge=1980, le=2100)
    inspection_due: date
class ItemIn(BaseModel):
    code: str
    title: str
    category: str
    critical: bool = False
    score: float = Field(ge=0, le=100)
    result: str
    notes: Optional[str] = ""
    photo_ref: Optional[str] = ""
class NoticeIn(BaseModel):
    inspection_item_id: int
    issue_summary: str
    severity: str
    deadline: date
    evidence_required: str
class RecheckIn(BaseModel):
    notice_id: int
    scheduled_at: datetime
    inspector: str
    photo_ref: str
    photo_item_code: str
    remarks: Optional[str] = ""
class RecheckReviewIn(BaseModel):
    result: str
    remarks: Optional[str] = ""
