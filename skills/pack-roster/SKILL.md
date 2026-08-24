---
name: pack-roster
description: Fetch and report the current Cub Scout pack roster from Scoutbook Plus (advancements.scouting.org), including who has paid their annual dues. Use this whenever the user asks about the pack roster, who is in the pack or in a specific den, scout ranks, BSA member IDs, parent/guardian contact info or emails, roster counts, dues payment status, or wants the roster exported to CSV or synced to a Google Sheet — even if they don't say "roster" (e.g. "email addresses for Wolf den parents", "how many Tigers do we have?", "list our scouts by den", "who still owes dues?", "did the Smiths pay?", "update the roster spreadsheet").
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
        "date_of_birth": "2016-04-17",
        "registration_expire": "2026-12-31", "registration_status": "Re-Registered",
        "is_expired": false,
        "parents": [{"name": "…", "relationship": "Parent/Guardian",
                     "email": "…", "phone": "…"}]}
     ]
   }
   ```

   `date_of_birth`/`registration_expire`/`registration_status`/`is_expired`
   come from the `orgYouths` endpoint (joined by `bsa_id`), a supplementary
   source — not the same endpoints the rest of the roster comes from. They
   default to empty/`false` if that endpoint wasn't captured this run. A
   given `memberId` can appear more than once there (old registration year +
   re-registration); the live one is picked by preferring
   `isManuallyEnded == false`, then the later expiration date among ties —
   getting this wrong silently resurfaces already-renewed scouts as
   "expired" (see `extract_registration_info()` in the script).

3. Answer the user's actual question from the data. When they asked for the
   roster itself, default to a markdown table grouped by den:

   | Scout | Rank | BSA ID | Parent/Guardian |
   |-------|------|--------|-----------------|

   with one `### <Den>` section per den, parents as `Name <email>`, and a
   closing line noting when the data was fetched (`fetched_at`) and that
   `~/.scouting-skills/roster.csv` holds the CSV export. For narrower
   questions (one den, a count, missing emails), answer directly — don't
   dump the full table.

4. Roster data is family PII, and so is the dues data (payer names, emails,
   payment amounts). Keep both in `~/.scouting-skills/`; don't copy them into
   git repos, artifacts, or anywhere outside the conversation unless the user
   explicitly asks.

5. **Dues payments.** Who has paid the pack's annual dues comes from a
   separate Google Sheet — the export of the online payment form — not from
   Scoutbook. Run (from this skill's directory):

   ```bash
   uv run scripts/fetch_dues.py
   ```

   It reads every configured sheet, joins them to the cached roster, caches
   `~/.scouting-skills/dues.json`, and prints per-sheet counts, paid/unpaid
   totals, and the unpaid scouts by den — enough to answer "who still owes?"
   directly. Read `dues.json` for the details behind a single family (amount,
   payment date, payer, PayPal status, and which sheet it came from). It is
   read-only; nothing is written to the payment sheets.

   Things worth knowing before reporting dues numbers:

   - **The sheets are per-year, and a year needs more than one of them.**
     `DUES_SHEET_ID` in `~/.scouting-skills/google-sheets.env` is a
     comma-separated list, each entry optionally suffixed with
     `@YYYY-MM-DD` meaning "only submissions on or after this date". It
     currently reads only the current program year's sheet — the prior
     year's sheet was dropped once its early-payer tail was no longer
     needed. **The two-sheet setup recurs every year, though:** the new
     form goes up in mid-August, but families who pay early go through the
     old form, so the first payments of a year sit at the bottom of the
     previous year's sheet. Without a cutoff those rows would be missed;
     without a cutoff *at all* the whole previous year would be imported and
     nearly everyone would read as paid. When a new year's early payments
     start landing in the previous year's sheet, add it back as a second
     entry with a cutoff of roughly the June before it, then drop it again
     once that tail is no longer relevant. `--sheet <ref>` (repeatable)
     overrides the list for one run.
   - **One row is one family, covering up to N scouts**, with "Scout 1/2/3…"
     column groups. Columns are located by header text, not position, so a
     rebuilt form with different field ids still parses — but if the labels
     stop matching `Scout <n> First Name`, the script exits 4 and prints the
     headers it saw.
   - **Paid means the PayPal cell says `COMPLETED`.** A blank cell is unpaid;
     free text (someone typing "cash" or "check #412") counts as paid, since
     the pack does take payment outside the form.
   - **Names are matched tolerantly but never guessed.** Parents type "Will"
     for William and "Gabriella" for Gabriela, so exact matches are claimed
     first, then a same-last-name scout can be claimed by a near-miss when one
     candidate is clearly closest. Ambiguous names — two roster siblings fit
     equally well — are reported for a human to resolve, never assigned.
   - **Paid but not in Scoutbook is normal, and those scouts still count.**
     Families pay through the form before the pack registers them, sometimes
     under a surname that differs from the payer's. `fetch_dues.py` lists them
     separately, and the sheet sync adds them as roster rows (see below) — so
     when reporting the roster or answering "how many scouts do we have", say
     they're paid but not yet registered rather than dropping them or treating
     them as fully registered. Worth telling the user each time: it usually
     means a registration still needs to be filed.
   - An unpaid scout means "no matching payment in this sheet" — treat it as a
     prompt to check, not proof the family hasn't paid.

6. **Optional: sync to Google Sheets.** If the user wants the roster pushed
   to (or updated in) a Google Sheet, run (from this skill's directory):

   ```bash
   uv run scripts/sync_roster_to_sheet.py
   ```

   This overwrites one tab with a fresh, formatted snapshot (title row,
   bold header, frozen top rows) built from the cached `roster.json` — run
   the fetch step first if it's stale. It reads the dues sheet at the same
   time and fills in a **Dues Paid** column (`Yes`/`No`, after `Rank`); with
   no dues sheet configured, or with `--no-dues`, that column is left out of
   the sheet entirely rather than written as a column of misleading `No`s.
   `--dues-sheet <ref>` (repeatable) points at different payment sheets for
   one run.

   Paid scouts who aren't in Scoutbook are **added to the sheet as extra
   rows**, sorted in by program level, with `Dues Paid` = `Yes` and
   `Registration Status` = `Not in Scoutbook (dues paid <date>)`. Name, den
   and the payer's name/email/phone come from the form; BSA ID, birthday and
   registration dates are deliberately left blank — filling them in would make
   an unregistered scout look registered, and blank is the signal that
   somebody still has to file the registration. These rows are rebuilt from
   the payment sheets on every run, so they persist across syncs without
   anyone hand-editing the sheet — and they disappear on their own once the
   scout shows up in Scoutbook and the name matches. Ambiguous payments are
   logged to stderr instead; pass those to the user. It defaults to the pack's roster
   spreadsheet (`ROSTER_SHEET_ID` in `~/.scouting-skills/google-sheets.env`);
   pass `--sheet <url-or-id>` to target a different spreadsheet for one run,
   or edit that env file to change the default going forward (the user has
   said this may change). `--tab <name>` targets a specific tab if the
   default (the URL's `gid`, or the first tab) isn't the right one.

   This writes real data into a document other people may have open —
   mention what's about to happen before running it the first time in a
   session, same as any other real-world-effect action. Use `--dry-run` to
   preview the rows without writing if there's any doubt about what will
   change.

7. **Optional: per-den summary report with charts.** If the user wants a
   report tab showing scout counts per den, broken down by registration
   expired/not-expired and dues paid/not-paid — plus bar charts of the same —
   run (from this skill's directory):

   ```bash
   uv run scripts/den_report.py
   ```

   This writes (creating it if needed) a **Den Report** tab in the roster
   spreadsheet: one row per den with scout count, registration
   expired/not-expired counts, and dues paid/not-paid counts, plus a Total
   row and two stacked column charts. Unlike a normal sync, the numbers
   themselves are **live spreadsheet formulas** (`COUNTIFS`) reading whichever
   other tab has the roster (normally the one `sync_roster_to_sheet.py`
   writes) — they recalculate on their own as that tab changes, no rerun
   needed. Only the *set of den rows* is fixed at write time (from that tab's
   Den column, at run time — not from `roster.json`); rerun the script when a
   den's been added, removed, or renamed, or a paid-but-unregistered scout's
   program level changes. This script reads only the spreadsheet — it never
   touches `roster.json` or `dues.json`, so it doesn't need `fetch_roster.py`
   or `fetch_dues.py` run first, only `sync_roster_to_sheet.py` at some point
   before it (for the roster tab and its Dues Paid column to exist).
   Paid-but-not-in-Scoutbook scouts appear as their own den row (e.g. "Tiger"
   with no number) rather than a footnote, since they're real rows on the
   roster tab with a program-level-only Den value — the same thing that makes
   them show up twice in the roster tab (see Troubleshooting) shows up here
   as an extra small row next to the numbered den.

   `--sheet`/`--dry-run` work like `sync_roster_to_sheet.py`; `--tab` names
   the Den Report tab itself (default `Den Report`) — the roster tab doesn't
   need naming, it's found automatically as the other tab. `--no-dues` drops
   the dues columns from the report even if the roster tab has a Dues Paid
   column. Rerunning replaces the tab's den rows and charts, so it's safe to
   run repeatedly; day-to-day count changes don't need a rerun at all.

## Troubleshooting

- **Exit code 2** — Chromium missing (install per above) or login never
  completed within the timeout; rerun and ask the user to finish logging in.
- **Exit code 3** — pages loaded but no roster-shaped JSON was recognized.
  The raw API responses are in `~/.scouting-skills/raw/*.json`; read them,
  find the payload that contains the member list, and update
  `extract_roster()` / `normalize_person()` in the script to match. This is
  the expected failure mode if Scouting America changes their API.
- **Stale-looking data** — check `fetched_at`; rerun with `--force`.
- **`registration_expire` blank for everyone** — the `orgYouths` endpoint
  wasn't among this run's captures (it loads incidentally when the SPA
  fetches org-level data, not on every navigation path); rerun and, if
  still blank, check `raw/*.json` for a URL matching
  `/organizations/v2/{guid}/orgYouths` to confirm whether it fired at all.
- **Parents missing** — the roster API may not include guardian contacts;
  report the scouts and tell the user parent info wasn't in the payload
  rather than guessing. (The API also doesn't say mother/father — every
  guardian is reported as "Parent/Guardian".)
- **Empty den or rank** — normal for newly joined scouts who haven't been
  assigned a den or earned a rank yet; report them as unassigned, don't
  drop them.
- **`sync_roster_to_sheet.py` exits 2, "No OAuth client"** — one-time setup
  needed: in Google Cloud Console, create (or reuse) a project, enable the
  **Google Sheets API**, add an **OAuth client ID** of type **Desktop app**
  (a dedicated one for scouting-skills, not shared with other projects —
  that's what the user asked for), and save the downloaded JSON to
  `~/.scouting-skills/google-oauth-client.json` (chmod 600). The next run
  opens a browser for a one-time consent as the user; after that a cached
  token (`~/.scouting-skills/google-sheets-token.json`) refreshes silently.
- **`sync_roster_to_sheet.py` exits 4** — a Google API error, printed to
  stderr; the most common cause is the signed-in Google account not having
  edit access to the target spreadsheet (auth is "as the user," so unlike a
  service account there's no separate sharing step — just make sure
  whoever consents in the browser is someone with edit access).
- **Wrong tab got overwritten** — the script defaults to the URL's `gid` if
  the `--sheet` value was a full URL, else the spreadsheet's first tab;
  pass `--tab <name>` to be explicit.
- **`fetch_dues.py` exits 2** — either no `DUES_SHEET_ID` (the pack moved to a
  new year's sheet and nobody updated `google-sheets.env`) or the same missing
  OAuth client as the sync script; the setup is identical and the token is
  shared.
- **`fetch_dues.py` exits 4** — usually the Google account that consented has
  no access to the payment sheet (it's a different document from the roster
  sheet and may be shared with a different set of people), or the form's
  headers no longer contain `Scout <n> First Name`; the script prints the
  headers it saw, so adjust `map_columns()` to the new labels.
- **Everyone shows unpaid** — check the dues sheets actually being read
  (printed in the summary by title, with how many submissions each
  contributed). A new year's sheet is nearly empty for its first weeks while
  the early payments still sit in the previous year's sheet; if the second
  entry and its `@date` cutoff are missing from `DUES_SHEET_ID`, that looks
  exactly like a pack that hasn't paid.
- **A sheet contributes 0 submissions** — its cutoff is later than every row
  in it, or the dates aren't in a recognized format. `fetch_dues.py` logs how
  many rows it skipped as undated; rows it can't date are skipped rather than
  assumed current whenever a cutoff is in force.
- **A family insists they paid but shows unpaid** — look for their row in the
  unmatched list first (nickname or misspelling), then check whether their
  PayPal cell says something other than `COMPLETED` (an abandoned checkout
  leaves a `PENDING`/failed row behind).
- **A scout appears twice in the sheet** — once from Scoutbook and once as a
  "Not in Scoutbook" row. That means the two spellings didn't match closely
  enough to join (different surname, say Mom's on the form and Dad's in
  Scoutbook). Fixing the spelling in either system merges them on the next
  run; don't hand-delete the row, it'll come back.
- **`den_report.py` exits 3** — either the spreadsheet has no tab besides the
  Den Report tab itself to read a roster from, or that tab's header row (row
  3) is missing "First Name", "Den", or "Registration Expires" — run
  `sync_roster_to_sheet.py` first, or pass `--tab` if the Den Report tab has
  an unusual name and got picked as the roster tab by mistake.
- **`den_report.py`'s counts look wrong or stale** — they're live formulas
  reading the roster tab directly, so check that tab's data, not
  `roster.json`/`dues.json` (this script doesn't read either). If a den row
  is missing or extra, rerun `den_report.py` to refresh the row set.
