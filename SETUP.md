# Setup

Everything a new user needs to configure before these skills work for
*their* pack. Each item below is also documented reactively in its skill's
own `SKILL.md` (usually under Troubleshooting) — this page just collects it
all in one place for first-time setup.

None of this configuration lives in the repo. It all goes in
`~/.scouting-skills/` (override the location for every skill at once with
`SCOUTING_SKILLS_DATA_DIR`), which also becomes the cache for live roster,
dues, and advancement data — real family PII. Nothing under that directory
should ever be committed to git.

## 0. Requirements

- [`uv`](https://docs.astral.sh/uv/) — every script is a `uv run
  scripts/....py`, using PEP 723 inline dependencies (no separate install
  step).
- Playwright's Chromium, for the three skills that log into Scoutbook Plus
  (pack-roster, pack-advancement, ypt-aging-report):
  `uv run --with playwright playwright install chromium`

## 1. pack-calendar

No credentials needed — it reads a public ICS feed. But the feed URL is
your pack's own calendar, hardcoded in `skills/pack-calendar/scripts/fetch_events.py`
(the `holyfamilypack97%40gmail.com` ICS link). Replace it with your own
pack's public ICS feed URL — most pack calendar tools (Google Calendar,
website calendar plugins) have a "subscribe via ICS" or "public URL" option.

## 2. pack-roster, ypt-aging-report, pack-advancement

These log into Scoutbook Plus (`advancements.scouting.org`) through a real
browser window — Scouting America's CAPTCHA rules out scripted login, so
there's no credential to configure here. Run any one of them; a browser
opens on first use (or whenever the session has expired) and you log in as
a registered leader with access to your pack's roster and reports. That
session is cached in `~/.scouting-skills/browser-profile/` and shared
across all three skills, so logging in once covers all of them.

## 3. pack-roster's optional Google Sheets sync

Only needed for `sync_roster_to_sheet.py`, `fetch_dues.py`, and
`den_report.py` — the roster fetch itself doesn't need this.

1. In Google Cloud Console, create (or reuse) a project, enable the
   **Google Sheets API**, and add an **OAuth client ID** of type
   **Desktop app** (a dedicated one for this plugin, not shared with other
   projects, is recommended).
2. Save the downloaded client JSON to
   `~/.scouting-skills/google-oauth-client.json` and `chmod 600` it.
3. Create `~/.scouting-skills/google-sheets.env`:

   ```
   ROSTER_SHEET_ID=<your roster spreadsheet id or full URL>
   DUES_SHEET_ID=<your dues-payment-form export sheet id or full URL>
   ```

   `DUES_SHEET_ID` accepts a comma-separated list, each entry optionally
   suffixed `@YYYY-MM-DD` — see `skills/pack-roster/SKILL.md` for why (the
   payment form is rebuilt every program year, so early payers can land in
   last year's sheet).
4. The first Sheets script you run opens a browser for one-time consent —
   sign in as the Google account that should own this access (it needs
   edit access to both spreadsheets; there's no separate sharing step
   since auth is "as the user," not a service account). The resulting
   token is cached at `~/.scouting-skills/google-sheets-token.json` and
   refreshes silently after that.

## 4. mailchimp-audience

Create `~/.scouting-skills/mailchimp.env`:

```
MAILCHIMP_API_KEY=<your API key>
MAILCHIMP_SERVER_PREFIX=<e.g. us21 — the subdomain in your API key / account URL>
MAILCHIMP_AUDIENCE_ID=<the audience (list) ID to read/write>
```

Find these in Mailchimp under **Account → Extras → API keys** (key; the
server prefix is the suffix after the last `-` in the key) and
**Audience → Settings → Audience name and defaults** (audience ID).

## 5. family-advancement-emails

No separate configuration. It reads the cached `roster.json` /
`advancement.json` from the skills above and sends through whatever
Gmail account the connected Gmail app/MCP tool is signed into.

## 6. Pack-specific reference content

`skills/reference/pack97-info.md` is Pack 97's own information (leadership
roster, dues amounts, mailing address, meeting details) — replace it with
your own pack's equivalent, or delete it and update any skill that points
to it. `skills/reference/cub-scout-adventures.md` is generic BSA program
content (official adventure names per rank) and needs no changes.

## Checklist

- [ ] `uv` installed
- [ ] Chromium installed for Playwright
- [ ] Logged into Scoutbook Plus once (opens automatically on first
      pack-roster/pack-advancement/ypt-aging-report run)
- [ ] `pack-calendar`'s ICS feed URL updated to your pack's calendar
- [ ] `skills/reference/pack97-info.md` replaced or removed
- [ ] *(optional)* Google Sheets: OAuth client + `google-sheets.env`
- [ ] *(optional)* Mailchimp: `mailchimp.env`
