# scouting-skills

A [Claude Code plugin](https://code.claude.com/docs/en/plugins.md) of skills
for Cub Scouts and Scouting America.

This repo is a single plugin that also serves as its own plugin marketplace
(`.claude-plugin/marketplace.json` lists the repo itself as the only plugin),
so it can be installed directly with the `/plugin` commands.

## Install

```
/plugin marketplace add <github-owner>/scouting-skills
/plugin install scouting-skills@scouting-skills
```

## Setup

Before using these skills for a pack other than Pack 97, see
[SETUP.md](SETUP.md) for the credentials, config files, and pack-specific
content each skill needs.

## Local development

Run Claude Code with the plugin loaded straight from this directory
(no install needed):

```
claude --plugin-dir /Users/frankmaulit/Sites/scout/scouting-skills
```

Or add the local checkout as a marketplace:

```
/plugin marketplace add /Users/frankmaulit/Sites/scout/scouting-skills
/plugin install scouting-skills@scouting-skills
```

After editing a skill in a running session, use `/reload-plugins` to pick up
changes. Validate manifests with:

```
claude plugin validate .
```

## Skills

- **pack-calendar** — fetches and reports Pack 97 events (meetings,
  campouts, deadlines) from the pack's public Google Calendar ICS feed;
  no login required.
- **pack-roster** — fetches the current pack roster (scouts, dens, ranks,
  BSA IDs, parent contacts) from Scoutbook Plus
  (advancements.scouting.org) via browser network capture, with a 24h
  local cache in `~/.scouting-skills/`; joins in dues payments from the
  year's payment-form spreadsheets (more than one, since early payers land
  in the prior year's sheet) to answer who still owes, and can sync the
  roster — with a "Dues Paid" column — into a Google Sheet.
- **ypt-aging-report** — fetches the Safeguarding Youth Training (YPT)
  Aging Report (each adult's training expiration status) from the
  Scoutbook Plus Reports page, same network-capture approach and cache
  location, sharing the pack-roster login session.
- **pack-advancement** — fetches the "Full Pack Advancement Report" custom
  report (rank and adventure completion per scout) from Scoutbook Plus,
  joins it with the pack-roster den assignments, and reports advancement
  progress by den; same cache location and shared login session.
- **family-advancement-emails** — generates one ready-to-send advancement
  update email per family (siblings auto-combined) from the cached
  pack-advancement + pack-roster data, so emailing the whole pack costs one
  script run plus one Gmail tool call per family instead of a drafting pass
  per kid in conversation.
- **mailchimp-audience** — reports on and maintains the pack's Mailchimp
  audience (member counts, tags, a member's status/merge fields, ad-hoc
  add/update/tag/unsubscribe) via direct Mailchimp API calls, and bulk-syncs
  the roster into the audience's per-scout merge fields (den, expiration,
  birthday, dues paid) from the pack-roster caches; campaign drafting is
  left to the connected Mailchimp app instead of being duplicated here.

## Adding a skill

Create a directory under `skills/` with a `SKILL.md`:

```
skills/
└── den-meeting-planner/
    └── SKILL.md
```

`SKILL.md` needs YAML frontmatter with `name` and `description` (the
description drives when Claude auto-invokes the skill), followed by the
instructions. Bundle supporting scripts in a `scripts/` subdirectory (see
`skills/pack-roster/` for an example).

Every new skill must also be added to the [Skills](#skills) list above so
this README stays a complete index of the plugin.
