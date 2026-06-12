"""
Research stage: Firecrawl website scraping + DuckDuckGo web search.

Confirmed Firecrawl endpoint (docs.firecrawl.dev, June 2026):
  POST https://api.firecrawl.dev/v2/scrape
  Auth: Authorization: Bearer <api_key>
  Request: { "url": "...", "formats": ["markdown"], "onlyMainContent": true }
  Response: { "success": true, "data": { "markdown": "...", "metadata": { "url": "...", ... } } }

Each scrape costs 1 Firecrawl credit. Plain markdown scrape used — no structured
extraction — to keep credit cost at the minimum 1 credit/page.

Web search uses the duckduckgo-search library (no API key required).
"""
import logging
import re
from typing import List, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import AppConfig
from src.models import Candidate, CompanyResearch, ResearchItem

logger = logging.getLogger("coldemail")

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"

# Internal page paths worth scraping for product/company context
_INTERNAL_KEYWORDS = ["about", "products", "solutions", "services", "company", "who-we-are", "what-we-do", "catalog"]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=20),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    reraise=True,
)
def _firecrawl_scrape(url: str, api_key: str) -> str:
    """Scrape one URL via Firecrawl v2 and return the markdown content."""
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": 30000,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(FIRECRAWL_SCRAPE_URL, json=payload, headers=headers, timeout=60)

    if resp.status_code == 402:
        raise Exception("Firecrawl account has no remaining credits (402). Add credits at firecrawl.dev.")
    if resp.status_code == 429:
        raise requests.exceptions.RequestException("Firecrawl rate limit (429) — backing off.")
    resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Firecrawl returned success=false: {data}")

    return data.get("data", {}).get("markdown", "") or ""


def _find_internal_links(markdown: str, domain: str) -> List[str]:
    """Pull relevant internal page URLs out of homepage markdown."""
    found: List[str] = []
    link_re = re.compile(r"\[([^\]]*)\]\((https?://[^\)]+)\)")
    for m in link_re.finditer(markdown):
        text = m.group(1).lower()
        url = m.group(2)
        url_lower = url.lower()
        if domain and domain.lower() not in url_lower:
            continue
        if any(kw in text or kw in url_lower for kw in _INTERNAL_KEYWORDS):
            if url not in found:
                found.append(url)
    return found[:3]


def _web_search(query: str, max_results: int = 3) -> List[dict]:
    """Run a DuckDuckGo text search. Returns list of {title, href, body} dicts."""
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        logger.warning(f"Web search failed for '{query}': {e}")
        return []


def research_company(candidate: Candidate, config: AppConfig) -> CompanyResearch:
    """
    Research one company via Firecrawl + optional web search.
    Never raises — degrades gracefully so one failure never stops the whole run.
    """
    company = candidate.company
    research_items: List[ResearchItem] = []
    notes: List[str] = []
    raw_parts: List[str] = []
    website_available = True

    # --- Apollo-sourced facts (free, always present) ---
    apollo_facts = []
    if company.industry:
        apollo_facts.append(f"Industry: {company.industry}")
    if company.employee_count:
        apollo_facts.append(f"Employees: {company.employee_count}")
    if company.location:
        apollo_facts.append(f"Location: {company.location}")
    if apollo_facts:
        research_items.append(
            ResearchItem(
                finding="; ".join(apollo_facts),
                source_url="https://app.apollo.io",
                source_type="apollo",
            )
        )

    # --- Firecrawl website scraping ---
    website = company.website
    if not website and company.domain:
        website = f"https://{company.domain}"

    pages_scraped = 0
    max_pages = config.research.scrape_pages_per_company

    if website:
        try:
            homepage_md = _firecrawl_scrape(website, config.firecrawl_api_key)
            if homepage_md:
                pages_scraped += 1
                raw_parts.append(homepage_md)
                research_items.append(
                    ResearchItem(
                        finding=f"Homepage scraped ({len(homepage_md):,} chars)",
                        source_url=website,
                        source_type="website",
                    )
                )

                # Attempt to scrape relevant internal pages
                if pages_scraped < max_pages:
                    domain = company.domain or ""
                    for link in _find_internal_links(homepage_md, domain):
                        if pages_scraped >= max_pages:
                            break
                        try:
                            page_md = _firecrawl_scrape(link, config.firecrawl_api_key)
                            if page_md:
                                pages_scraped += 1
                                raw_parts.append(page_md)
                                page_label = link.rstrip("/").split("/")[-1] or "page"
                                research_items.append(
                                    ResearchItem(
                                        finding=f"Internal page '/{page_label}' scraped ({len(page_md):,} chars)",
                                        source_url=link,
                                        source_type="website",
                                    )
                                )
                        except Exception as e:
                            logger.debug(f"Skipped internal page {link}: {e}")

        except Exception as e:
            website_available = False
            msg = f"Website unavailable ({website}): {e}"
            notes.append(msg)
            logger.warning(f"{company.name} — {msg}")
    else:
        website_available = False
        notes.append("No website URL available — sourced from Apollo data only.")

    # --- Web search for recent signals ---
    if config.research.enable_web_search:
        for query in [
            f'"{company.name}" medical supply distributor packaging',
            f'"{company.name}" specimen bags biohazard',
        ]:
            if len(research_items) >= config.research.max_sources_per_company:
                break
            for result in _web_search(query, max_results=2):
                if len(research_items) >= config.research.max_sources_per_company:
                    break
                href = result.get("href", "")
                body = result.get("body", "")
                if href and body:
                    research_items.append(
                        ResearchItem(
                            finding=body[:300],
                            source_url=href,
                            source_type="web_search",
                        )
                    )

    # Cap website text passed to Claude to keep prompt size reasonable
    raw_text = "\n\n---\n\n".join(raw_parts)[:8000]

    # Detect Medline partnership — check all scraped text and web findings
    all_research_text = raw_text + " " + " ".join(item.finding for item in research_items)
    medline_mention = bool(re.search(r'\bmedline\b', all_research_text, re.IGNORECASE))
    if medline_mention:
        notes.append("Medline partner detected — company mentions Medline in their content.")
        logger.warning(f"{company.name} — Medline mention detected; will be added to blocklist.")

    return CompanyResearch(
        candidate=candidate,
        research_items=research_items[: config.research.max_sources_per_company],
        raw_website_text=raw_text,
        website_available=website_available,
        notes=notes,
        medline_mention=medline_mention,
    )
