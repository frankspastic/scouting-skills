---
name: mailchimp-audience
description: Get the current state of the pack's Mailchimp audience (member counts, tags, a specific parent's status/merge fields) and make ad-hoc changes to it (add or update a member, tag/untag, unsubscribe). Use this whenever the user asks about the Mailchimp list/audience itself — "how many people are on our Mailchimp list", "is jane@example.com subscribed", "tag all Wolf den parents", "unsubscribe this person", "add this new family to Mailchimp" — as opposed to campaign/email design, which is handled by the connected Mailchimp app instead.
---

# Mailchimp Audience

Reads and maintains the pack's Mailchimp audience (the same list
`scout_mailchimp_sync.py` in `~/Sites/mailchimp-scout` bulk-syncs from
Scoutbook) via direct Mailchimp Marketing API calls — no browser or MCP
connector needed, since Mailchimp's API is a plain REST API with key auth.

## Division of labor

- **This skill**: audience *reporting* (counts, tags, a member's status) and
  **ad-hoc, single-member** maintenance (add/update one person, tag/untag,
  unsubscribe). Good for "what does the list look like right now" and
  one-off requests.
- **`scripts/sync_roster_to_mailchimp.py`** (this skill): the *bulk* roster
  push — one member per parent/guardian email, with each family's scouts in
  the `SCOUT{1,2,3}*` merge fields including `SCOUT{n}PAID` from the dues
  sheets. Use it for "resync everyone" / "push the latest roster to
  Mailchimp". It reads the pack-roster skill's caches (`roster.json`,
  `dues.json`), so run that skill's `fetch_roster.py` (and `fetch_dues.py`)
  first — it never opens a browser itself.
- **`~/Sites/mailchimp-scout/scout_mailchimp_sync.py`**: the *previous* bulk
  pipeline, superseded by the script above and **no longer runnable**. It
  logs into Scoutbook with a username and password, which Scouting America
  now gates behind a CAPTCHA; its `.env` names those credentials
  `MYSCOUTING_*` while the script requires `SCOUTBOOK_*`, so it exits at
  startup; and it writes merge fields named `Scout 1`/`ADDRESS`, which are
  not tags on this audience (the real tags are `SCOUT1NAME`, `SCOUT1DEN`, …
  — run `fields` to see them). Don't try to repair it in place unless the
  user asks; the roster half is what the pack-roster skill already does
  properly.
- **The connected Mailchimp app (`mcp__claude_ai_Intuit_Mailchimp__*`
  tools)**: campaign drafting and design — `campaign_planner`,
  `edit_campaign`, `apply_theme`, `edit_text`/`edit_image`,
  `get_email_themes`, `save_to_mailchimp`, `get_analytics`. Use those tools
  directly for "draft a campaign about the pack picnic" — don't build
  custom campaign-creation code; that connector already exists and is
  already wired to this account. Its own tool descriptions say contact/
  audience management is out of its scope, which is why this skill exists
  for that half.

## Credentials

`scripts/mailchimp_audience.py` reads `MAILCHIMP_API_KEY`,
`MAILCHIMP_SERVER_PREFIX`, and `MAILCHIMP_AUDIENCE_ID` from the environment,
falling back to `~/.scouting-skills/mailchimp.env` (plain `KEY=VALUE`
lines, chmod 600) if unset. That file was seeded from the same credentials
`scout_mailchimp_sync.py` uses, so both point at the same audience. If it's
ever missing, copy the three `MAILCHIMP_*` lines out of
`~/Sites/mailchimp-scout/.env`.

## Workflow

Run from this skill's directory:

```bash
uv run scripts/sync_roster_to_mailchimp.py            # dry run: show every change
uv run scripts/sync_roster_to_mailchimp.py --live     # apply it
uv run scripts/mailchimp_audience.py summary
uv run scripts/mailchimp_audience.py tags
uv run scripts/mailchimp_audience.py fields
uv run scripts/mailchimp_audience.py list --status subscribed --tag "Wolf Den"
uv run scripts/mailchimp_audience.py get --email jane@example.com
uv run scripts/mailchimp_audience.py search --query smith
uv run scripts/mailchimp_audience.py upsert --email jane@example.com \
    --merge-fields '{"FNAME":"Jane","LNAME":"Doe"}' --tags "Wolf Den,Parent"
uv run scripts/mailchimp_audience.py tag --email jane@example.com --add "Volunteer" --remove "Prospect"
uv run scripts/mailchimp_audience.py unsubscribe --email jane@example.com
```

Every subcommand prints JSON to stdout (log/progress lines go to stderr) —
read that JSON and answer the user's actual question directly (a count, a
member's status, a tag list) rather than dumping the raw JSON at them,
unless they asked for the full list.

**Real changes, confirm first.** `upsert`, `tag`, `unsubscribe`, and
`sync_roster_to_mailchimp.py --live` write to the live Mailchimp audience —
real parents' subscription status and data. Get an explicit go-ahead before
running them (a `get`, or the bulk script's dry run, to show current state
first is good practice), same as any other real-world-effect action.

### What the bulk sync does and doesn't touch

- **Never changes an existing member's status.** Merge fields are updated in
  place, so someone who unsubscribed stays unsubscribed and simply stops
  carrying stale den data. Only emails that aren't on the list at all get a
  status, from `--new-member-status` (`subscribed`, or `pending` to make
  Mailchimp send its confirmation opt-in). Adding a parent to a mailing list
  is a consent decision — ask before a run that creates anyone, or pass
  `--no-create` to update only people already on the list.
- **Never overwrites `FNAME`/`LNAME` that already have a value**, only fills
  empty ones. Mailchimp holds what a parent entered about themselves; the
  roster has one "name" string whose naive split turns "Mary Jane Smith"
  into FNAME "Mary" / LNAME "Jane Smith", and on a two-parent household it
  would replace one parent's name with the other's.
- **Leaves non-roster members completely alone** — alumni, prospects, and past
  families outnumber current ones on this audience.
- **`SCOUT{n}DEN` is the program level** (Lion/Tiger/Wolf/Bear/Webelos/AOL),
  derived from the roster's den unit name ("Aol Den 6 (Male)" -> "AOL"). That
  matches what the audience has always held and what reads well in an email.
  The field is only three slots wide; a family with more scouts is reported.
- **Tags every roster member `existing`** (`--tag NAME` to use a different
  one, `--no-tag` to skip), adding it only where missing and never removing
  anything. Mailchimp tag names are case-sensitive, so pass the exact spelling
  the audience already uses — run `tags` first to check. Note that `tags`
  reports **subscribed** members only, while a full `list` dump counts
  unsubscribed ones too; the two numbers differ by design, so compare
  like with like before concluding a tag was lost.
- **`--include-unregistered`** additionally adds payers whose scouts paid dues
  but aren't in Scoutbook yet, from the dues form's own names. Off by default:
  the pack hasn't registered those scouts, so it's the user's call.

## Troubleshooting

- **Exit code 2** — credentials missing; check
  `~/.scouting-skills/mailchimp.env` exists and has all three
  `MAILCHIMP_*` values (see Credentials above).
- **Exit code 4** — Mailchimp API rejected the request; the error detail
  from Mailchimp is printed to stderr (e.g. a malformed email, a merge
  field that doesn't exist on this audience — check `summary`'s output or
  the Mailchimp app's Audience > Settings > Merge fields to see what
  fields actually exist before upserting new ones).
- **`get`/`search` finds nothing for an email you know is on the list** —
  double check for typos/case; the API itself lowercases for hashing but
  `search` does fuzzy matching that can still miss unusual formatting.
- **Tag changes don't seem to apply** — Mailchimp tag names are
  case-sensitive and must match existing tags exactly to merge into the
  same tag; run `tags` first to see the exact spelling in use.
