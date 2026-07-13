---
name: family-advancement-emails
description: Generate and send per-family advancement update emails to parents, covering each of their scouts' rank and adventure progress. Use this when the user wants to email one or more families about their scout's (or scouts') advancement progress — "email the Smiths about Ava's progress", "send an update to everyone in Bear den", "send advancement updates to the whole pack" — especially for more than a couple of families, where drafting each email in conversation would be slow and token-heavy.
---

# Family Advancement Update Emails

Turns the cached `pack-advancement` + `pack-roster` data into ready-to-send
email drafts, one per family (siblings automatically combined into a single
email), without having to draft each email's text by hand in conversation.

## Why this exists

Drafting one advancement email at a time — look up the scout, look up the
parent, compose prose, confirm, send — is fine for one family but doesn't
scale: for the whole pack it means dozens of round trips, each adding to a
growing conversation that gets more expensive to carry as it goes.
`scripts/generate_family_updates.py` does the lookup, sibling-grouping, and
prose generation in one local pass with no model calls involved, so
answering "email the whole pack" costs one script run plus one Gmail tool
call per family — not one drafting pass per kid.

## How it works

`scripts/generate_family_updates.py` reads the two caches other skills
already maintain — **no network calls of its own**:
- `~/.scouting-skills/advancement.json` (from the `pack-advancement` skill)
- `~/.scouting-skills/roster.json` (from the `pack-roster` skill)

It groups scouts into families by shared parent email using union-find, so
siblings are merged into one email even when their parent-email lists don't
fully match (e.g. one kid's Scoutbook record lists only Dad, another lists
Mom and Dad — they still end up in the same email since the emails overlap
on Dad's address). For each family it renders a plain-text subject and body
covering, per kid: current rank and completion date (or "working toward"
progress), required-adventure completion (via
`reference/required_adventures.json`, shared with the `pack-advancement`
skill — keep the two in sync if you update one), electives completed, and
awards.

## Workflow

1. Make sure both source caches are fresh — from their respective skills'
   directories:

   ```bash
   uv run ../pack-advancement/scripts/fetch_advancement.py --max-age-hours 24
   uv run ../pack-roster/scripts/fetch_roster.py --max-age-hours 24
   ```

   (Adjust the relative path if invoked from elsewhere — both scripts
   accept `--data-dir` too.) These may open a browser for login; see those
   skills' own SKILL.md for details.

2. Generate the drafts:

   ```bash
   uv run scripts/generate_family_updates.py
   ```

   Use `--only "LastName1,LastName2"` to restrict to specific families —
   good for spot-checking wording before a pack-wide send, or when the user
   only wants a handful of families. Output:
   - `~/.scouting-skills/family_advancement_emails.json` — `[{to, subject,
     body, kids}, …]`, one entry per family.
   - `~/.scouting-skills/family_advancement_emails.md` — the same content
     as a single readable document, for one skim instead of reading each
     JSON entry.

3. Read the JSON (not the whole `.md` unless the user wants to eyeball
   wording) and decide how to deliver it — this is real email going to real
   families, so get **one** explicit go-ahead for the whole batch rather
   than asking per family:
   - **Drafts (safer default for a first pack-wide send)** — call
     `mcp__gmail__draft_email` once per entry (`to`, `subject`, `body`
     straight from the JSON). The user reviews and sends each from their
     own inbox.
   - **Send directly** — only when the user has clearly said to send (as
     opposed to draft) — call `mcp__gmail__send_email` once per entry.
   - Issue these as multiple tool calls in one turn where possible rather
     than one round-trip per family; the tool-call parameters are already
     fully rendered, so there's no drafting cost per call.
   - Show a short summary first (family count, kid count, any scouts
     skipped for missing email — logged by the script) so the user isn't
     confirming blind.

4. This generates and sends real communications to real families — the
   same PII-handling rules as the other skills apply to the cache files,
   plus: don't send/draft without the explicit go-ahead in step 3, and
   don't silently drop scouts with no parent email on file — the script
   logs them; tell the user who was skipped.

## Troubleshooting

- **Exit code 3** — `advancement.json` or `roster.json` doesn't exist yet;
  run the two fetches in step 1 first.
- **A family's wording looks off** — check
  `~/.scouting-skills/family_advancement_emails.md` for that family and
  adjust `render_kid_section()` / `render_family()` in the script; rerun
  with `--only "LastName"` to iterate quickly without regenerating the
  whole pack.
- **Missing/wrong required-adventure flagging** — this reuses the same
  `required_adventures.json` shape as `pack-advancement`; if that skill's
  copy gets updated (BSA renames an adventure), copy the change into this
  skill's `reference/required_adventures.json` too.
- **A den's rank looks wrong for a brand-new scout** — `infer_rank()` falls
  back to parsing the rank out of the den name when `current_rank` is
  blank (new scouts with zero completions have no `current_rank` yet); if
  a den name doesn't state its rank clearly (e.g. a renamed or opt-out
  den), it'll produce an empty rank and the script reports "No den/rank on
  file yet." — check the roster den name in that case.
- **Two scouts who should be siblings ended up in separate emails** — they
  share no parent email in Scoutbook (e.g. divorced parents each listing
  only themselves for their own kid); there's nothing to group on, so
  they'll get separate emails. That's correct behavior, not a bug.
