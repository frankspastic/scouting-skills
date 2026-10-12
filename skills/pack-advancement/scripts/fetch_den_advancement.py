#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.40"]
# ///
"""
fetch_den_advancement.py — fetch rank and adventure progress for one den's
scouts straight from Scoutbook Plus (advancements.scouting.org), without the
legacy Custom Reports system.

How it works: Scoutbook Plus's youth profile page loads each scout's
advancement as JSON from api.scouting.org. Like fetch_roster.py, this script
just opens the pages in the shared browser profile and records the JSON the
site itself loads — it never handles tokens or calls the API directly:

  1. reads the unit youth list fetch_roster.py saved in raw/, which maps
     each scout's BSA member ID to the Scoutbook user ID the profile URL
     needs;
  2. picks the den's scouts from roster.json (run pack-roster's
     fetch_roster.py first);
  3. opens advancements.scouting.org/youthProfile/<userId> for each scout
     and captures .../youth/<userId>/ranks and .../youth/<userId>/adventures.

Usage:
    uv run fetch_den_advancement.py --den "Webelos Den 7"
        [--max-age-hours 24] [--force] [--login-timeout 300]

Outputs (in --data-dir, default ~/.scouting-skills/):
    den-advancement/<den-slug>.json   normalized per-scout ranks + adventures

Exit codes:
    0  success (fresh fetch, or cache was recent enough)
    2  environment/login failure (browser missing, login never completed)
    3  den not found in roster.json, or no advancement data captured
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://advancements.scouting.org"
ROSTER_URL = f"{BASE_URL}/roster"
PROFILE_URL = f"{BASE_URL}/youthProfile/{{user_id}}"

# Scoutbook rank IDs (verified against rank/adventure links in Scoutbook).
RANK_IDS = {"Lion": 14, "Bobcat": 13, "Tiger": 8, "Wolf": 9, "Bear": 10,
            "Webelos": 11, "Arrow of Light": 12}
RANK_NAMES = {v: k for k, v in RANK_IDS.items()}

YOUTHS_RE = re.compile(r"/organizations/v2/units/[^/]+/youths(\?|$)", re.I)
YOUTH_RANKS_RE = re.compile(r"/advancements/v2/youth/(\d+)/ranks(\?|$)", re.I)
YOUTH_ADV_RE = re.compile(r"/advancements/v2/youth/(\d+)/adventures(\?|$)", re.I)
YOUTH_RANK_REQS_RE = re.compile(
    r"/advancements/v2/youth/(\d+)/ranks/(\d+)/requirements(\?|$)", re.I)
ADV_CATALOG_RE = re.compile(r"/advancements/adventures(\?|$)", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[fetch_den_advancement] {msg}", file=sys.stderr, flush=True)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def den_rank(den: str) -> str | None:
    """'Webelos Den 7' -> 'Webelos'; 'Aol Den 6 (Male)' -> 'Arrow of Light'."""
    d = den.lower()
    if d.startswith("aol") or "arrow of light" in d:
        return "Arrow of Light"
    for rank in RANK_IDS:
        if d.startswith(rank.lower()):
            return rank
    return None


def cache_is_fresh(path: Path, max_age_hours: float) -> bool:
    if not path.exists():
        return False
    try:
        fetched = datetime.fromisoformat(json.loads(path.read_text())["fetched_at"])
    except Exception:
        return False
    age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
    if age_h <= max_age_hours:
        log(f"Cached den advancement is {age_h:.1f}h old "
            f"(limit {max_age_hours}h) — using cache.")
        return True
    return False


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def norm_rank(r: dict) -> dict:
    return {
        "name": r.get("name"),
        "rank_id": r.get("id"),
        "version": r.get("version"),
        "status": r.get("status"),
        "percent_completed": r.get("percentCompleted"),
        "date_earned": r.get("dateEarned") or None,
        "leader_approved": r.get("leaderApprovedDate") or None,
        "awarded": r.get("awardedDate") or None,
    }


def norm_adventure(a: dict) -> dict:
    return {
        "name": a.get("adventureName") or a.get("shortName"),
        "adventure_id": a.get("adventureId"),
        "rank": RANK_NAMES.get(a.get("rankId"), str(a.get("rankId"))),
        "rank_version": a.get("rankVersion"),
        "required": bool(a.get("isRequired")),
        "sort_order": a.get("sortOrder"),
        "status": a.get("status"),
        "percent_completed": a.get("percentCompleted"),
        "completed": a.get("dateCompleted") or None,
        "leader_approved": a.get("leaderApprovedDate") or None,
        "awarded": a.get("awardedDate") or None,
    }


def norm_rank_requirements(body: dict | None) -> dict | None:
    """The den rank's own requirement list (1a-1f required adventures, 2a/2b
    elective slots, ...) with this scout's status on each."""
    if not body:
        return None
    reqs = []
    for r in sorted(body.get("requirements") or [],
                    key=lambda r: float(r.get("sortOrder") or 0)):
        if not r.get("requirementNumber"):
            continue  # section headers ("Complete the six required adventures:")
        reqs.append({
            "number": r["requirementNumber"],
            "name": r.get("name"),
            "adventure_id": r.get("linkedAdventureId"),
            # 2a/2b-style "any elective" slots; which elective filled the
            # slot isn't said here — see the scout's adventures list.
            "elective_slot": bool(r.get("electiveAdventure")),
            "status": r.get("status"),
            "percent_completed": r.get("percentCompleted"),
            "completed": r.get("dateCompleted") or None,
            "leader_approved": r.get("leaderApprovedDate") or None,
        })
    return {
        "rank": body.get("name"),
        "version": body.get("version"),
        "status": body.get("status"),
        "percent_completed": body.get("percentCompleted"),
        "awarded": body.get("awardedDate") or body.get("dateEarned") or None,
        "requirements": reqs,
    }


def ranks_from_body(body) -> list[dict]:
    out = []
    for prog in (body or {}).get("program", []) or []:
        out.extend(norm_rank(r) for r in prog.get("ranks", []) or [])
    return out


def load_saved_youth_ids(data_dir: Path) -> dict[str, str]:
    """memberId -> userId from the newest raw/*units*youths*.json capture."""
    caps = sorted((data_dir / "raw").glob("*units_*_youths*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for cap in caps:
        try:
            users = (json.loads(cap.read_text()).get("body") or {}).get("users") or []
        except Exception:
            continue
        ids = {str(u["memberId"]): str(u["userId"]) for u in users
               if u.get("memberId") and u.get("userId")}
        if ids:
            return ids
    return {}


# ---------------------------------------------------------------------------
# Browser fetch
# ---------------------------------------------------------------------------

def on_login_page(page) -> bool:
    return any(w in page.url.lower() for w in ("login", "signin"))


def fetch_live(den_scouts: list[dict], rank_id: int | None, data_dir: Path,
               login_timeout: int, debug: bool = False) -> tuple[dict, list]:
    """Return ({bsa_id: {...}} for den_scouts, the full adventure catalog)."""
    from playwright.sync_api import sync_playwright

    youths: dict[str, str] = {}          # memberId -> userId
    ranks: dict[str, list] = {}          # userId -> ranks body
    advs: dict[str, list] = {}           # userId -> adventures body
    rank_reqs: dict[tuple, dict] = {}    # (userId, rankId) -> requirements body
    catalog: list = []                   # every adventure, every version

    def on_response(resp):
        url = resp.url
        try:
            if "json" not in (resp.headers.get("content-type") or "").lower():
                return
            if debug and "api.scouting.org" in url:
                log(f"  saw {resp.status} {url.split('?')[0]}")
            if YOUTHS_RE.search(url):
                for y in (resp.json() or {}).get("users") or []:
                    if y.get("memberId") and y.get("userId"):
                        youths[str(y["memberId"])] = str(y["userId"])
            elif m := YOUTH_RANKS_RE.search(url):
                ranks[m.group(1)] = resp.json()
            elif m := YOUTH_ADV_RE.search(url):
                advs[m.group(1)] = resp.json()
            elif m := YOUTH_RANK_REQS_RE.search(url):
                rank_reqs[(m.group(1), m.group(2))] = resp.json()
            elif ADV_CATALOG_RE.search(url) and not catalog:
                catalog.extend((resp.json() or {}).get("adventures") or [])
        except Exception:
            pass  # a body we can't read is just a missed capture

    profile_dir = data_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,  # login may need a human (CAPTCHA)
                viewport={"width": 1280, "height": 900},
            )
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                log("Chromium is not installed for Playwright. Run:")
                log("  uv run --with playwright playwright install chromium")
                sys.exit(2)
            raise
        context.on("response", on_response)
        page = context.pages[0] if context.pages else context.new_page()

        # Scoutbook user IDs (needed for profile URLs) come from the unit youth
        # list that fetch_roster.py captures into raw/ on every run.
        youths.update(load_saved_youth_ids(data_dir))
        if not youths:
            log("No saved unit youth list in raw/ — run pack-roster's "
                "fetch_roster.py first.")
            context.close()
            sys.exit(3)
        log(f"Mapped {len(youths)} youth member IDs from the saved roster capture.")

        first_uid = next((youths[s["bsa_id"]] for s in den_scouts
                          if s["bsa_id"] in youths), None)
        if first_uid:
            try:
                page.goto(PROFILE_URL.format(user_id=first_uid), timeout=60_000)
            except Exception:
                pass
            if on_login_page(page):
                print("\n" + "=" * 64, flush=True)
                print("A browser window is open showing a login page. Please log in", flush=True)
                print("there (solve the CAPTCHA if asked). The fetch resumes on its", flush=True)
                print("own once you're in.", flush=True)
                print("=" * 64 + "\n", flush=True)
                deadline = time.time() + login_timeout
                while on_login_page(page) and time.time() < deadline:
                    page.wait_for_timeout(2000)
                if on_login_page(page):
                    log("Login never completed within the timeout.")
                    context.close()
                    sys.exit(2)

        for s in den_scouts:
            uid = youths.get(s["bsa_id"])
            name = f"{s['first_name']} {s['last_name']}"
            if not uid:
                log(f"  {name}: not in Scoutbook's youth list — skipped")
                continue
            log(f"  {name}: opening profile {uid} …")
            try:
                page.goto(PROFILE_URL.format(user_id=uid), timeout=60_000)
            except Exception:
                pass
            want = [lambda: uid in ranks, lambda: uid in advs]
            if rank_id:
                want.append(lambda: (uid, str(rank_id)) in rank_reqs)
            t_end = time.time() + 30
            while time.time() < t_end and not all(w() for w in want):
                page.wait_for_timeout(500)
            if uid not in advs:
                log(f"  {name}: adventures never loaded")
            results[s["bsa_id"]] = {
                "user_id": uid,
                "ranks": ranks_from_body(ranks.get(uid)),
                "adventures": [norm_adventure(a) for a in advs.get(uid) or []],
                "raw_rank_requirements": rank_reqs.get((uid, str(rank_id))),
                "rank_progress": norm_rank_requirements(rank_reqs.get((uid, str(rank_id)))),
                "captured": uid in advs,
            }

        # Opening a scout's profile (especially your own child's) can leave
        # Scoutbook Plus in that scout's view, after which /roster redirects
        # to the profile and fetch_roster.py can't see the pack. Go back to
        # the roster and confirm the unit youth list still loads.
        youths.clear()
        try:
            page.goto(ROSTER_URL, timeout=60_000)
        except Exception:
            pass
        t_end = time.time() + 30
        while time.time() < t_end and not youths:
            page.wait_for_timeout(1000)
        if youths:
            log("Roster view check: OK (pack roster still loads).")
        else:
            log("WARNING: /roster no longer shows the pack roster "
                f"(landed on {page.url}). In this browser window, switch back "
                "to your pack leader role before running fetch_roster.py.")
        context.close()
    return results, catalog


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--den", required=True,
                    help='den name exactly as in roster.json, e.g. "Webelos Den 7"')
    ap.add_argument("--max-age-hours", type=float, default=24,
                    help="reuse the cached den file if newer than this (default 24)")
    ap.add_argument("--force", action="store_true",
                    help="fetch even if the cache is fresh")
    ap.add_argument("--login-timeout", type=int, default=300,
                    help="seconds to wait for login (default 300)")
    ap.add_argument("--debug", action="store_true",
                    help="log every api.scouting.org response seen")
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")))
    args = ap.parse_args()

    out_path = args.data_dir / "den-advancement" / f"{slugify(args.den)}.json"
    if not args.force and cache_is_fresh(out_path, args.max_age_hours):
        print(out_path)
        return

    roster_path = args.data_dir / "roster.json"
    if not roster_path.exists():
        log(f"{roster_path} not found — run pack-roster's fetch_roster.py first.")
        sys.exit(3)
    roster = json.loads(roster_path.read_text())
    den_scouts = [s for s in roster.get("scouts", [])
                  if (s.get("den") or "").lower() == args.den.lower()]
    if not den_scouts:
        dens = sorted({s.get("den") or "" for s in roster.get("scouts", [])} - {""})
        log(f'No scouts in den "{args.den}". Dens in roster.json: {", ".join(dens)}')
        sys.exit(3)
    rank = den_rank(den_scouts[0]["den"])
    log(f'{len(den_scouts)} scouts in "{den_scouts[0]["den"]}" (rank: {rank or "?"}).')

    results, catalog = fetch_live(den_scouts, RANK_IDS.get(rank), args.data_dir,
                                  args.login_timeout, args.debug)
    raw_dir = args.data_dir / "den-advancement" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for bsa_id, r in results.items():
        raw = r.pop("raw_rank_requirements", None)
        if raw:
            (raw_dir / f"{r['user_id']}_rank_requirements.json").write_text(
                json.dumps(raw, indent=1))
    if not any(r["captured"] for r in results.values()):
        log("No advancement data captured for any scout.")
        sys.exit(3)

    scouts = []
    for s in sorted(den_scouts, key=lambda s: (s["last_name"], s["first_name"])):
        r = results.get(s["bsa_id"], {})
        scouts.append({
            "name": f"{s['first_name']} {s['last_name']}",
            "first_name": s["first_name"], "last_name": s["last_name"],
            "bsa_id": s["bsa_id"], "user_id": r.get("user_id"),
            "captured": r.get("captured", False),
            "rank_progress": r.get("rank_progress"),
            "ranks": r.get("ranks", []),
            "adventures": r.get("adventures", []),
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "fetched_at": now_iso(),
        "source": PROFILE_URL,
        "den": den_scouts[0]["den"],
        "den_rank": rank,
        "scout_count": len(scouts),
        # The den rank's current-version adventures, for listing electives
        # nobody has started yet.
        "rank_catalog": [
            {"name": a.get("name"), "adventure_id": a.get("id"),
             "required": bool(a.get("required")), "version": a.get("version")}
            for a in catalog
            if a.get("rankId") == RANK_IDS.get(rank) and not a.get("expiryDt")
            and str(a.get("version")) == str(max(
                (b.get("version") or 0 for b in catalog
                 if b.get("rankId") == RANK_IDS.get(rank)), default=""))
        ],
        "scouts": scouts,
    }, indent=2))
    log(f"Wrote {len(scouts)} scouts to {out_path}")
    print(out_path)


if __name__ == "__main__":
    main()
