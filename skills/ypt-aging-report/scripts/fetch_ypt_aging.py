#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.40"]
# ///
"""
fetch_ypt_aging.py — fetch the Safeguarding Youth Training (YPT) Aging
Report from Scoutbook Plus (advancements.scouting.org/reports).

Strategy: same as fetch_roster.py — the site is a React SPA whose DOM is
unscrapeable (hashed class names), so this script opens a real browser,
navigates to the Reports page, tries to open the "Safeguarding Youth
Training Aging Report" by its visible link text, and captures the report
data (JSON or CSV) off the wire. The persistent browser profile in the data
dir keeps the login session, so the manual CAPTCHA login is only needed
when the session expires.

Usage:
    uv run fetch_ypt_aging.py [--max-age-hours 24] [--force] [--login-timeout 300]

Outputs (in --data-dir, default ~/.scouting-skills/):
    ypt_aging.json   normalized report with fetched_at timestamp
    ypt_aging.csv    flat CSV export
    raw-ypt/         every JSON/CSV response captured (for debugging)

Exit codes:
    0  success (fresh fetch, or cache was recent enough)
    2  environment/login failure (browser missing, login never completed)
    3  fetched pages but could not find training-report-shaped data
       (raw-ypt/ saved — inspect it and improve the parser)
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = "https://advancements.scouting.org"
REPORTS_URL = f"{BASE_URL}/reports"

# Visible link text of the report on the Reports page (BSA renamed YPT to
# "Safeguarding Youth Training"; match either era's wording).
REPORT_LINK_PATTERNS = (
    re.compile(r"safeguarding\s+youth\s+training.*aging", re.I),
    re.compile(r"ypt\s*aging", re.I),
    re.compile(r"youth\s+protection\s+training.*aging", re.I),
)

EXPIRING_SOON_DAYS = 60

# Capture every JSON/CSV response except known noise. The advancements
# catalogs (awards, merit badges, …) are item lists whose rows have name +
# expiredDate and false-positive as training rows.
NOISE_URL_HINTS = (
    "sentry", "translations", "manifest", "analytics", "googletagmanager",
    "doubleclick", "hotjar", "launchdarkly",
    "/advancements/awards", "/advancements/meritbadges",
    "/advancements/adventures", "/advancements/sselectives",
    "/advancements/ranks",
)

# The Reports page menu (GetReportListByOrg) hands out each report's URI on
# these legacy hosts; the report itself usually renders as HTML there, so
# capture everything they serve, not just JSON/CSV.
REPORT_HOST_HINTS = ("mystrpt", "reportspd.scouting.org", "webreport")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[fetch_ypt_aging] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cache_is_fresh(report_json: Path, max_age_hours: float) -> bool:
    if not report_json.exists():
        return False
    try:
        data = json.loads(report_json.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return False
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
    if age_hours <= max_age_hours:
        log(f"Cached report is {age_hours:.1f}h old (limit {max_age_hours}h) — using cache.")
        return True
    return False


# ---------------------------------------------------------------------------
# Report extraction from captured responses
# ---------------------------------------------------------------------------

def _norm_keys(d: dict) -> dict:
    return {re.sub(r"[_\s]", "", k).lower(): v for k, v in d.items()}


def _get_first(d: dict, *candidates) -> str:
    normalized = _norm_keys(d)
    for cand in candidates:
        v = normalized.get(cand)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def parse_date(value: str):
    """Parse the date formats this API is known to emit; None if unparseable."""
    s = str(value).strip()
    if not s:
        return None
    s = s.split("T")[0].rstrip("Zz")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _date_by_pattern(d: dict, pattern: str):
    """First parseable date among values whose key matches the pattern."""
    for k, v in _norm_keys(d).items():
        if re.search(pattern, k) and v not in (None, ""):
            parsed = parse_date(v)
            if parsed:
                return parsed
    return None


# Deliberately excludes bare "name" — catalog rows (awards, badges) have a
# name too; a training row must identify a *person*.
PERSON_KEYS = {"firstname", "lastname", "fullname", "membername",
               "personfullname", "adultname", "leadername", "memberid",
               "bsamemberid", "bsaid"}
TRAINING_KEY_HINT = re.compile(r"ypt|training|course|safeguard|expir")


def _looks_like_training_row(item) -> bool:
    if not isinstance(item, dict):
        return False
    normalized = _norm_keys(item)
    if not set(normalized) & PERSON_KEYS:
        return False
    # …and carry a training signal: a ypt/safeguard/course key, or an
    # expiration/completion key holding an actual date.
    for k, v in normalized.items():
        if re.search(r"ypt|safeguard|course|trainingname|trainingcode", k):
            return True
        if re.search(r"expir|complet", k) and v not in (None, "") and parse_date(v):
            return True
    return False


def find_training_lists(obj, path="$"):
    """Recursively yield (path, list) for every list of training-row dicts."""
    if isinstance(obj, list):
        rows = [x for x in obj if _looks_like_training_row(x)]
        if rows and len(rows) >= max(1, len(obj) // 2):
            yield path, rows
        else:
            for i, item in enumerate(obj):
                yield from find_training_lists(item, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from find_training_lists(v, f"{path}.{k}")


def parse_csv_capture(text: str) -> list[dict]:
    """Reports may come down as CSV instead of JSON; turn a plausible one
    into row dicts so normalize_row() can handle both the same way."""
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return []
    if len(rows) < 2:
        return []
    header_norm = [re.sub(r"[_\s]", "", h).lower() for h in rows[0]]
    has_name = any("name" in h for h in header_norm)
    has_training = any(TRAINING_KEY_HINT.search(h) for h in header_norm)
    if not (has_name and has_training):
        return []
    return [dict(zip(rows[0], r)) for r in rows[1:] if any(c.strip() for c in r)]


class _TableParser(HTMLParser):
    """Collect every <table> in an HTML document as a list of row lists
    (cell text only). Uses a stack so nested tables don't corrupt parents."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._cells = None
        self._text = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._text = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._text is not None:
            self._cells.append(" ".join("".join(self._text).split()))
            self._text = None
        elif tag == "tr" and self._cells is not None:
            if self._cells and self._stack:
                self._stack[-1].append(self._cells)
            self._cells = None
        elif tag == "table" and self._stack:
            rows = self._stack.pop()
            if rows:
                self.tables.append(rows)

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


def parse_html_capture(text: str) -> list[dict]:
    """Legacy my.scouting.org web reports render as HTML tables; turn the
    most plausible one into row dicts (first row as header)."""
    parser = _TableParser()
    try:
        parser.feed(text)
    except Exception:
        return []
    best: list[dict] = []
    for rows in parser.tables:
        if len(rows) < 2:
            continue
        header = rows[0]
        header_norm = [re.sub(r"[_\s]", "", h).lower() for h in header]
        if not (any("name" in h for h in header_norm)
                and any(TRAINING_KEY_HINT.search(h) for h in header_norm)):
            continue
        dicts = [dict(zip(header, r)) for r in rows[1:]
                 if any(c.strip() for c in r)]
        if len(dicts) > len(best):
            best = dicts
    return best


def normalize_row(r: dict) -> dict:
    first = _get_first(r, "firstname", "givenname")
    last = _get_first(r, "lastname", "surname", "familyname")
    if not (first or last):
        full = _get_first(r, "fullname", "personfullname", "membername",
                          "adultname", "leadername", "name")
        parts = full.rsplit(" ", 1) if full else []
        first, last = (parts + ["", ""])[:2] if len(parts) == 2 else (full, "")

    completed = _date_by_pattern(r, r"complet|trainingdate|dateearned|effective")
    expires = _date_by_pattern(r, r"expir")

    days = (expires - date.today()).days if expires else None
    if days is None:
        status = "unknown"
    elif days < 0:
        status = "expired"
    elif days <= EXPIRING_SOON_DAYS:
        status = "expiring_soon"
    else:
        status = "current"

    return {
        "first_name": first,
        "last_name": last,
        "bsa_id": _get_first(r, "memberid", "bsamemberid", "bsaid",
                             "membernumber", "userid"),
        "position": _get_first(r, "position", "positionname", "registeredposition",
                               "role", "positiondescription"),
        "course": _get_first(r, "coursename", "trainingname", "coursecode",
                             "course", "training"),
        "completed": completed.isoformat() if completed else "",
        "expires": expires.isoformat() if expires else "",
        "days_until_expiration": days,
        "status": status,
    }


def extract_org_adults(captures: list[dict]) -> tuple[list[dict], list[str]]:
    """Extract from the known /organizations/v2/{guid}/orgAdults endpoint,
    which the SPA loads on the Reports page: {"organizationInfo": …,
    "members": [{firstName, lastName, memberId, position, yptCompletedDate,
    yptExpiredDate, yptStatus}, …]} — exactly the aging report's content.
    One row per position, so adults are deduped by memberId with positions
    merged."""
    for cap in captures:
        if not re.search(r"/organizations/v2/[^/]+/orgadults", cap["url"], re.I):
            continue
        body = cap["body"]
        members = (body or {}).get("members") if isinstance(body, dict) else None
        if not members:
            continue
        by_member: dict = {}
        for r in members:
            mid = str(r.get("memberId") or r.get("personGuid") or "")
            adult = by_member.get(mid)
            if adult is None:
                adult = normalize_row(r)
                adult["course"] = adult["course"] or "Safeguarding Youth Training (YPT)"
                adult["api_status"] = str(r.get("yptStatus") or "")
                adult["positions"] = []
                by_member[mid] = adult
            pos = str(r.get("position") or "").strip()
            if pos and pos not in adult["positions"]:
                adult["positions"].append(pos)
        adults = []
        for adult in by_member.values():
            adult["position"] = "; ".join(adult.pop("positions"))
            adults.append(adult)
        return adults, [cap["url"]]
    return [], []


def extract_report(captures: list[dict]) -> tuple[list[dict], list[str]]:
    """Extract the report, preferring the known orgAdults endpoint and
    falling back to a generic training-row heuristic over every captured
    JSON/CSV/HTML response (in case the API changes)."""
    adults, sources = extract_org_adults(captures)
    if adults:
        log(f"orgAdults endpoint: {len(adults)} adults (deduped from positions)")
        return adults, sources
    return _extract_report_generic(captures)


def _extract_report_generic(captures: list[dict]) -> tuple[list[dict], list[str]]:
    """Find the training report in the captures: score every candidate list
    (JSON heuristic or parsed CSV/HTML) and take the best one."""
    candidates = []  # (score, url, rows)
    for cap in captures:
        url_l = cap["url"].lower()
        url_bonus = 0
        if any(h in url_l for h in ("ypt", "aging", "safeguard", "training")):
            url_bonus += 100
        if "report" in url_l:
            url_bonus += 50

        if any(h in url_l for h in REPORT_HOST_HINTS):
            url_bonus += 100

        if isinstance(cap["body"], str):
            rows = parse_csv_capture(cap["body"]) or parse_html_capture(cap["body"])
            if rows:
                candidates.append((len(rows) + url_bonus, cap["url"], rows))
        else:
            for path, rows in find_training_lists(cap["body"]):
                candidates.append((len(rows) + url_bonus, cap["url"], rows))

    if not candidates:
        return [], []
    candidates.sort(key=lambda c: c[0], reverse=True)
    score, url, rows = candidates[0]
    log(f"Best report candidate: {len(rows)} rows from {url}")
    adults = [normalize_row(r) for r in rows]
    adults = [a for a in adults if a["first_name"] or a["last_name"]]
    return adults, [url]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(adults: list[dict], sources: list[str], data_dir: Path) -> None:
    # Soonest-expiring first — that's the point of an aging report.
    adults = sorted(adults, key=lambda a: (a["expires"] or "9999-12-31",
                                           a["last_name"], a["first_name"]))
    report = {
        "fetched_at": now_iso(),
        "source": "advancements.scouting.org",
        "source_urls": sources,
        "adult_count": len(adults),
        "expiring_soon_days": EXPIRING_SOON_DAYS,
        "adults": adults,
    }
    (data_dir / "ypt_aging.json").write_text(json.dumps(report, indent=2))

    with open(data_dir / "ypt_aging.csv", "w", newline="") as f:
        w = csv.writer(f)
        fields = ["first_name", "last_name", "bsa_id", "position", "course",
                  "completed", "expires", "days_until_expiration", "status",
                  "api_status"]
        w.writerow(fields)
        for a in adults:
            w.writerow([a.get(k, "") for k in fields])

    log(f"Wrote {len(adults)} adults to {data_dir / 'ypt_aging.json'} "
        f"and {data_dir / 'ypt_aging.csv'}")


def save_raw_captures(captures: list[dict], raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in list(raw_dir.glob("*.json")) + list(raw_dir.glob("*.txt")):
        old.unlink()
    for i, cap in enumerate(captures):
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", cap["url"].split("//", 1)[-1])[:120]
        if isinstance(cap["body"], str):
            (raw_dir / f"{i:03d}_{slug}.txt").write_text(cap["body"])
        else:
            (raw_dir / f"{i:03d}_{slug}.json").write_text(
                json.dumps({"url": cap["url"], "body": cap["body"]}, indent=2)
            )
    log(f"Saved {len(captures)} raw captures to {raw_dir}/")


# ---------------------------------------------------------------------------
# Browser fetch
# ---------------------------------------------------------------------------

def report_uri_from_menu(captures: list[dict]):
    """The Reports page loads its menu from a GetReportListByOrg API whose
    entries carry each report's direct URI; find the YPT aging report's.
    (Known shape: {"Reports": [{"ReportName", "ReportCode": "YPTAgingReport",
    "ReportURI": "https://my.scouting.org/mystrpt/webreport/…"}]})"""
    for cap in captures:
        if "getreportlistbyorg" not in cap["url"].lower():
            continue
        body = cap["body"]
        reports = body.get("Reports") if isinstance(body, dict) else None
        for r in reports or []:
            if not isinstance(r, dict):
                continue
            name = str(r.get("ReportName", ""))
            code = str(r.get("ReportCode", ""))
            if code == "YPTAgingReport" or any(p.search(name)
                                               for p in REPORT_LINK_PATTERNS):
                return r.get("ReportURI")
    return None


def try_open_report(page) -> bool:
    """Click the report by its visible text. Text selectors survive the
    site's hashed class names; if the wording changes this just no-ops and
    the user clicks it themselves."""
    for pattern in REPORT_LINK_PATTERNS:
        try:
            loc = page.get_by_text(pattern).first
            if loc.count():
                loc.click(timeout=5_000)
                log(f"Clicked report link matching {pattern.pattern!r}")
                return True
        except Exception:
            continue
    return False


def fetch_live(data_dir: Path, login_timeout: int) -> list[dict]:
    from playwright.sync_api import sync_playwright

    profile_dir = data_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    pending = []   # Response objects, bodies read on the main thread later
    captures = []  # {"url", "body"} dicts; body is parsed JSON or CSV text

    def on_response(resp):
        try:
            ct = (resp.headers.get("content-type") or "").lower()
        except Exception:
            return
        url_l = resp.url.lower()
        if any(h in url_l for h in NOISE_URL_HINTS):
            return
        if ("json" in ct or "csv" in ct or url_l.endswith(".csv")
                or any(h in url_l for h in REPORT_HOST_HINTS)):
            log(f"captured response: {resp.url}")
            pending.append(resp)

    def drain_pending():
        while pending:
            resp = pending.pop(0)
            try:
                body = resp.json()
            except Exception:
                try:
                    body = resp.text()
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
        # report opens are captured, not just the first page.
        context.on("response", on_response)

        log(f"Opening {REPORTS_URL} …")
        try:
            page.goto(REPORTS_URL, timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass  # SPA network chatter often never goes idle; the poll below decides

        print("\n" + "=" * 64, flush=True)
        print("A browser window is open — use THAT window (not your regular", flush=True)
        print("browser). If you see a login page: log in (solve the CAPTCHA", flush=True)
        print("if asked). Then, on the Reports page, open the 'Safeguarding", flush=True)
        print("Youth Training Aging Report' (the script also tries to click", flush=True)
        print("it for you). The report data is detected automatically.", flush=True)
        print("=" * 64 + "\n", flush=True)

        deadline = time.time() + login_timeout
        last_nudge = time.time()
        last_click = 0.0
        last_status = 0.0
        saved_count = 0
        report_opened = False
        adults: list[dict] = []
        while time.time() < deadline:
            drain_pending()
            # Persist captures as we go so a crash/timeout still leaves
            # everything on disk for inspection.
            if len(captures) > saved_count:
                save_raw_captures(captures, data_dir / "raw-ypt")
                saved_count = len(captures)
            adults, sources = extract_report(captures)
            if adults:
                break
            active = context.pages[-1] if context.pages else page
            url_l = active.url.lower()
            if time.time() - last_status > 15:
                last_status = time.time()
                log(f"waiting… {len(captures)} responses captured; "
                    f"current page: {active.url}")
            # Prefer navigating straight to the report URI once the Reports
            # menu API has told us what it is; fall back to clicking the
            # link by text. Never mid-login.
            if not report_opened:
                uri = report_uri_from_menu(captures)
                if uri:
                    log(f"Opening report URI from Reports menu: {uri}")
                    try:
                        tab = context.new_page()
                        tab.goto(uri, timeout=60_000)
                        report_opened = True
                    except Exception as exc:
                        log(f"could not open report URI: {exc}")
                elif "report" in url_l and time.time() - last_click > 30:
                    last_click = time.time()
                    if try_open_report(active):
                        report_opened = True
            # Parked somewhere that is neither login nor reports: steer back.
            if time.time() - last_nudge > 45:
                last_nudge = time.time()
                if not any(w in url_l for w in ("login", "signin", "report")):
                    try:
                        active.goto(REPORTS_URL, timeout=30_000)
                    except Exception:
                        pass
            time.sleep(3)

        drain_pending()
        context.close()

    save_raw_captures(captures, data_dir / "raw-ypt")
    return captures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=24,
                    help="reuse ypt_aging.json if newer than this (default 24)")
    ap.add_argument("--force", action="store_true",
                    help="fetch even if the cache is fresh")
    ap.add_argument("--login-timeout", type=int, default=300,
                    help="seconds to wait for login + report data (default 300)")
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")),
                    help="where ypt_aging.json/ypt_aging.csv/raw-ypt/ live")
    ap.add_argument("--from-raw", action="store_true",
                    help="re-extract from the saved raw-ypt/ captures instead "
                         "of opening a browser (for iterating on the parser)")
    args = ap.parse_args()

    data_dir = args.data_dir.expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    report_json = data_dir / "ypt_aging.json"

    if args.from_raw:
        captures = []
        raw_dir = data_dir / "raw-ypt"
        for f in sorted(raw_dir.glob("*")):
            if f.suffix == ".json":
                saved = json.loads(f.read_text())
                captures.append({"url": saved["url"], "body": saved["body"]})
            elif f.suffix == ".txt":
                captures.append({"url": f.name, "body": f.read_text()})
        log(f"Loaded {len(captures)} raw captures from {raw_dir}/")
    elif not args.force and cache_is_fresh(report_json, args.max_age_hours):
        print(report_json)
        return
    else:
        captures = fetch_live(data_dir, args.login_timeout)
    adults, sources = extract_report(captures)
    if not adults:
        log("No training-report-shaped data found in any captured response.")
        log(f"Inspect the raw captures in {data_dir / 'raw-ypt'}/ to see what "
            "the site returned, then improve extract_report()/normalize_row().")
        sys.exit(3)

    write_outputs(adults, sources, data_dir)
    print(report_json)


if __name__ == "__main__":
    main()
