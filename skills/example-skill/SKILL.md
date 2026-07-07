---
name: example-skill
description: Template skill — replace this with a real Scouting skill. The description is what Claude uses to decide when to invoke the skill, so state what it does and when to use it.
---

# Example Skill

This is a placeholder so the repo has a valid skill to load. Copy this
directory to create a new skill:

```
skills/
└── my-new-skill/
    └── SKILL.md
```

Each skill is a directory under `skills/` containing a `SKILL.md` with
YAML frontmatter (`name`, `description`) followed by the instructions
Claude should follow when the skill is invoked. Supporting files
(scripts, references, templates) can live alongside `SKILL.md` in the
same directory.

Delete this skill once you have real ones.
