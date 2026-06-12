"""
Drafting stage: one Claude API call per company produces a structured JSON response
containing a fit_summary and a cold email (subject + body).

The model is instructed to return ONLY valid JSON. If parsing fails on the first
attempt, one retry with a stricter instruction is made. If both fail, the row is
marked DRAFT FAILED so the user can see it needs manual attention.
"""
import json
import logging
import re
from typing import Optional

import anthropic

from src.config import AppConfig
from src.models import CompanyResearch, DraftResult, EmailDraft

logger = logging.getLogger("coldemail")

# ── Fixed Seng Seng pitch — verbatim from the brief ──────────────────────────
SENG_SENG_PITCH = """
COMPANY: Seng Seng Plastic manufactures flexible plastic packaging — LDPE/PE bags,
CPP bags, laminated bags, zipper bags, and specialty bags — standard and custom-designed,
for food, retail, agriculture, consumer goods, and the medical industry.

PROBLEM SOLVED: Packaging that protects products in storage/shipping, extends shelf life,
meets safety/regulatory requirements, and is fully customizable without the client investing
in their own manufacturing equipment.

MEDICAL FOCUS: Primary medical product is the specimen transport bag (zip-style with biohazard
symbol) — two-pouch design separating specimen paperwork from the sample to reduce contamination
risk, with secure zip closure, printed biohazard labeling, durable construction up to 2 mil thick.
Also offers fully custom medical packaging tailored to operational, safety, and regulatory needs.

DIFFERENTIATORS:
- Specialization in flexible packaging (PE, CPP, laminated, zipper)
- In-house R&D and custom product development
- Custom sizes/designs/printing; multi-color printing up to 10 colors per side
- Large-scale manufacturing capacity

KEY CREDENTIALS (lead with these for medical audiences):
- Established 1986; manufacturing in Guangdong, China since 1995
- ~50,000 sq meter factory; ~250 production machines; ~1,500 tons/month capacity
- FDA-CERTIFIED MATERIALS for packaging production
- ISO 9001 CERTIFIED manufacturing
- EXISTING MEDICAL CLIENT: A major national medical distributor (confidential) — currently supplies their
  zip-style specimen bags. Reference as "a leading national medical distributor" if social proof is needed,
  but NEVER name the client.
"""

SYSTEM_PROMPT = """You are a B2B sales copywriter helping Seng Seng Plastic reach medical supply distributors.
Write a personalized cold email and a 2-3 sentence fit summary.

Return ONLY a valid JSON object — no markdown fences, no preamble, no explanation.
Exact required shape:
{
  "fit_summary": "2-3 sentences on why this specific company would benefit from Seng Seng's medical packaging. Ground every claim in the research provided.",
  "email": {
    "subject": "concise subject line under 60 characters",
    "body": "full email body text"
  }
}"""


def _build_user_prompt(research: CompanyResearch, tone: str, word_count: int) -> str:
    candidate = research.candidate
    company = candidate.company
    contact = candidate.contact

    # Build research context block
    research_blocks: list[str] = []

    if research.raw_website_text:
        research_blocks.append(f"WEBSITE CONTENT (excerpts):\n{research.raw_website_text[:3000]}")

    web_findings = [
        f"  - {item.finding[:200]} (source: {item.source_url})"
        for item in research.research_items
        if item.source_type == "web_search" and item.finding
    ]
    if web_findings:
        research_blocks.append("WEB SEARCH FINDINGS:\n" + "\n".join(web_findings[:4]))

    apollo_facts = [
        f"  - {item.finding}"
        for item in research.research_items
        if item.source_type == "apollo"
    ]
    if apollo_facts:
        research_blocks.append("APOLLO DATA:\n" + "\n".join(apollo_facts))

    if research.notes:
        research_blocks.append("RESEARCH NOTES: " + "; ".join(research.notes))

    research_text = "\n\n".join(research_blocks) if research_blocks else "No website data available — use Apollo data and industry knowledge only."

    return f"""Write a cold email and fit summary for this prospect.

RECIPIENT:
  Name: {contact.full_name}
  Title: {contact.title}
  Company: {company.name}
  Industry: {company.industry or "medical supply distribution"}
  Location: {company.location or "United States"}
  Employees: {company.employee_count or "unknown"}
  Website: {company.website or "N/A"}

RESEARCH:
{research_text}

SENG SENG PITCH CONTEXT:
{SENG_SENG_PITCH}

INSTRUCTIONS:
1. LEAD with FDA-certified materials + ISO 9001 certification as the trust anchors.
   These are the gatekeepers for medical procurement audiences. If social proof is needed,
   reference "a leading national medical distributor" — never name the client.
2. Open the email with ONE specific, real observation about {company.name} drawn only from
   the research above. Never fabricate a fact.
3. Target approximately {word_count} words for the email body. Tone: {tone}.
4. One clear CTA — request a short intro call OR offer to send samples/spec sheet.
5. Sign off as: Danny Chen
6. Fit summary: 2-3 sentences, grounded only in the research provided.
   Answer: why is {company.name} a plausible buyer of medical packaging specifically?
7. If website data was unavailable, personalize using industry knowledge and Apollo data only.
8. Return ONLY the JSON object. No markdown. No extra text.
9. CRITICAL: Do NOT mention "Medline" anywhere in the email — not as a reference, not as
   a proof point, not anywhere. Use "a leading national medical distributor" instead."""


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences that the model sometimes wraps JSON in."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def draft_email(
    research: CompanyResearch,
    config: AppConfig,
    client: anthropic.Anthropic,
) -> DraftResult:
    """
    Call Claude to produce a fit summary + cold email for one company.
    Tries up to twice (second attempt uses a stricter JSON-only instruction).
    Never raises — marks the row as DRAFT FAILED instead.
    """
    candidate = research.candidate
    user_prompt = _build_user_prompt(research, config.drafting.tone, config.drafting.word_count_target)
    raw_content: str = ""

    for attempt in range(2):
        extra = (
            ""
            if attempt == 0
            else "\n\nIMPORTANT: Your previous response could not be parsed as JSON. "
            "Return ONLY the raw JSON object. No markdown fences. No text before or after the JSON."
        )
        try:
            message = client.messages.create(
                model=config.drafting.model,
                max_tokens=config.drafting.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt + extra}],
            )
            raw_content = message.content[0].text
            parsed = json.loads(_strip_fences(raw_content))

            email_data = parsed.get("email", {})
            return DraftResult(
                candidate=candidate,
                research=research,
                fit_summary=parsed.get("fit_summary", ""),
                email=EmailDraft(
                    subject=email_data.get("subject", ""),
                    body=email_data.get("body", ""),
                ),
            )

        except json.JSONDecodeError as e:
            if attempt == 0:
                logger.warning(
                    f"JSON parse error for {candidate.company.name} (attempt 1) — retrying with stricter prompt."
                )
                continue
            logger.error(f"JSON parse failed twice for {candidate.company.name}: {e}")
            logger.debug(f"Raw response excerpt: {raw_content[:300]}")
            return DraftResult(
                candidate=candidate,
                research=research,
                draft_failed=True,
                draft_error=f"JSON parse error after 2 attempts: {e}",
            )

        except anthropic.APIError as e:
            logger.error(f"Anthropic API error for {candidate.company.name}: {e}")
            return DraftResult(
                candidate=candidate,
                research=research,
                draft_failed=True,
                draft_error=f"Anthropic API error: {e}",
            )

        except Exception as e:
            logger.error(f"Unexpected drafting error for {candidate.company.name}: {e}")
            return DraftResult(
                candidate=candidate,
                research=research,
                draft_failed=True,
                draft_error=str(e),
            )

    return DraftResult(
        candidate=candidate,
        research=research,
        draft_failed=True,
        draft_error="Drafting failed unexpectedly.",
    )
