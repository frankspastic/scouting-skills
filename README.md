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

- **pack-roster** — fetches the current pack roster (scouts, dens, ranks,
  BSA IDs, parent contacts) from Scoutbook Plus
  (advancements.scouting.org) via browser network capture, with a 24h
  local cache in `~/.scouting-skills/`.

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
