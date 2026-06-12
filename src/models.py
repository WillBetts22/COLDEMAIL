from typing import List, Literal, Optional
from pydantic import BaseModel


class Contact(BaseModel):
    full_name: str
    title: str
    email: str
    apollo_person_id: Optional[str] = None


class Company(BaseModel):
    name: str
    domain: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    location: Optional[str] = None
    apollo_org_id: Optional[str] = None


class Candidate(BaseModel):
    company: Company
    contact: Contact
    additional_contacts: List["Contact"] = []


class ResearchItem(BaseModel):
    finding: str
    source_url: str
    source_type: Literal["website", "web_search", "apollo"]


class CompanyResearch(BaseModel):
    candidate: Candidate
    research_items: List[ResearchItem] = []
    raw_website_text: str = ""
    website_available: bool = True
    notes: List[str] = []
    medline_mention: bool = False


class EmailDraft(BaseModel):
    subject: str
    body: str


class DraftResult(BaseModel):
    candidate: Candidate
    research: CompanyResearch
    fit_summary: str = ""
    email: Optional[EmailDraft] = None
    draft_failed: bool = False
    draft_error: Optional[str] = None
