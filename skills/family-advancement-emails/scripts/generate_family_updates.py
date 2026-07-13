#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
generate_family_updates.py — turn the cached pack-advancement + pack-roster
data into one ready-to-send email draft per family, so sending an
advancement update to the whole pack costs one tool call per family instead
of a full drafting pass in conversation for each kid.

Reads (already-fetched, local cache — this script makes no network calls):
    ~/.scouting-skills/advancement.json   (pack-advancement skill)
    ~/.scouting-skills/roster.json        (pack-roster skill)

Groups scouts into families by shared parent email (siblings are merged into
one email even if their parent-email lists only partially overlap — e.g. one
kid lists just Dad, another lists Mom and Dad), and renders a plain-text
subject/body per family covering each kid's current rank status, required
adventures, electives, and awards.

Usage:
    uv run generate_family_updates.py [--data-dir ~/.scouting-skills]
        [--out family_advancement_emails.json]
        [--only "Last1,Last2"]   # restrict to families containing these
                                  # scout last names, for testing/spot review

Outputs (in --data-dir):
    family_advancement_emails.json    [{to, subject, body, kids}, …]
    family_advancement_emails.md      human-readable preview of every draft,
                                       for a single skim before sending

Exit codes:
    0  success
    3  advancement.json / roster.json missing or empty — fetch them first
"""

import argparse
import json
import os
import sys
from pathlib import Path

RANK_WORDS = ("Lion", "Tiger", "Wolf", "Bear", "Webelos", "Arrow of Light")


def log(msg: str) -> None:
    print(f"[generate_family_updates] {msg}", flush=True)


def load_required_adventures() -> dict:
    path = Path(__file__).resolve().parent.parent / "reference" / "required_adventures.json"
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def infer_rank(scout: dict, den: str) -> str:
    """The rank a scout is currently working toward: current_rank when set
    (a brand-new scout with no completions yet has none), else read it off
    the den name, which always names the rank directly."""
    if scout.get("current_rank"):
        return scout["current_rank"]
    for w in RANK_WORDS:
        if w.lower() in den.lower():
            return w
    return ""


# ---------------------------------------------------------------------------
# Family grouping (union-find over scouts, merged by shared parent email)
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def group_families(roster_scouts: list[dict]) -> list[list[dict]]:
    uf = UnionFind(r["bsa_id"] for r in roster_scouts)
    by_email: dict[str, list[str]] = {}
    for r in roster_scouts:
        for p in r.get("parents", []):
            email = (p.get("email") or "").strip().lower()
            if not email:
                continue
            by_email.setdefault(email, []).append(r["bsa_id"])
    for ids in by_email.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    groups: dict[str, list[dict]] = {}
    for r in roster_scouts:
        root = uf.find(r["bsa_id"])
        groups.setdefault(root, []).append(r)
    return list(groups.values())


# ---------------------------------------------------------------------------
# Per-kid / per-family text rendering
# ---------------------------------------------------------------------------

def render_kid_section(roster_entry: dict, adv_scout: dict, required: dict) -> str:
    den = roster_entry.get("den", "").strip()
    rank = infer_rank(adv_scout, den)
    lines = [f"{adv_scout['name']}" + (f" ({den})" if den else "") + ":"]

    if not rank:
        lines.append("  No den/rank on file yet.")
        return "\n".join(lines)

    completed_on = adv_scout["ranks"].get(rank)
    if completed_on:
        lines.append(f"  Completed {rank} rank on {completed_on}.")
    else:
        lines.append(f"  Working toward {rank} rank.")

    req_names = required.get(rank, [])
    if req_names:
        done = [n for n in req_names if any(
            a["name"] == n for a in adv_scout["adventures"])]
        pending = [n for n in req_names if n not in done]
        if not pending:
            lines.append(f"  Required adventures: all {len(req_names)} done.")
        else:
            lines.append(
                f"  Required adventures: {len(done)} of {len(req_names)} done"
                + (f" — still needed: {', '.join(pending)}." if pending else "."))
    elif rank == "Tiger":
        lines.append("  Required rank requirements tracked directly on the "
                      "Tiger rank (not as separate adventures).")

    elective_names = [a["name"] for a in adv_scout["adventures"]
                       if a["rank"] == rank and a["name"] not in req_names]
    if elective_names:
        lines.append(f"  Electives completed ({len(elective_names)}): "
                     + ", ".join(elective_names) + ".")

    if not req_names and not elective_names and not completed_on:
        lines.append("  No adventures logged yet.")

    if adv_scout["awards"]:
        award_bits = [f"{a['name']} ({a['completed']})" for a in adv_scout["awards"]
                     if a.get("completed")]
        if award_bits:
            lines.append("  Awards: " + "; ".join(award_bits) + ".")

    return "\n".join(lines)


def parent_first_names(family: list[dict]) -> list[str]:
    seen = {}
    for r in family:
        for p in r.get("parents", []):
            email = (p.get("email") or "").strip().lower()
            name = (p.get("name") or "").strip()
            if email and email not in seen:
                seen[email] = name.split()[0] if name else email
    return list(seen.values())


def kid_first_names(family: list[dict]) -> list[str]:
    return [r["first_name"] for r in sorted(family, key=lambda r: r["last_name"])]


def join_and(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def render_family(family: list[dict], by_id: dict, required: dict) -> dict | None:
    recipients = sorted({
        (p.get("email") or "").strip().lower()
        for r in family for p in r.get("parents", [])
        if (p.get("email") or "").strip()
    })
    if not recipients:
        return None

    kids_present = [(r, by_id[r["bsa_id"]]) for r in family if r["bsa_id"] in by_id]
    if not kids_present:
        return None
    kids_present.sort(key=lambda p: p[0]["last_name"])

    kid_names = kid_first_names(family)
    subject = join_and(kid_names) + "'s Advancement Update"

    parents = parent_first_names(family)
    greeting = f"Hi {join_and(parents)}," if parents else "Hi,"

    sections = [render_kid_section(r, a, required) for r, a in kids_present]
    body = (
        f"{greeting}\n\n"
        f"Quick update on {join_and(kid_names)}'s advancement progress:\n\n"
        + "\n\n".join(sections)
        + f"\n\nThanks for all you do to support {join_and(kid_names)}'s scouting journey!"
    )

    return {
        "to": recipients,
        "subject": subject,
        "body": body,
        "kids": [r["first_name"] + " " + r["last_name"] for r, _ in kids_present],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")))
    ap.add_argument("--out", default="family_advancement_emails.json")
    ap.add_argument("--only", default=None,
                    help="comma-separated scout last names to restrict output to")
    args = ap.parse_args()

    data_dir = args.data_dir.expanduser()
    adv_path = data_dir / "advancement.json"
    ros_path = data_dir / "roster.json"
    if not adv_path.exists() or not ros_path.exists():
        log(f"Missing {adv_path} or {ros_path} — fetch both first "
            "(pack-advancement's fetch_advancement.py and pack-roster's "
            "fetch_roster.py).")
        sys.exit(3)

    adv = json.loads(adv_path.read_text())
    ros = json.loads(ros_path.read_text())
    required = load_required_adventures()

    by_id = {s["bsa_id"]: s for s in adv["scouts"]}
    roster_scouts = ros["scouts"]
    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        roster_scouts = [r for r in roster_scouts
                         if r["last_name"].lower() in wanted]

    families = group_families(roster_scouts)
    drafts = []
    no_email = []
    for family in families:
        draft = render_family(family, by_id, required)
        if draft:
            drafts.append(draft)
        else:
            no_email.extend(r["first_name"] + " " + r["last_name"] for r in family)

    drafts.sort(key=lambda d: d["kids"][0])

    out_path = data_dir / args.out
    out_path.write_text(json.dumps(drafts, indent=2))

    preview_path = data_dir / (Path(args.out).stem + ".md")
    with open(preview_path, "w") as f:
        f.write(f"# Family Advancement Update Drafts ({len(drafts)} families)\n\n")
        f.write(f"Source: advancement.json fetched {adv.get('fetched_at')}, "
               f"roster.json fetched {ros.get('fetched_at')}.\n\n")
        if no_email:
            f.write(f"**No parent email on file, skipped:** {', '.join(no_email)}\n\n")
        for d in drafts:
            f.write(f"---\n\n**To:** {', '.join(d['to'])}\n\n"
                   f"**Subject:** {d['subject']}\n\n```\n{d['body']}\n```\n\n")

    log(f"Wrote {len(drafts)} family drafts to {out_path}")
    log(f"Preview: {preview_path}")
    if no_email:
        log(f"{len(no_email)} scout(s) skipped (no parent email on file): "
            f"{', '.join(no_email)}")
    print(out_path)


if __name__ == "__main__":
    main()
