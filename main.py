#!/usr/bin/env python3
"""
Seng Seng Plastic — Cold Outreach Researcher
Usage:
  python main.py                   # normal run
  python main.py --dry-run         # full pipeline, no history update, file marked DRY_RUN
  python main.py --count 3         # override companies_per_run for this run
  python main.py --dry-run --count 2
"""
import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import load_config
from src.dedupe import filter_candidates, write_history
from src.drafting import draft_email
from src.logging_setup import setup_logging
from src.models import DraftResult
from src.output import write_spreadsheet
from src.research import research_company
from src.sourcing import search_candidates, ApolloError

console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seng Seng Plastic cold outreach researcher — finds, researches, and drafts emails for medical supply companies."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but write to a DRY_RUN_ file and do NOT update history.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Override companies_per_run from config.yaml for this run only.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:8]

    # Set up logging (console via rich + file)
    Path("output").mkdir(exist_ok=True)
    log_file = f"output/run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log"
    logger = setup_logging(log_file)

    console.print(
        Panel.fit(
            "[bold blue]Seng Seng Plastic — Cold Outreach Researcher[/bold blue]\n"
            f"Run ID: [dim]{run_id}[/dim]"
            + ("\n[yellow]  DRY RUN — history will NOT be updated[/yellow]" if args.dry_run else ""),
            border_style="blue",
        )
    )

    # ── Load config ───────────────────────────────────────────────────────────
    config = load_config()
    if args.count:
        config.run.companies_per_run = args.count
        logger.info(f"companies_per_run overridden to {args.count} via --count flag.")

    target = config.run.companies_per_run

    # ── Stage 1: Source ───────────────────────────────────────────────────────
    console.print(f"\n[bold]Stage 1/5 — Sourcing up to {target * 2} candidates from Apollo...[/bold]")
    try:
        raw_candidates, credit_calls = search_candidates(config, target)
    except ApolloError as e:
        console.print(f"\n[red]Apollo error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Unexpected error during Apollo search:[/red] {e}")
        logger.exception("Apollo search crashed.")
        sys.exit(1)

    console.print(
        f"  Sourced [green]{len(raw_candidates)}[/green] candidates "
        f"({credit_calls} email reveal credit call(s) made)"
    )

    # ── Stage 2: Dedupe + Blocklist ───────────────────────────────────────────
    console.print("\n[bold]Stage 2/5 — Deduplicating and checking blocklist...[/bold]")
    new_candidates, already_seen, blocklisted = filter_candidates(raw_candidates, config)

    console.print(
        f"  New: [green]{len(new_candidates)}[/green]  |  "
        f"Already seen: {len(already_seen)}  |  "
        f"Blocklisted: [yellow]{len(blocklisted)}[/yellow]"
    )
    for candidate, matched_entry in blocklisted:
        console.print(f"    [yellow]BLOCKED:[/yellow] {candidate.company.name!r} → matched '{matched_entry}'")

    # Trim to target
    new_candidates = new_candidates[:target]

    if not new_candidates:
        console.print(
            "\n[yellow]No new candidates after deduplication.[/yellow]\n"
            "Try expanding the ICP filters in config.yaml or run again tomorrow."
        )
        sys.exit(0)

    # ── Stage 3: Research ─────────────────────────────────────────────────────
    console.print(f"\n[bold]Stage 3/5 — Researching {len(new_candidates)} company/companies...[/bold]")
    researched = []
    website_failures = 0

    for i, candidate in enumerate(new_candidates, 1):
        console.print(f"  [{i}/{len(new_candidates)}] {candidate.company.name}")
        r = research_company(candidate, config)
        researched.append(r)
        if not r.website_available:
            website_failures += 1

    console.print(f"  Done — {website_failures} website(s) unavailable (those rows use Apollo data only).")

    # ── Stage 4: Draft ────────────────────────────────────────────────────────
    console.print(f"\n[bold]Stage 4/5 — Drafting emails with Claude ({config.drafting.model})...[/bold]")
    anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    results: List[DraftResult] = []
    draft_failures = 0

    for i, research in enumerate(researched, 1):
        console.print(f"  [{i}/{len(researched)}] {research.candidate.company.name}")
        result = draft_email(research, config, anthropic_client)
        results.append(result)
        if result.draft_failed:
            draft_failures += 1
            console.print(f"    [red]  Draft failed:[/red] {result.draft_error}")

    console.print(f"  Done — {draft_failures} draft(s) failed (marked in spreadsheet).")

    # ── Stage 5: Output ───────────────────────────────────────────────────────
    console.print("\n[bold]Stage 5/5 — Writing spreadsheet...[/bold]")
    output_path = write_spreadsheet(results, config, dry_run=args.dry_run)

    # ── Update history (skip in dry run) ──────────────────────────────────────
    if not args.dry_run:
        successful = [r.candidate for r in results if not r.draft_failed]
        write_history(successful, run_id, config)
        console.print(f"  History updated with {len(successful)} record(s).")
    else:
        console.print("  [yellow]DRY RUN — history was NOT updated.[/yellow]")

    # ── Run summary ───────────────────────────────────────────────────────────
    console.print()
    table = Table(title="Run Summary", border_style="blue", show_header=True)
    table.add_column("Metric", style="bold", min_width=30)
    table.add_column("Value", min_width=20)

    table.add_row("Candidates sourced from Apollo", str(len(raw_candidates)))
    table.add_row("Duplicates / seen before", str(len(already_seen)))
    table.add_row("Blocklist drops", str(len(blocklisted)))
    if blocklisted:
        for cand, entry in blocklisted:
            table.add_row(f"  └ {cand.company.name}", f"matched '{entry}'")
    table.add_row("Companies researched", str(len(new_candidates)))
    table.add_row("Website failures", str(website_failures))
    table.add_row("Draft failures", str(draft_failures))
    table.add_row("Apollo credit calls made", str(credit_calls))
    table.add_row("Output file", output_path)
    table.add_row("Log file", log_file)

    console.print(table)

    if args.dry_run:
        console.print(
            Panel(
                "[yellow]DRY RUN complete.[/yellow] The spreadsheet above is for review only — "
                "history was not updated, so these companies can still appear in a real run.",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                f"[green]Run complete![/green]\n\nOpen [bold]{output_path}[/bold] in Excel or Numbers.\n"
                "For each row: review the email, then type [bold]Approve[/bold] or [bold]Skip[/bold] "
                "in the Decision column.\nApproved rows are ready for you to send manually.",
                border_style="green",
            )
        )


if __name__ == "__main__":
    main()
