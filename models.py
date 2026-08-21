from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class ProfileRecord(BaseModel):
    full_name: str
    headline: Optional[str] = "N/A"
    current_company: Optional[str] = "N/A"
    current_title: Optional[str] = "N/A"
    location: Optional[str] = "N/A"
    skills: List[str] = Field(default_factory=list)
    profile_url: str
    scraped_at: str = Field(default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))