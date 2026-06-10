"""
Dedupe and blocklist enforcement stage.

Blocklist matching is intentionally conservative: over-exclusion of a lookalike
is acceptable; accidentally emailing a blocked company (e.g. Interplast, a Medline
competitor) could damage a real business relationship.

Matching rules:
  1. Normalize names: lowercase, strip corporate suffixes and punctuation, collapse whitespace.
  2. Substring match: if the normalized blocklist entry appears anywhere in the normalized
     company name, block it.
  3. Domain check: if a blocklist company name token appears in the candidate's domain, block it.
  4. Explicit domain blocklist: exact normalized-domain match.
"""
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from src.config import AppConfig
from src.models import Candidate

logger = logging.getLogger("coldemail")

# Corporate suffixes stripped before name comparison
_SUFFIX_RE = re.compile(
    r"\b(inc\.?|llc\.?|ltd\.?|group|corp\.?|co\.?|company|holdings|"
    r"international|intl\.?|partners|enterprises|solutions|services|"
    r"technologies|tech|industries|distribution|distributing|supply|"
    r"medical|health|healthcare)\b",
    re.IGNORECASE,
)


def _norm_name(name: str) -> str:
    """Lowercase, strip suffixes and punctuation, collapse whitespace."""
    name = name.lower().strip()
    name = _SUFFIX_RE.sub("", name)
    name = re.sub(r"[^a-z0-9\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _norm_domain(domain: str) -> str:
    domain = domain.lower().strip()
    domain = re.sub(r"^https?://", "", domain)
    domain = re.sub(r"^www\.", "", domain)
    return domain.split("/")[0].strip()


def _blocklist_match(
    company_name: str,
    domain: Optional[str],
    blocklist_names: List[str],
    blocklist_domains: List[str],
) -> Optional[str]:
    """
    Return the matched blocklist entry string if the company should be blocked, else None.
    Checks normalized name substring and domain-based matches.
    """
    norm_candidate = _norm_name(company_name)

    for entry in blocklist_names:
        norm_entry = _norm_name(entry)
        if not norm_entry:
            continue
        # Substring match in either direction (entry in name, or name in entry)
        if norm_entry in norm_candidate or norm_candidate in norm_entry:
            return entry
        # Token overlap: any word from the blocklist entry appearing in the candidate name
        entry_tokens = set(norm_entry.split())
        candidate_tokens = set(norm_candidate.split())
        if entry_tokens & candidate_tokens:
            return entry

    if domain:
        norm_d = _norm_domain(domain)
        for entry in blocklist_names:
            token = _norm_name(entry).replace(" ", "")
            if token and token in norm_d:
                return entry
        for blocked_domain in blocklist_domains:
            if _norm_domain(blocked_domain) in norm_d:
                return blocked_domain

    return None


def _load_history(history_file: str) -> List[dict]:
    path = Path(history_file)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not read history file ({e}). Starting with empty history.")
        return []


def filter_candidates(
    candidates: List[Candidate],
    config: AppConfig,
) -> Tuple[List[Candidate], List[Candidate], List[Tuple[Candidate, str]]]:
    """
    Split candidates into (new, already_seen, blocklisted).
    blocklisted entries are (candidate, matched_entry) tuples.
    Blocklist check runs first to avoid burning research credits on blocked companies.
    """
    history = _load_history(config.run.history_file)
    seen_domains = {_norm_domain(r.get("company_domain", "")) for r in history if r.get("company_domain")}
    seen_emails = {r.get("contact_email", "").lower() for r in history if r.get("contact_email")}

    new_candidates: List[Candidate] = []
    already_seen: List[Candidate] = []
    blocklisted: List[Tuple[Candidate, str]] = []

    for candidate in candidates:
        # --- Blocklist check (must happen before history check) ---
        match = _blocklist_match(
            candidate.company.name,
            candidate.company.domain,
            config.icp.blocklist_companies,
            config.icp.blocklist_domains,
        )
        if match:
            logger.warning(f"BLOCKED (matched blocklist entry '{match}'): {candidate.company.name}")
            blocklisted.append((candidate, match))
            continue

        # --- History / dedupe check ---
        candidate_domain = _norm_domain(candidate.company.domain or "")
        candidate_email = candidate.contact.email.lower()

        if candidate_domain and candidate_domain in seen_domains:
            logger.debug(f"Skipping (domain seen before): {candidate.company.name}")
            already_seen.append(candidate)
            continue

        if candidate_email in seen_emails:
            logger.debug(f"Skipping (email seen before): {candidate.contact.email}")
            already_seen.append(candidate)
            continue

        new_candidates.append(candidate)

    return new_candidates, already_seen, blocklisted


def write_history(candidates: List[Candidate], run_id: str, config: AppConfig) -> None:
    """Atomically append successfully processed candidates to history.json."""
    history_path = Path(config.run.history_file)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_history(config.run.history_file)
    now = datetime.utcnow().isoformat()

    for c in candidates:
        existing.append(
            {
                "company_domain": c.company.domain or "",
                "company_name": c.company.name,
                "contact_email": c.contact.email.lower(),
                "first_seen_run_id": run_id,
                "date": now,
            }
        )

    # Atomic write: write to a temp file in the same directory, then rename
    dir_ = history_path.parent
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
        ) as tf:
            json.dump(existing, tf, indent=2)
            tmp_path = tf.name
        os.replace(tmp_path, history_path)
    except Exception as e:
        logger.error(f"Failed to write history file: {e}")
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
