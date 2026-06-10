import os
import sys
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class RunConfig(BaseModel):
    companies_per_run: int = 12
    output_dir: str = "output"
    history_file: str = "data/history.json"


class EmployeeRange(BaseModel):
    min: int = 50
    max: int = 2000


class ICPConfig(BaseModel):
    target_mode: str = "medical_supply_distributors"
    industries: List[str] = []
    keywords: List[str] = []
    employee_range: EmployeeRange = EmployeeRange()
    locations: List[str] = ["United States"]
    contact_titles: List[str] = []
    exclude_keywords: List[str] = []
    blocklist_companies: List[str] = []
    blocklist_domains: List[str] = []


class ResearchConfig(BaseModel):
    scrape_pages_per_company: int = 3
    enable_web_search: bool = True
    max_sources_per_company: int = 8


class DraftingConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1000
    tone: str = "professional, direct, concise"
    word_count_target: int = 130


class AppConfig(BaseModel):
    run: RunConfig
    icp: ICPConfig
    research: ResearchConfig
    drafting: DraftingConfig
    apollo_api_key: str
    anthropic_api_key: str
    firecrawl_api_key: str


def _require_env(key: str) -> str:
    """Load a required env var and exit with a friendly message if missing."""
    val = os.getenv(key, "").strip()
    placeholders = {"paste_your_apollo_key_here", "paste_your_anthropic_key_here", "paste_your_firecrawl_key_here"}
    if not val or val in placeholders:
        hints = {
            "APOLLO_API_KEY": "Apollo key — sign in at apollo.io → Settings → API Keys",
            "ANTHROPIC_API_KEY": "Anthropic key — sign in at console.anthropic.com → API Keys",
            "FIRECRAWL_API_KEY": "Firecrawl key — sign in at firecrawl.dev → Dashboard → API Keys",
        }
        hint = hints.get(key, key)
        print(f"\nERROR: Missing {key}.\n  Open your .env file and paste your {hint} after the equals sign.\n")
        sys.exit(1)
    return val


def load_config(config_path: str = "config.yaml") -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        print(f"\nERROR: config.yaml not found at {path.resolve()}.\n  Make sure you are running from the project root folder.\n")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    icp_raw = raw.get("icp", {})
    er = icp_raw.get("employee_range", {})

    return AppConfig(
        run=RunConfig(**raw.get("run", {})),
        icp=ICPConfig(
            target_mode=icp_raw.get("target_mode", "medical_supply_distributors"),
            industries=icp_raw.get("industries", []),
            keywords=icp_raw.get("keywords", []),
            employee_range=EmployeeRange(min=er.get("min", 50), max=er.get("max", 2000)),
            locations=icp_raw.get("locations", ["United States"]),
            contact_titles=icp_raw.get("contact_titles", []),
            exclude_keywords=icp_raw.get("exclude_keywords", []),
            blocklist_companies=icp_raw.get("blocklist_companies", []),
            blocklist_domains=icp_raw.get("blocklist_domains", []),
        ),
        research=ResearchConfig(**raw.get("research", {})),
        drafting=DraftingConfig(**raw.get("drafting", {})),
        apollo_api_key=_require_env("APOLLO_API_KEY"),
        anthropic_api_key=_require_env("ANTHROPIC_API_KEY"),
        firecrawl_api_key=_require_env("FIRECRAWL_API_KEY"),
    )
