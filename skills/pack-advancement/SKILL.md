---
name: pack-advancement
description: Fetch and report the Full Pack Advancement Report from Scoutbook Plus, showing each den's rank and adventure completion progress. Use this whenever the user asks about advancement progress, which adventures a scout or den has completed, rank progress, who's finished a specific adventure or requirement, or wants an advancement chart by den — even if they don't say "advancement" (e.g. "how's Wolf den doing on adventures?", "has Ava finished Council Fire?", "who still needs Bobcat?", "print an advancement chart for the pack").
---

# Full Pack Advancement Report

Retrieves the user's saved "Full Pack Advancement Report" (a Scoutbook
Report Builder custom report) from Scoutbook Plus, joins it with the pack
roster to know each scout's den, and reports rank/adventure completion
per den.

## Per-den progress (preferred for one den)

For one den's progress toward its rank ("how's Webelos den doing?", "who
still needs My Family?"), skip the custom report and use the Scoutbook Plus
youth profiles directly. These carry real adventure names, partial progress
(percent complete), and approval dates, and you never need to open Custom
Reports:

```bash
uv run scripts/fetch_den_advancement.py --den "Webelos Den 7"   # --force for fresh
uv run scripts/den_progress.py --den "Webelos Den 7"            # prints markdown
```

- The den name must match `roster.json`'s `den` field exactly. Run
  `pack-roster`'s `fetch_roster.py` first. It saves the unit youth list in
  `~/.scouting-skills/raw/`, which maps member IDs to the Scoutbook user
  IDs the profile pages need.
- It opens `advancements.scouting.org/youthProfile/<userId>` for each scout
  and records the JSON the page loads (`youth/<id>/ranks`,
  `youth/<id>/adventures`, `youth/<id>/ranks/<rankId>/requirements`, and the
  adventure catalog). It never calls the API or handles tokens itself.
  About 5 seconds per scout.
- Output: `~/.scouting-skills/den-advancement/<den-slug>.json`, cached for
  24 hours.
- `den_progress.py` prints a summary (rank status, required n/6, electives
  n/2) and an adventure-by-scout table. Show its output as is; add
  observations (e.g. who's missing a requirement) below it. `--initials`
  shortens the column headers.
- `--status-words` makes the cells say Complete / Incomplete / Not Started.
  To put that grid in a Google Sheet tab, run
  `uv run scripts/den_progress_to_sheet.py --den "<den>" --sheet <id or URL>`.
  It uses pack-roster's saved Google Sheets login and creates the tab
  ("<Rank> Advancement" by default). On reruns, pass `--replace`. Without
  it, the script won't overwrite an existing tab. Webelos Den 7's grid lives
  in the "2026-2027 Webelos Plan" sheet, which is shared with families.
- Each scout's `adventures` list only includes adventures they've *started*.
  Rows for unstarted required adventures come from the rank's requirement
  list. Cells always come from `adventures`, because the rank-requirements
  response sometimes comes back empty for a scout and never shows partial
  progress.
- After visiting the profiles, the script goes back to `/roster` and checks
  that the pack roster still loads. If it logs a WARNING, Scoutbook Plus has
  switched this browser to a single scout's view (seen once after opening a
  leader's own child's profile). Ask the user to switch back to their pack
  leader role in that browser window before running `fetch_roster.py`.
  Otherwise the roster fetch only sees a handful of people.

## How it works (full pack custom report)

Custom reports (Custom Reports → My Reports on the Reports page) live in
the *legacy* Scoutbook report system (`reportspd.scouting.org`), separate
from the modern `api.scouting.org` endpoints the roster/YPT skills use, and
they render as a plain HTML page rather than JSON — no SPA scraping needed,
but a different login is required.

`scripts/fetch_advancement.py`:
1. Opens the Scoutbook dashboard once to establish the legacy Scoutbook SSO
   session (it piggybacks on the my.scouting.org login already in the shared
   browser profile — reportspd.scouting.org refuses report requests without
   it: "Single Sign On (SSO) is required to run reports").
2. Loads the Reports page and captures the `SBLSharedReportsManagerAPI`
   response, which lists every saved custom report with its title and a
   direct `run_uri` — so it can find "Full Pack Advancement Report" by name
   even if BSA changes its numeric report ID.
3. GETs that `run_uri` and parses the rendered HTML: one big table where
   columns are scouts and rows are rank names, per-rank-version requirement
   numbers, and adventure/award names, with completion dates as cells.

It shares the persistent browser profile in
`~/.scouting-skills/browser-profile/` with the other skills, so a login
from any of them covers all; when the session has expired the user must log
in manually (Scouting America uses a CAPTCHA, so login cannot be
automated — never try to type credentials for the user, and never ask the
user to paste their password into the chat).

Results are cached in `~/.scouting-skills/advancement.json` for 24 hours by
default.

## Workflow

1. Make sure both data sources are fresh — run them in parallel:

   ```bash
   uv run scripts/fetch_advancement.py --max-age-hours 24
   ```

   and (from the `pack-roster` skill's directory) its
   `fetch_roster.py --max-age-hours 24` to get den assignments — this
   skill needs both. Tell the user first: "a browser window may open — if
   you see a login page, log in and the fetch resumes on its own." Run
   fetches in the background when possible; a manual login can take a
   few minutes (`--login-timeout 300` is the default wait). Add `--force`
   if the user wants guaranteed-fresh data. If Playwright's Chromium is
   missing, the script says so — install with
   `uv run --with playwright playwright install chromium` and rerun.

2. Read `~/.scouting-skills/advancement.json`. Shape:

   ```json
   {
     "fetched_at": "2026-07-08T00:00:00+00:00",
     "report_title": "Full Pack Advancement Report",
     "scout_count": 67,
     "catalog": {
       "ranks": ["Lion", "Bobcat", "Tiger", "Wolf", "Bear", "Webelos", "Arrow of Light"],
       "adventures": {"Wolf": ["Bobcat (Wolf)", "Council Fire", "…"], "…": []},
       "awards": ["Whittling Chip", "…"]
     },
     "scouts": [
       {"name": "…", "first_name": "…", "last_name": "…", "bsa_id": "…",
        "current_rank": "Wolf", "next_rank": "",
        "ranks": {"Wolf": "2026-04-12"},
        "rank_requirements": {"Wolf v2024": {"completed": "2026-04-12",
                                             "reqs": {"1a": "2026-03-01", "…": "…"}}},
        "adventures": [{"name": "Council Fire", "rank": "Wolf", "completed": "2026-03-15"}],
        "awards": [{"name": "Whittling Chip", "completed": "2026-02-01"}]}
     ]
   }
   ```

   A date means completed (on that date); absence means not done. History
   is included — a current Webelos still shows their Lion/Tiger/Wolf/Bear
   completions. `rank_requirements` covers in-progress rank work (dates on
   individual requirements but no rank-completion date yet).

   `reference/required_adventures.json` lists each rank's *required*
   adventures (exact spelling matches `catalog.adventures[rank]`); anything
   in the catalog not listed there is an elective. Tiger's list is
   deliberately empty — its required content lives in the Tiger rank's own
   requirement rows (`rank_requirements`), not as separate adventure rows,
   per the 2024 Tiger Trail Cards curriculum.

3. Join to the roster: read `~/.scouting-skills/roster.json` (from the
   `pack-roster` skill) and match scouts by `bsa_id`. Group by the roster's
   `den` field (e.g. "Wolf Den 10"). Determine each den's rank from the den
   name (it names the rank directly, e.g. "Bear Den 7" → Bear, "Webelos Den
   6 (Male)" → Webelos); if a den's name doesn't clearly state a rank, fall
   back to the most common `current_rank` among its scouts, and flag any
   scout whose `current_rank` disagrees with the den's rank rather than
   silently reassigning them. Scouts with a blank den are "unassigned" —
   report them separately, don't drop them.

4. Render one table per den, in the style of `cub-scout-adventures.md`
   (headings + a compact table), but with adventures as rows and the den's
   actual scouts as columns instead of one column per rank:

   ```markdown
   ### Wolf Den 10

   Rank progress: **completed** — Ava M., Cole R. · **in progress** — Ben T.
   (6/8 reqs) · **not started** — Dana K.

   | Adventure | Ava M. | Ben T. | Cole R. | Dana K. |
   |---|---|---|---|---|
   | **Bobcat (Wolf)** | 3/1/26 | | 3/1/26 | |
   | **Council Fire** | 4/12/26 | 5/1/26 | | |
   | **Footsteps** | 4/12/26 | | 4/9/26 | |
   | … required rows … | | | | |
   | Digging in the Past | | 5/1/26 | | |
   | … elective rows … | | | | |
   ```

   - Bold the adventure name for rows found in
     `reference/required_adventures.json[rank]`; list those first, then
     electives — both in the report's own catalog order.
   - Cell = the completion date (`M/D/YY` is fine) if present, blank
     otherwise. Don't invent a ✓/✗ scheme beyond that.
   - Use full names as column headers (this is a leader's own roster data,
     not being published outside the conversation) unless the user asks
     for initials or fewer columns.
   - Sort scout columns alphabetically by last name.
   - Skip awards/Nova/religious-emblem rows by default — they're not
     adventures; answer questions about them directly from `scouts[].awards`
     instead of cluttering the den table.
   - For a narrow question ("has Ava finished Council Fire?", "who's done
     with Bobcat?"), answer directly — don't dump a full den table.

5. This report is scout PII. Keep it in `~/.scouting-skills/`; don't copy
   den tables, names, or dates into git repos, artifacts, or anywhere
   outside the conversation unless the user explicitly asks.

## Troubleshooting

- **Exit code 2** — Chromium missing (install per above) or login never
  completed within the timeout; rerun and ask the user to finish logging in.
- **Exit code 3** — the page loaded but no report table was recognized.
  Inspect `~/.scouting-skills/raw-advancement/report.html` — the most
  common cause is an "SSO required" error page (the Scoutbook-dashboard
  step didn't establish the session; rerun) or BSA renaming/restructuring
  the report. Fix `report_matrix()`/`parse_matrix()` in
  `scripts/fetch_advancement.py` to match, then rerun with `--from-raw` to
  iterate without reopening the browser.
- **Report title not found** — if the user renames or deletes "Full Pack
  Advancement Report" in Scoutbook, the script logs the titles it did find
  and falls back to the last known report ID. Pass
  `--report-title "New Name"` (or `--report-id N`) to point it at the
  right one, and update `DEFAULT_REPORT_TITLE` in the script once the user
  confirms the new name is permanent.
- **A den's rank looks wrong / adventures missing** — check
  `reference/required_adventures.json` still matches
  `catalog.adventures[rank]` exactly (BSA occasionally renames an
  adventure); update the spelling there if a required row isn't bolding.
- **Stale-looking data**: check `fetched_at` on both `advancement.json`
  and `roster.json`, and rerun either fetch with `--force`.
- **Custom report has only "Req #1a…" rows, no adventure names** (seen
  2026-10): the saved report's layout changed and `parse_matrix()`
  misfiles the data. Use the per-den scripts above instead.
