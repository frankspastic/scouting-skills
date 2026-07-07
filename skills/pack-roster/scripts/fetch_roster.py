#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.40"]
# ///
"""
fetch_roster.py — fetch the current pack roster from Scoutbook Plus
(advancements.scouting.org).

Strategy: the site is a React SPA that loads roster data as JSON over XHR.
Scraping its DOM is fragile (class names are hashed and change between
deploys), so instead this script opens a real browser, lets the page make its
own API calls, and captures the JSON responses off the wire. A persistent
browser profile keeps the login session between runs, so the manual
CAPTCHA login is only needed when the session expires.

Usage:
    uv run fetch_roster.py [--max-age-hours 24] [--force] [--login-timeout 300]

Outputs (in --data-dir, default ~/.scouting-skills/):
    roster.json   normalized roster with fetched_at timestamp
    roster.csv    flat CSV export
    raw/          every JSON API response captured (for debugging/re-parsing)

Exit codes:
    0  success (fresh fetch, or cache was recent enough)
    2  environment/login failure (browser missing, login never completed)
    3  fetched pages but could not find roster-shaped JSON (raw/ saved —
       inspect it and improve the parser)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://advancements.scouting.org"
ROSTER_URL = f"{BASE_URL}/roster"

# Capture every JSON response except known noise: raw captures are cheap and
# are our only debugging artifact when the parser misses.
NOISE_URL_HINTS = (
    "sentry", "translations", "manifest", "analytics", "googletagmanager",
    "doubleclick", "hotjar", "launchdarkly",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[fetch_roster] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cache_is_fresh(roster_json: Path, max_age_hours: float) -> bool:
    if not roster_json.exists():
        return False
    try:
        data = json.loads(roster_json.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return False
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
    if age_hours <= max_age_hours:
        log(f"Cached roster is {age_hours:.1f}h old (limit {max_age_hours}h) — using cache.")
        return True
    return False


# ---------------------------------------------------------------------------
# Roster extraction from captured JSON
# ---------------------------------------------------------------------------

def _get_first(d: dict, *candidates) -> str:
    """Return the first non-empty value whose key matches a candidate
    (case-insensitive, ignoring underscores)."""
    normalized = {re.sub(r"[_\s]", "", k).lower(): v for k, v in d.items()}
    for cand in candidates:
        v = normalized.get(cand)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _looks_like_person(item) -> bool:
    if not isinstance(item, dict):
        return False
    keys = {re.sub(r"[_\s]", "", k).lower() for k in item.keys()}
    return bool({"firstname", "lastname"} & keys or {"firstname"} <= keys)


def find_person_lists(obj, path="$"):
    """Recursively yield (path, list) for every list of person-shaped dicts."""
    if isinstance(obj, list):
        people = [x for x in obj if _looks_like_person(x)]
        if people and len(people) >= max(1, len(obj) // 2):
            yield path, people
        else:
            for i, item in enumerate(obj):
                yield from find_person_lists(item, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_person_lists(v, f"{path}.{k}")


def normalize_person(p: dict) -> dict:
    parents = []
    for key in ("parents", "guardians", "parentsGuardians", "relationships", "adults"):
        rel_list = p.get(key)
        if isinstance(rel_list, list):
            for r in rel_list:
                if not isinstance(r, dict):
                    continue
                name = _get_first(r, "fullname", "name") or (
                    f"{_get_first(r, 'firstname')} {_get_first(r, 'lastname')}".strip()
                )
                parents.append({
                    "name": name,
                    "relationship": _get_first(r, "relationship", "relationshiptype", "role"),
                    "email": _get_first(r, "email", "emailaddress"),
                })
    den = _get_first(p, "denname", "subunitname", "den", "patrolname", "subunit")
    if not den:
        den_type = _get_first(p, "dentype")
        den_num = _get_first(p, "dennumber", "subunitnumber")
        den = f"{den_type} Den {den_num}".strip() if (den_type or den_num) else ""
    return {
        "first_name": _get_first(p, "firstname", "givenname"),
        "last_name": _get_first(p, "lastname", "surname", "familyname"),
        "bsa_id": _get_first(p, "memberid", "bsamemberid", "bsaid", "membernumber", "userid"),
        "den": den,
        "rank": _get_first(p, "currentrankname", "rankname", "currentrank", "rank",
                           "highestrankapproved"),
        "member_type": _get_first(p, "membertype", "positiontype", "isadult", "personrole"),
        "parents": parents,
    }


# denType arrives lowercase-plural ("wolves"); map to the proper den name.
DEN_TYPE_NAMES = {
    "lions": "Lion", "tigers": "Tiger", "wolves": "Wolf",
    "bears": "Bear", "webelos": "Webelos", "aols": "Arrow of Light",
}


def _den_from_positions(positions) -> str:
    for p in positions or []:
        raw = (p.get("denType") or "").lower()
        den_type = DEN_TYPE_NAMES.get(raw, raw.title())
        den_num = p.get("denNumber") or ""
        if den_type or den_num:
            return f"{den_type} Den {den_num}".strip()
        if p.get("patrolName"):
            return p["patrolName"]
    return ""


def _rank_from_history(y: dict) -> str:
    ranks = (y.get("highestRanksAwarded") or []) + (y.get("highestRanksApproved") or [])
    cub = [r for r in ranks if (r.get("program") or "").lower().startswith("cub")] or ranks
    if not cub:
        return ""
    best = max(cub, key=lambda r: r.get("level") or 0)
    return best.get("rank") or ""


def extract_unit_roster(captures: list[dict]) -> tuple[list[dict], list[str]]:
    """Extract from the known Scoutbook Plus unit endpoints:
    /organizations/v2/units/{guid}/youths (users with positions + rank
    history) joined with /units/{guid}/parents (parentInformation linked by
    youthUserId). These carry den, rank, and guardian contacts; the broader
    orgYouths list does not, and includes lapsed registrations.
    """
    youths_cap = parents_cap = None
    for cap in captures:
        url = cap["url"].lower()
        if re.search(r"/units/[^/]+/youths", url):
            youths_cap = cap
        elif re.search(r"/units/[^/]+/parents", url):
            parents_cap = cap
    if not youths_cap:
        return [], []

    users = (youths_cap["body"] or {}).get("users") or []
    parents_by_youth: dict = {}
    if parents_cap and isinstance(parents_cap["body"], list):
        for link in parents_cap["body"]:
            info = link.get("parentInformation") or {}
            entry = {
                "name": info.get("personFullName")
                        or f"{info.get('firstName', '')} {info.get('lastName', '')}".strip(),
                "relationship": "Parent/Guardian",  # this API doesn't say which
                "email": info.get("email") or "",
                "phone": info.get("mobilePhone") or info.get("homePhone") or "",
            }
            parents_by_youth.setdefault(link.get("youthUserId"), []).append(entry)

    scouts = []
    for y in users:
        if y.get("isAdult"):
            continue
        scouts.append({
            "first_name": y.get("firstName") or "",
            "last_name": y.get("lastName") or "",
            "bsa_id": str(y.get("memberId") or ""),
            "den": _den_from_positions(y.get("positions")),
            "rank": _rank_from_history(y),
            "member_type": y.get("memberType") or "Youth",
            "parents": parents_by_youth.get(y.get("userId"), []),
        })
    sources = [youths_cap["url"]] + ([parents_cap["url"]] if parents_cap else [])
    return scouts, sources


def extract_roster(captures: list[dict]) -> tuple[list[dict], list[str]]:
    """Extract the roster, preferring the known unit endpoints and falling
    back to a generic person-list heuristic (in case the API changes or a
    different unit type is in play).
    """
    scouts, sources = extract_unit_roster(captures)
    if scouts:
        log(f"Unit roster endpoints: {len(scouts)} youth (with dens/ranks/parents)")
        return scouts, sources
    candidates = []  # (score, source_url, people)
    for cap in captures:
        for path, people in find_person_lists(cap["body"]):
            url_l = cap["url"].lower()
            score = len(people)
            if any(h in url_l for h in ("roster", "youth", "members")):
                score += 100
            candidates.append((score, cap["url"], path, people))

    if not candidates:
        return [], []

    candidates.sort(key=lambda c: c[0], reverse=True)
    score, url, path, people = candidates[0]
    log(f"Best roster candidate: {len(people)} people from {url} at {path}")
    scouts = [normalize_person(p) for p in people]
    # Drop adults when the payload tells us who they are; a Cub Scout roster
    # question is about the youth. Keep everyone if we can't tell.
    youth = [s for s in scouts
             if not re.search(r"adult|leader|true", s["member_type"], re.I)]
    if youth and len(youth) < len(scouts):
        log(f"Filtered {len(scouts) - len(youth)} adult/leader entries; {len(youth)} youth remain.")
        scouts = youth
    sources = [url]
    return scouts, sources


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(scouts: list[dict], sources: list[str], data_dir: Path) -> None:
    roster = {
        "fetched_at": now_iso(),
        "source": "advancements.scouting.org",
        "source_urls": sources,
        "scout_count": len(scouts),
        "scouts": sorted(scouts, key=lambda s: (s["den"], s["last_name"], s["first_name"])),
    }
    (data_dir / "roster.json").write_text(json.dumps(roster, indent=2))

    with open(data_dir / "roster.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["first_name", "last_name", "bsa_id", "den", "rank", "parents"])
        for s in roster["scouts"]:
            def fmt(p):
                bits = [p["name"]]
                if p.get("email"):
                    bits.append(f"<{p['email']}>")
                if p.get("phone"):
                    bits.append(p["phone"])
                return " ".join(bits)
            parents = "; ".join(fmt(p) for p in s["parents"])
            w.writerow([s["first_name"], s["last_name"], s["bsa_id"],
                        s["den"], s["rank"], parents])

    log(f"Wrote {len(scouts)} scouts to {data_dir / 'roster.json'} and {data_dir / 'roster.csv'}")


def save_raw_captures(captures: list[dict], raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("*.json"):
        old.unlink()
    for i, cap in enumerate(captures):
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", cap["url"].split("//", 1)[-1])[:120]
        (raw_dir / f"{i:03d}_{slug}.json").write_text(
            json.dumps({"url": cap["url"], "body": cap["body"]}, indent=2)
        )
    log(f"Saved {len(captures)} raw API captures to {raw_dir}/")


# ---------------------------------------------------------------------------
# Browser fetch
# ---------------------------------------------------------------------------

def fetch_live(data_dir: Path, login_timeout: int) -> list[dict]:
    from playwright.sync_api import sync_playwright

    profile_dir = data_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    pending = []   # Response objects, bodies read on the main thread later
    captures = []  # {"url", "body"} dicts

    def on_response(resp):
        try:
            ct = (resp.headers.get("content-type") or "").lower()
        except Exception:
            return
        if "json" in ct and not any(h in resp.url.lower() for h in NOISE_URL_HINTS):
            log(f"captured JSON response: {resp.url}")
            pending.append(resp)

    def drain_pending():
        while pending:
            resp = pending.pop(0)
            try:
                body = resp.json()
            except Exception:
                continue
            captures.append({"url": resp.url, "body": body})

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

        page = context.pages[0] if context.pages else context.new_page()
        # Listen at the context level so responses from every tab/popup the
        # user ends up in are captured, not just the first page.
        context.on("response", on_response)

        log(f"Opening {ROSTER_URL} …")
        try:
            page.goto(ROSTER_URL, timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass  # SPA network chatter often never goes idle; the poll below decides

        print("\n" + "=" * 64, flush=True)
        print("A browser window is open — use THAT window (not your regular", flush=True)
        print("browser). If you see a login page: log in (solve the CAPTCHA", flush=True)
        print("if asked), then open the pack ROSTER page. The script detects", flush=True)
        print("the roster data automatically; nothing else to press.", flush=True)
        print("=" * 64 + "\n", flush=True)

        deadline = time.time() + login_timeout
        last_nudge = time.time()
        last_status = 0.0
        saved_count = 0
        scouts: list[dict] = []
        while time.time() < deadline:
            drain_pending()
            # Persist captures as we go so a crash/timeout still leaves
            # everything on disk for inspection.
            if len(captures) > saved_count:
                save_raw_captures(captures, data_dir / "raw")
                saved_count = len(captures)
            scouts, sources = extract_roster(captures)
            if scouts:
                break
            active = context.pages[-1] if context.pages else page
            if time.time() - last_status > 15:
                last_status = time.time()
                log(f"waiting… {len(captures)} JSON responses captured; "
                    f"current page: {active.url}")
            # If the user finished logging in but is parked somewhere other
            # than the roster, steer there occasionally. Never mid-login, and
            # never when they're already on the roster page (a reload would
            # cancel its in-flight API calls).
            if time.time() - last_nudge > 45:
                last_nudge = time.time()
                url_l = active.url.lower()
                if not any(w in url_l for w in ("login", "signin", "roster")):
                    try:
                        active.goto(ROSTER_URL, timeout=30_000)
                    except Exception:
                        pass
            time.sleep(3)

        drain_pending()
        context.close()

    save_raw_captures(captures, data_dir / "raw")
    return captures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=24,
                    help="reuse roster.json if newer than this (default 24)")
    ap.add_argument("--force", action="store_true",
                    help="fetch even if the cache is fresh")
    ap.add_argument("--login-timeout", type=int, default=300,
                    help="seconds to wait for login + roster data (default 300)")
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")),
                    help="where roster.json/roster.csv/raw/ live")
    ap.add_argument("--from-raw", action="store_true",
                    help="re-extract from the saved raw/ captures instead of "
                         "opening a browser (for iterating on the parser)")
    args = ap.parse_args()

    data_dir = args.data_dir.expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    roster_json = data_dir / "roster.json"

    if args.from_raw:
        captures = [json.loads(f.read_text())
                    for f in sorted((data_dir / "raw").glob("*.json"))]
        log(f"Loaded {len(captures)} raw captures from {data_dir / 'raw'}/")
    elif not args.force and cache_is_fresh(roster_json, args.max_age_hours):
        print(roster_json)
        return
    else:
        captures = fetch_live(data_dir, args.login_timeout)
    scouts, sources = extract_roster(captures)
    if not scouts:
        log("No roster-shaped JSON found in any captured API response.")
        log(f"Inspect the raw captures in {data_dir / 'raw'}/ to see what the "
            "site returned, then improve extract_roster()/normalize_person().")
        sys.exit(3)

    write_outputs(scouts, sources, data_dir)
    print(roster_json)


if __name__ == "__main__":
    main()
