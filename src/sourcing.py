"""
Apollo sourcing stage.

Confirmed endpoint (docs.apollo.io/reference/people-api-search, June 2026):
  POST https://api.apollo.io/api/v1/mixed_people/api_search
  Auth: Authorization: Bearer <master_api_key>
  Key filters: person_titles[], organization_locations[], organization_num_employees_ranges[],
               q_keywords (string)

  Note: this endpoint does NOT return emails. A separate enrichment call is required:
  POST https://api.apollo.io/api/v1/people/match
  with { id, reveal_personal_emails: true } — this call consumes credits.
"""
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import AppConfig
from src.models import Candidate, Company, Contact

logger = logging.getLogger("coldemail")

APOLLO_BASE = "https://api.apollo.io/api/v1"


class ApolloError(Exception):
    """Non-retryable Apollo API error (bad key, plan tier, etc.)."""


def _headers(api_key: str) -> dict:
    # Apollo uses X-Api-Key header (Bearer token removed Sept 2024 per Apollo docs)
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _handle_status(resp: requests.Response) -> None:
    """Raise descriptive errors for known Apollo HTTP codes."""
    if resp.status_code == 401:
        raise ApolloError(
            "Apollo API key rejected (401). Check that APOLLO_API_KEY in your .env is correct and active."
        )
    if resp.status_code == 403:
        raise ApolloError(
            "Apollo returned 403 Forbidden. This endpoint may require a higher-tier Apollo plan. "
            "Check your subscription at apollo.io/settings/billing."
        )
    if resp.status_code == 429:
        # Raise a retryable exception so tenacity will back off and retry
        raise requests.exceptions.RequestException("Apollo rate limit hit (429) — backing off.")
    resp.raise_for_status()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    reraise=True,
)
def _apollo_post(url: str, payload: dict, api_key: str) -> dict:
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=30)
    _handle_status(resp)
    return resp.json()


def _extract_org(person: dict) -> dict:
    """Safely pull the organization dict from a person record."""
    org = person.get("organization")
    if isinstance(org, dict):
        return org
    # Some API responses nest it differently
    emp_history = person.get("employment_history")
    if isinstance(emp_history, list) and emp_history:
        return emp_history[0] if isinstance(emp_history[0], dict) else {}
    return {}


def _clean_domain(raw: str) -> Optional[str]:
    if not raw:
        return None
    domain = raw.lower().replace("https://", "").replace("http://", "").split("/")[0].strip()
    return domain or None


def _build_candidate(person: dict, email: str) -> Optional[Candidate]:
    """Build a Candidate from a raw Apollo person dict + resolved email."""
    name = person.get("name", "").strip()
    if not name:
        first = person.get("first_name", "")
        last = person.get("last_name", "")
        name = f"{first} {last}".strip()
    if not name:
        return None

    title = person.get("title", "")
    person_id = person.get("id")

    org = _extract_org(person)
    org_name = org.get("name", "") or person.get("organization_name", "")
    if not org_name:
        return None

    domain = _clean_domain(org.get("primary_domain") or org.get("website_url", ""))
    website = org.get("website_url") or (f"https://{domain}" if domain else None)

    # employee count: try several field names Apollo uses
    emp = (
        org.get("num_employees")
        or org.get("estimated_num_employees")
        or org.get("headcount")
    )
    try:
        emp = int(emp) if emp else None
    except (ValueError, TypeError):
        emp = None

    location_parts = [
        org.get("city", ""),
        org.get("state", ""),
        org.get("country", ""),
    ]
    location = ", ".join(p for p in location_parts if p) or None

    company = Company(
        name=org_name,
        domain=domain,
        website=website,
        industry=org.get("industry"),
        employee_count=emp,
        location=location,
        apollo_org_id=org.get("id"),
    )
    contact = Contact(
        full_name=name,
        title=title,
        email=email,
        apollo_person_id=person_id,
    )
    return Candidate(company=company, contact=contact)


def search_candidates(config: AppConfig, target_count: int) -> Tuple[List[Candidate], int]:
    """
    Search Apollo for people matching the ICP, enrich to get verified emails.
    Returns (candidates, credit_calls_made).
    Over-fetches ~2x to absorb duplicates lost in the dedupe stage.
    """
    api_key = config.apollo_api_key
    icp = config.icp

    fetch_target = target_count * 2
    per_page = min(25, fetch_target)
    pages_needed = max(1, -(-fetch_target // per_page))  # ceiling division

    emp_range = f"{icp.employee_range.min},{icp.employee_range.max}"

    # Combine industries + keywords for q_organization_keyword_tags (confirmed array format, June 2026)
    all_terms = list(dict.fromkeys(icp.industries + icp.keywords))  # deduplicated, order preserved

    all_people: List[dict] = []

    for page in range(1, pages_needed + 1):
        payload = {
            "page": page,
            "per_page": per_page,
            "person_titles": icp.contact_titles,
            "organization_locations": icp.locations,
            "organization_num_employees_ranges": [emp_range],
            "q_organization_keyword_tags": all_terms,
        }
        logger.info(f"Apollo search — page {page}/{pages_needed} (fetching up to {per_page} per page)")
        try:
            data = _apollo_post(f"{APOLLO_BASE}/mixed_people/api_search", payload, api_key)
        except ApolloError:
            raise  # propagate immediately; not retryable
        except Exception as e:
            logger.error(f"Apollo search failed on page {page}: {e}")
            break

        people = data.get("people", [])
        if not people:
            logger.info("Apollo returned no more results.")
            break
        all_people.extend(people)
        if len(all_people) >= fetch_target:
            break

    logger.info(f"Apollo search returned {len(all_people)} raw person records.")

    # Enrich each person to get a verified email (credit-consuming step)
    credit_cap = target_count * 2
    credit_calls = 0
    candidates: List[Candidate] = []
    no_email_dropped = 0

    for person in all_people:
        if credit_calls >= credit_cap:
            logger.warning(f"Email reveal credit cap reached ({credit_cap}). Stopping enrichment early.")
            break

        person_id = person.get("id")
        if not person_id:
            continue

        # Always enrich — search results have obfuscated names and sparse org data.
        # Enrichment returns the full name, email, and complete organization object.
        try:
            enrich_data = _apollo_post(
                f"{APOLLO_BASE}/people/match",
                {"id": person_id, "reveal_personal_emails": True},
                api_key,
            )
            credit_calls += 1
            enriched = enrich_data.get("person", {}) or {}
        except ApolloError:
            raise
        except Exception as e:
            logger.warning(f"Enrichment failed for person {person_id}: {e}")
            no_email_dropped += 1
            continue

        email = enriched.get("email", "")
        if not email or "@" not in email:
            logger.debug(f"No email resolved for person {person_id} — skipping.")
            no_email_dropped += 1
            continue

        # Build candidate from enriched data (has full name + complete org)
        candidate = _build_candidate(enriched, email)
        if candidate:
            candidates.append(candidate)

    if no_email_dropped:
        logger.info(f"Dropped {no_email_dropped} contact(s) with no resolvable email.")
    logger.info(f"Built {len(candidates)} candidate records. Apollo credit calls made: {credit_calls}.")

    # Group by company domain (or name fallback), keep primary + up to 2 additional contacts
    grouped: Dict[str, List[Candidate]] = defaultdict(list)
    for cand in candidates:
        key = cand.company.domain or cand.company.name.lower()
        grouped[key].append(cand)

    final_candidates: List[Candidate] = []
    for key, group in grouped.items():
        primary = group[0]
        extras = group[1:3]
        final_candidates.append(
            Candidate(
                company=primary.company,
                contact=primary.contact,
                additional_contacts=[c.contact for c in extras],
            )
        )

    logger.info(f"Collapsed to {len(final_candidates)} unique companies (with up to 2 additional contacts each).")
    return final_candidates, credit_calls
