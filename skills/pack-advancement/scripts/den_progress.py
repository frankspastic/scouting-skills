#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
den_progress.py — print a markdown rank-progress report for one den from the
file fetch_den_advancement.py wrote.

  * a summary table: each scout's rank status and required/elective counts;
  * a detail table (--status-words for Complete/Incomplete/Not Started
    cells): one row per required adventure (in the rank's own
    requirement order), then one row per elective at least one scout has
    started; one column per scout. A cell is the completion date (M/D/YY)
    when done, the percent complete when started, blank otherwise.

Usage:
    uv run den_progress.py --den "Webelos Den 7" [--initials]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def short_date(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = iso[:10].split("-")
    return f"{int(m)}/{int(d)}/{y[2:]}"


def is_done(item: dict | None) -> bool:
    return bool(item and (item.get("completed") or item.get("leader_approved")
                          or item.get("awarded")))


def cell(item: dict | None) -> str:
    if not item:
        return ""
    if is_done(item):
        return short_date(item.get("completed") or item.get("leader_approved")
                          or item.get("awarded"))
    pct = item.get("percent_completed") or 0
    return f"{round(pct * 100)}%" if pct else ""


def status_word(item: dict | None) -> str:
    if is_done(item):
        return "Complete"
    if item and (item.get("percent_completed") or 0) > 0:
        return "Incomplete"
    return "Not Started"


def label(s: dict, initials: bool) -> str:
    return f"{s['first_name']} {s['last_name'][:1]}." if initials else s["name"]


def analyze(data: dict) -> dict:
    """Work out the den rank's rows and each scout's adventures by ID."""
    rank = data["den_rank"]
    scouts = [s for s in data["scouts"] if s.get("captured")]
    missing = [s["name"] for s in data["scouts"] if not s.get("captured")]
    if not scouts:
        sys.exit("No scout in the file has advancement data — refetch.")

    # Row layout (requirement order + names) comes from the rank's own
    # requirement list, identical for every scout; take the first one that
    # loaded. Cell values come from each scout's adventures list, which is
    # the more reliable source: it carries partial progress and is present
    # even when a scout's rank-requirements response comes back empty.
    layout = next((s["rank_progress"]["requirements"] for s in scouts
                   if (s.get("rank_progress") or {}).get("requirements")), None)
    if layout:
        required = [(r["name"], r["adventure_id"]) for r in layout if r["adventure_id"]]
        electives_needed = sum(1 for r in layout if r["elective_slot"])
    else:  # fall back to the catalog's required list
        required = [(a["name"], a["adventure_id"])
                    for a in data.get("rank_catalog", []) if a["required"]]
        electives_needed = 2  # every 2024 rank
    required_ids = {aid for _, aid in required}
    version = next((r["version"] for s in scouts for r in s["ranks"]
                    if r["name"] == rank and r.get("version")), None)

    def rank_advs(s):
        return [a for a in s["adventures"] if a["rank"] == rank
                and (not version or a["rank_version"] in (None, version))]

    def by_id(s):
        return {a["adventure_id"]: a for a in rank_advs(s)}

    def electives(s):
        return {a["name"]: a for a in rank_advs(s)
                if a["adventure_id"] not in required_ids}

    return {
        "rank": rank, "version": version, "scouts": scouts, "missing": missing,
        "required": required, "electives_needed": electives_needed,
        "by_id": [by_id(s) for s in scouts],
        "electives": [electives(s) for s in scouts],
    }


def adventure_grid(info: dict, show) -> list[tuple[str, bool, list[str]]]:
    """(adventure name, required?, one cell per scout): required adventures
    in the rank's order, then electives at least one scout has started."""
    rows = [(name, True, [show(advs.get(aid)) for advs in info["by_id"]])
            for name, aid in info["required"]]
    started = sorted({n for e in info["electives"] for n, a in e.items() if cell(a)})
    rows += [(name, False, [show(e.get(name)) for e in info["electives"]])
             for name in started]
    return rows


def local_date(iso: str) -> str:
    """fetched_at (UTC ISO) as the local calendar date."""
    return datetime.fromisoformat(iso).astimezone().date().isoformat()


def load_den(data_dir: Path, den: str) -> dict:
    path = data_dir / "den-advancement" / f"{slugify(den)}.json"
    if not path.exists():
        sys.exit(f"{path} not found — run fetch_den_advancement.py first.")
    return json.loads(path.read_text())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--den", required=True)
    ap.add_argument("--initials", action="store_true",
                    help='column headers as "First L." instead of full names')
    ap.add_argument("--status-words", action="store_true",
                    help='cells say "Complete" / "Incomplete" / "Not Started" '
                         'instead of dates and percents')
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")))
    args = ap.parse_args()
    show = status_word if args.status_words else cell

    data = load_den(args.data_dir, args.den)
    info = analyze(data)
    rank, version, scouts = info["rank"], info["version"], info["scouts"]
    required, electives_needed = info["required"], info["electives_needed"]

    print(f"### {data['den']} — {rank} rank" + (f" ({version})" if version else ""))
    print(f"\n_Scoutbook Plus data as of {local_date(data['fetched_at'])}_\n")

    print("| Scout | Rank | Required adventures | Electives |")
    print("|---|---|---|---|")
    for i, s in enumerate(scouts):
        r = next((r for r in s["ranks"] if r["name"] == rank
                  and (not version or r["version"] == version)), None)
        status = (r or {}).get("status") or "Not Started"
        if r and r.get("awarded"):
            status += f" {short_date(r['awarded'])}"
        advs, els = info["by_id"][i], info["electives"][i]
        req_done = sum(is_done(advs.get(aid)) for _, aid in required)
        el_done = sum(is_done(a) for a in els.values())
        print(f"| {s['name']} | {status} | {req_done}/{len(required)} "
              f"| {min(el_done, electives_needed)}/{electives_needed}"
              + (f" (+{el_done - electives_needed})" if el_done > electives_needed else "")
              + " |")

    cols = [label(s, args.initials) for s in scouts]
    print("\n| Adventure | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    rows = adventure_grid(info, show)
    for name, req, cells in rows:
        print(f"| {'**' + name + '**' if req else name} | " + " | ".join(cells) + " |")
    if not any(not req for _, req, _ in rows):
        print("| _no electives started yet_ |" + " |" * len(cols))

    key = ("Complete = finished; Incomplete = started, not finished"
           if args.status_words else "Date = completed; % = started")
    print(f"\n_Bold = required. {key}. "
          f"The rank needs all {len(required)} required adventures "
          f"plus {electives_needed} electives._")
    if info["missing"]:
        print(f"\n_No data captured for: {', '.join(info['missing'])}_")


if __name__ == "__main__":
    main()
