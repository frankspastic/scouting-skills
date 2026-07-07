---
name: pack-roster
description: Fetch and report the current Cub Scout pack roster from Scoutbook Plus (advancements.scouting.org). Use this whenever the user asks about the pack roster, who is in the pack or in a specific den, scout ranks, BSA member IDs, parent/guardian contact info or emails, roster counts, or wants the roster exported to CSV — even if they don't say "roster" (e.g. "email addresses for Wolf den parents", "how many Tigers do we have?", "list our scouts by den").
---

# Pack Roster

Retrieves the live roster of the user's Cub Scout pack from Scoutbook Plus
and answers roster questions from it.

## How it works

`scripts/fetch_roster.py` opens a real browser, lets the Scoutbook Plus SPA
make its own API calls, and captures the roster JSON off the wire (the DOM
is not scraped — its class names are hashed and unstable). A persistent
browser profile in `~/.scouting-skills/browser-profile/` keeps the login
session, so most runs need no interaction; when the session has expired the
user must log in manually in the opened window (Scouting America uses a
CAPTCHA, so login cannot be automated — never try to type credentials for
the user, and never ask the user to paste their password into the chat).

Results are cached in `~/.scouting-skills/roster.json` for 24 hours by
default, so repeat questions don't reopen the browser.

## Workflow

1. Run the fetch (from this skill's directory):

   ```bash
   uv run scripts/fetch_roster.py --max-age-hours 24
   ```

   - Tell the user first: "a browser window may open — if you see a login
     page, log in and the script will pick things up automatically."
   - Run it in the background if possible; it can take a few minutes when a
     manual login is needed (`--login-timeout 300` is the default wait).
   - The user wants guaranteed-fresh data ("as of right now")? Add `--force`.
   - Playwright's Chromium missing? The script says so; install it with
     `uv run --with playwright playwright install chromium` and rerun.

2. Read `~/.scouting-skills/roster.json`. Shape:

   ```json
   {
     "fetched_at": "2026-07-07T12:00:00+00:00",
     "scout_count": 42,
     "scouts": [
       {"first_name": "…", "last_name": "…", "bsa_id": "…",
        "den": "…", "rank": "…",
        "parents": [{"name": "…", "relationship": "Parent/Guardian",
                     "email": "…", "phone": "…"}]}
     ]
   }
   ```

3. Answer the user's actual question from the data. When they asked for the
   roster itself, default to a markdown table grouped by den:

   | Scout | Rank | BSA ID | Parent/Guardian |
   |-------|------|--------|-----------------|

   with one `### <Den>` section per den, parents as `Name <email>`, and a
   closing line noting when the data was fetched (`fetched_at`) and that
   `~/.scouting-skills/roster.csv` holds the CSV export. For narrower
   questions (one den, a count, missing emails), answer directly — don't
   dump the full table.

4. Roster data is family PII. Keep it in `~/.scouting-skills/`; don't copy
   it into git repos, artifacts, or anywhere outside the conversation
   unless the user explicitly asks.

## Troubleshooting

- **Exit code 2** — Chromium missing (install per above) or login never
  completed within the timeout; rerun and ask the user to finish logging in.
- **Exit code 3** — pages loaded but no roster-shaped JSON was recognized.
  The raw API responses are in `~/.scouting-skills/raw/*.json`; read them,
  find the payload that contains the member list, and update
  `extract_roster()` / `normalize_person()` in the script to match. This is
  the expected failure mode if Scouting America changes their API.
- **Stale-looking data** — check `fetched_at`; rerun with `--force`.
- **Parents missing** — the roster API may not include guardian contacts;
  report the scouts and tell the user parent info wasn't in the payload
  rather than guessing. (The API also doesn't say mother/father — every
  guardian is reported as "Parent/Guardian".)
- **Empty den or rank** — normal for newly joined scouts who haven't been
  assigned a den or earned a rank yet; report them as unassigned, don't
  drop them.
