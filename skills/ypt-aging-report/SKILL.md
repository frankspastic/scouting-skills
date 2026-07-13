---
name: ypt-aging-report
description: Fetch and report the Safeguarding Youth Training (YPT) Aging Report from Scoutbook Plus (advancements.scouting.org/reports). Use this whenever the user asks about Youth Protection Training or Safeguarding Youth Training status — whose YPT is expired or expiring, when a leader's training expires, whether adults/leaders are current on training, or who needs to retake YPT — even if they don't say "report" (e.g. "is anyone's YPT about to lapse?", "which leaders are out of compliance?", "training expiration dates for our adults").
---

# Safeguarding Youth Training (YPT) Aging Report

Retrieves the Safeguarding Youth Training Aging Report — each registered
adult's Youth Protection Training completion/expiration status — from
Scoutbook Plus and answers training-compliance questions from it.

## How it works

`scripts/fetch_ypt_aging.py` opens a real browser at the Reports page and
captures the report data off the wire (the DOM is not scraped — its class
names are hashed and unstable). The primary source is the
`/organizations/v2/{guid}/orgAdults` API call the Reports page makes
itself, whose rows carry each adult's `yptCompletedDate`, `yptExpiredDate`,
and `yptStatus`; if that ever disappears, the script falls back to opening
the "Safeguarding Youth Training Aging Report" (via its URI from the
Reports-menu API, or by clicking its link text) and running a generic
JSON/CSV/HTML-table parser over what it returns. It shares the
persistent browser profile in `~/.scouting-skills/browser-profile/` with the
pack-roster skill, so a login from either skill covers both; when the
session has expired the user must log in manually in the opened window
(Scouting America uses a CAPTCHA, so login cannot be automated — never try
to type credentials for the user, and never ask the user to paste their
password into the chat).

Results are cached in `~/.scouting-skills/ypt_aging.json` for 24 hours by
default, so repeat questions don't reopen the browser.

## Workflow

1. Run the fetch (from this skill's directory):

   ```bash
   uv run scripts/fetch_ypt_aging.py --max-age-hours 24
   ```

   - Tell the user first: "a browser window may open — if you see a login
     page, log in; the script will then open the report itself, but if it
     doesn't, click 'Safeguarding Youth Training Aging Report' on the
     Reports page."
   - Run it in the background if possible; it can take a few minutes when a
     manual login is needed (`--login-timeout 300` is the default wait).
   - The user wants guaranteed-fresh data ("as of right now")? Add `--force`.
   - Playwright's Chromium missing? The script says so; install it with
     `uv run --with playwright playwright install chromium` and rerun.

2. Read `~/.scouting-skills/ypt_aging.json`. Shape:

   ```json
   {
     "fetched_at": "2026-07-07T12:00:00+00:00",
     "adult_count": 15,
     "expiring_soon_days": 60,
     "adults": [
       {"first_name": "…", "last_name": "…", "bsa_id": "…",
        "position": "Cubmaster; Committee Member", "course": "…",
        "completed": "2025-08-01", "expires": "2027-08-01",
        "days_until_expiration": 390,
        "status": "current | expiring_soon | expired | unknown",
        "api_status": "ACTIVE | Expired | Expires 31-60d | …"}
     ]
   }
   ```

   Adults are deduped by BSA ID (multi-position adults have their positions
   merged) and pre-sorted soonest-expiring first. `status` is computed from
   `expires` at fetch time (`expiring_soon` = within 60 days); if the cache
   is old, recompute from the dates rather than trusting `status`.
   `api_status` is Scouting America's own bucket, present only when the
   orgAdults endpoint was the source.

3. Answer the user's actual question from the data. When they asked for the
   report itself, default to a markdown table sorted soonest-expiring first:

   | Adult | Position | YPT Expires | Days Left | Status |
   |-------|----------|-------------|-----------|--------|

   Lead with the problems — call out **expired** and **expiring soon**
   adults above the table (or say "everyone is current" if so) — and close
   with when the data was fetched (`fetched_at`) and that
   `~/.scouting-skills/ypt_aging.csv` holds the CSV export. For narrower
   questions (one person, a count), answer directly — don't dump the full
   table.

4. Training records are adult-member PII. Keep them in
   `~/.scouting-skills/`; don't copy them into git repos, artifacts, or
   anywhere outside the conversation unless the user explicitly asks.

## Troubleshooting

- **Exit code 2** — Chromium missing (install per above) or login never
  completed within the timeout; rerun and ask the user to finish logging in.
- **Exit code 3** — pages loaded but no training-report-shaped data was
  recognized. The raw responses are in `~/.scouting-skills/raw-ypt/`; read
  them, find the payload with the training rows, and update
  `extract_org_adults()` / `normalize_row()` in the script to match, then rerun
  with `--from-raw` to iterate without reopening the browser. This is the
  expected failure mode if Scouting America changes their API. If the site
  renamed the report link, also update `REPORT_LINK_PATTERNS`.
- **Script never finds the report but the user sees it on screen** — the
  report may render without a captured JSON/CSV response (e.g. a PDF).
  Check `raw-ypt/` for what was captured; if the data arrives as PDF only,
  tell the user and ask them to use the report's CSV/export option in the
  opened window.
- **Stale-looking data** — check `fetched_at`; rerun with `--force`.
- **`status: unknown`** — the row had no parseable expiration date; report
  the adult with whatever dates exist rather than guessing, and consider
  adding the date format to `parse_date()`.
