#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.40"]
# ///
"""
fetch_advancement.py — fetch the user's saved "Full Pack Advancement Report"
(Scoutbook Report Builder custom report) from Scoutbook Plus.

How it works: custom reports live in the legacy Scoutbook report system
(reportspd.scouting.org), which requires a legacy Scoutbook SSO session in
addition to the my.scouting.org login. The script:

  1. opens the Scoutbook dashboard once to establish that SSO session
     (it piggybacks on the my.scouting.org login in the shared profile);
  2. loads advancements.scouting.org/reports and captures the
     SBLSharedReportsManagerAPI response, which lists every saved custom
     report with its title and run_uri;
  3. GETs the matching report's run_uri and parses the rendered HTML matrix
     (rows = ranks/requirements/adventures/awards, columns = scouts,
     cells = completion dates).

Usage:
    uv run fetch_advancement.py [--max-age-hours 24] [--force]
        [--report-title "Full Pack Advancement Report"] [--report-id N]
        [--login-timeout 300] [--from-raw]

Outputs (in --data-dir, default ~/.scouting-skills/):
    advancement.json       normalized per-scout advancement data
    advancement.csv        flat long-format CSV (one completion per row)
    raw-advancement/       report.html + reports_list.json (for debugging)

Exit codes:
    0  success (fresh fetch, or cache was recent enough)
    2  environment/login failure (browser missing, login never completed)
    3  report fetched (or --from-raw) but no report matrix was recognized
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SCOUTBOOK_SSO_URL = "https://scoutbook.scouting.org/mobile/dashboard/"
REPORTS_URL = "https://advancements.scouting.org/reports"
DEFAULT_REPORT_TITLE = "Full Pack Advancement Report"
# Last known report id, used only if the reports-list API can't be captured.
FALLBACK_REPORT_ID = "2171874"

RANK_HEADERS = ("Lion", "Bobcat", "Tiger", "Wolf", "Bear", "Webelos",
                "Arrow of Light")
VERSION_RE = re.compile(
    r"^(Lion|Bobcat|Tiger|Wolf|Bear|Webelos|Arrow of Light) v\d{4}$")
REQ_RE = re.compile(r"^Req\s*#\s*(.+)$", re.I)
# Rows in the report that are awards rather than rank adventures. Verified
# against every non-adventure row label in the current report output.
AWARD_RE = re.compile(
    r"award|medal|strip|emblem|knot|chip\b|supernova|messengers of peace|"
    r"faith in god|scout ranger|pala challenge|religious", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[fetch_advancement] {msg}", flush=True)


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


def fix_mojibake(html: str) -> str:
    """reportspd.scouting.org declares charset=iso-8859-1 but actually
    serves UTF-8 bytes; Playwright decodes per the declared charset,
    mangling every non-ASCII character (e.g. 'ó' -> 'Ã³'). Reverse it by
    round-tripping through the encoding the browser wrongly assumed."""
    try:
        return html.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return html


def parse_date(value: str) -> str:
    """'4/30/26' or '4/30/2026' -> '2026-04-30'; '' if unparseable."""
    s = str(value).strip()
    if not s:
        return ""
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# HTML matrix parsing
# ---------------------------------------------------------------------------

class _TableParser(HTMLParser):
    """Collect every <table> as a list of row lists (cell text only)."""

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


def report_matrix(html: str):
    """The report's biggest table, or None. Expected shape: row 0 = scout
    names ('' first), then label rows × scout columns."""
    parser = _TableParser()
    try:
        parser.feed(html)
    except Exception:
        return None
    best = None
    for rows in parser.tables:
        if len(rows) >= 20 and len(rows[0]) >= 3:
            if best is None or len(rows) > len(best):
                best = rows
    return best


def parse_matrix(rows: list[list[str]]) -> dict:
    """Turn the label-rows × scout-columns matrix into per-scout records."""
    names = rows[0][1:]
    ncols = len(names)

    def full_row(r):
        return r + [""] * (1 + ncols - len(r))

    scouts = []
    for name in names:
        parts = name.rsplit(" ", 1)
        first, last = parts if len(parts) == 2 else (name, "")
        scouts.append({
            "name": name, "first_name": first, "last_name": last,
            "bsa_id": "", "current_rank": "", "next_rank": "",
            "ranks": {}, "rank_requirements": {},
            "adventures": [], "awards": [],
        })

    catalog = {"ranks": [], "adventures": {}, "awards": []}
    section = ""            # current rank section
    req_parent = None       # ("version"|"award"|"adventure", label) for Req rows

    for row in rows[1:]:
        row = full_row(row)
        label, cells = row[0].strip(), row[1:1 + ncols]
        if not label:
            continue
        if label == "BSA Member #":
            for s, v in zip(scouts, cells):
                s["bsa_id"] = v.strip()
        elif label == "Current Rank":
            for s, v in zip(scouts, cells):
                s["current_rank"] = v.strip()
        elif label == "Next Rank":
            for s, v in zip(scouts, cells):
                s["next_rank"] = v.strip()
        elif label in RANK_HEADERS:
            section = label
            req_parent = None
            catalog["ranks"].append(label)
            catalog["adventures"].setdefault(label, [])
            for s, v in zip(scouts, cells):
                d = parse_date(v)
                if d:
                    s["ranks"][label] = d
        elif VERSION_RE.match(label):
            req_parent = ("version", label)
            for s, v in zip(scouts, cells):
                d = parse_date(v)
                if d:
                    s["rank_requirements"].setdefault(
                        label, {})["completed"] = d
        elif REQ_RE.match(label):
            req = REQ_RE.match(label).group(1).strip()
            if req_parent is None:
                continue
            kind, parent = req_parent
            for s, v in zip(scouts, cells):
                d = parse_date(v)
                if not d:
                    continue
                if kind == "version":
                    s["rank_requirements"].setdefault(
                        parent, {}).setdefault("reqs", {})[req] = d
                elif kind == "award":
                    for a in s["awards"]:
                        if a["name"] == parent:
                            a.setdefault("requirements", {})[req] = d
                            break
                    else:
                        s["awards"].append({"name": parent, "completed": "",
                                            "requirements": {req: d}})
                else:  # adventure
                    for a in s["adventures"]:
                        if a["name"] == parent:
                            a.setdefault("requirements", {})[req] = d
                            break
        elif AWARD_RE.search(label):
            req_parent = ("award", label)
            if label not in catalog["awards"]:
                catalog["awards"].append(label)
            for s, v in zip(scouts, cells):
                d = parse_date(v)
                if d:
                    s["awards"].append({"name": label, "completed": d})
        else:
            req_parent = ("adventure", label)
            catalog["adventures"].setdefault(section or "?", [])
            if label not in catalog["adventures"][section or "?"]:
                catalog["adventures"][section or "?"].append(label)
            for s, v in zip(scouts, cells):
                d = parse_date(v)
                if d:
                    s["adventures"].append(
                        {"name": label, "rank": section, "completed": d})

    return {"scouts": scouts, "catalog": catalog}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(parsed: dict, meta: dict, data_dir: Path) -> None:
    report = {
        "fetched_at": now_iso(),
        "source": "reportspd.scouting.org (Scoutbook Report Builder)",
        **meta,
        "scout_count": len(parsed["scouts"]),
        "catalog": parsed["catalog"],
        "scouts": parsed["scouts"],
    }
    (data_dir / "advancement.json").write_text(json.dumps(report, indent=2))

    with open(data_dir / "advancement.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scout", "bsa_id", "kind", "rank", "item", "completed"])
        for s in parsed["scouts"]:
            for rank, d in s["ranks"].items():
                w.writerow([s["name"], s["bsa_id"], "rank", rank, rank, d])
            for a in s["adventures"]:
                w.writerow([s["name"], s["bsa_id"], "adventure",
                            a["rank"], a["name"], a["completed"]])
            for a in s["awards"]:
                w.writerow([s["name"], s["bsa_id"], "award", "",
                            a["name"], a.get("completed", "")])
    log(f"Wrote {len(parsed['scouts'])} scouts to "
        f"{data_dir / 'advancement.json'} and {data_dir / 'advancement.csv'}")


# ---------------------------------------------------------------------------
# Browser fetch
# ---------------------------------------------------------------------------

def wait_out_login(page, login_timeout: int) -> bool:
    """If we landed on a login page, wait for the user to finish logging in."""
    if not any(w in page.url.lower() for w in ("login", "signin")):
        return True
    print("\n" + "=" * 64, flush=True)
    print("A browser window is open showing a login page. Please log in", flush=True)
    print("there (solve the CAPTCHA if asked). The fetch resumes on its", flush=True)
    print("own once you're in.", flush=True)
    print("=" * 64 + "\n", flush=True)
    deadline = time.time() + login_timeout
    while time.time() < deadline:
        if not any(w in page.url.lower() for w in ("login", "signin")):
            time.sleep(3)  # let the post-login redirect settle
            return True
        time.sleep(2)
    return False


def fetch_live(data_dir: Path, report_title: str, report_id: str | None,
               login_timeout: int) -> str:
    """Return the report's rendered HTML (saved to raw-advancement/ too)."""
    from playwright.sync_api import sync_playwright

    raw_dir = data_dir / "raw-advancement"
    raw_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = data_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    reports_list: list[dict] = []

    def on_response(resp):
        if "sblsharedreportsmanagerapi" not in resp.url.lower():
            return
        try:
            body = resp.json()
        except Exception:
            return
        if isinstance(body, dict) and body.get("reports"):
            reports_list.clear()
            reports_list.extend(body["reports"])
            (raw_dir / "reports_list.json").write_text(
                json.dumps(body, indent=2))
            log(f"captured custom-reports list ({len(body['reports'])} reports)")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,  # login may need a human (CAPTCHA)
                viewport={"width": 1400, "height": 950},
            )
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                log("Chromium is not installed for Playwright. Run:")
                log("  uv run --with playwright playwright install chromium")
                sys.exit(2)
            raise

        context.on("response", on_response)
        page = context.pages[0] if context.pages else context.new_page()

        # Step 1: legacy Scoutbook SSO. reportspd.scouting.org rejects the
        # report request ("Single Sign On (SSO) is required") unless the
        # legacy Scoutbook session exists; visiting the Scoutbook dashboard
        # establishes it off the my.scouting.org login.
        log(f"Establishing Scoutbook SSO via {SCOUTBOOK_SSO_URL} …")
        try:
            page.goto(SCOUTBOOK_SSO_URL, timeout=90_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        if not wait_out_login(page, login_timeout):
            log("Login never completed within the timeout.")
            context.close()
            sys.exit(2)

        # Step 2: the Reports page loads the custom-reports list on its own;
        # capture it to resolve the report title -> run_uri.
        run_uri = None
        if not report_id:
            log(f"Opening {REPORTS_URL} to find {report_title!r} …")
            try:
                page.goto(REPORTS_URL, timeout=60_000)
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            deadline = time.time() + 45
            while time.time() < deadline and not reports_list:
                time.sleep(2)
            for r in reports_list:
                if str(r.get("report_title", "")).strip().lower() \
                        == report_title.strip().lower():
                    run_uri = r.get("run_uri")
                    report_id = str(r.get("report_id", ""))
                    log(f"Found report id {report_id}: {r.get('report_title')}")
                    break
            if not run_uri and reports_list:
                titles = [r.get("report_title") for r in reports_list]
                log(f"No report titled {report_title!r}; available: {titles}")
        if not run_uri:
            report_id = report_id or FALLBACK_REPORT_ID
            log(f"Falling back to report id {report_id}.")
            run_uri = ("https://reportspd.scouting.org/advancements/webscript/"
                       "output/sbreports.aspx?method=ExecuteContainerAPI"
                       "&container=SBReportBuilderOutput&api=ReportPresentation"
                       f"&ReportID={report_id}")

        # Step 3: run the report and wait for the matrix to render.
        html = ""
        for attempt in (1, 2):
            log(f"Running report (attempt {attempt}): {run_uri}")
            try:
                page.goto(run_uri, timeout=120_000)
            except Exception as exc:
                log(f"goto run_uri: {exc}")
            deadline = time.time() + 180
            while time.time() < deadline:
                try:
                    html = fix_mojibake(page.content())
                except Exception:
                    html = ""
                if report_matrix(html):
                    break
                if "aa-msg--error" in html:
                    break
                time.sleep(3)
            if report_matrix(html):
                break
            if "Single Sign On" in html and attempt == 1:
                # SSO session still missing — refresh it once and retry.
                log("SSO error from reportspd; refreshing Scoutbook session …")
                try:
                    page.goto(SCOUTBOOK_SSO_URL, timeout=90_000)
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
                wait_out_login(page, login_timeout)
            else:
                break

        (raw_dir / "report.html").write_text(html)
        log(f"Saved rendered report HTML ({len(html)} bytes) to "
            f"{raw_dir / 'report.html'}")
        context.close()

    # Stash the resolution for meta/fallback on later runs.
    (raw_dir / "report_meta.json").write_text(json.dumps(
        {"report_title": report_title, "report_id": report_id,
         "run_uri": run_uri}, indent=2))
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=24,
                    help="reuse advancement.json if newer than this (default 24)")
    ap.add_argument("--force", action="store_true",
                    help="fetch even if the cache is fresh")
    ap.add_argument("--report-title", default=DEFAULT_REPORT_TITLE,
                    help=f"saved report to run (default {DEFAULT_REPORT_TITLE!r})")
    ap.add_argument("--report-id", default=None,
                    help="skip title lookup and run this report id directly")
    ap.add_argument("--login-timeout", type=int, default=300,
                    help="seconds to wait for a manual login (default 300)")
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")),
                    help="where advancement.json/.csv/raw-advancement/ live")
    ap.add_argument("--from-raw", action="store_true",
                    help="re-parse raw-advancement/report.html instead of "
                         "opening a browser (for iterating on the parser)")
    args = ap.parse_args()

    data_dir = args.data_dir.expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    report_json = data_dir / "advancement.json"
    raw_dir = data_dir / "raw-advancement"

    if args.from_raw:
        html = (raw_dir / "report.html").read_text()
        log(f"Loaded {len(html)} bytes from {raw_dir / 'report.html'}")
    elif not args.force and cache_is_fresh(report_json, args.max_age_hours):
        print(report_json)
        return
    else:
        html = fetch_live(data_dir, args.report_title, args.report_id,
                          args.login_timeout)

    matrix = report_matrix(html)
    if not matrix:
        log("No report matrix found in the rendered page.")
        log(f"Inspect {raw_dir / 'report.html'} to see what came back "
            "(an SSO/login error page is the usual cause), then rerun; "
            "use --from-raw to iterate on the parser without a browser.")
        sys.exit(3)

    parsed = parse_matrix(matrix)
    meta = {}
    meta_file = raw_dir / "report_meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
        except json.JSONDecodeError:
            pass
    meta.setdefault("report_title", args.report_title)
    write_outputs(parsed, meta, data_dir)
    print(report_json)


if __name__ == "__main__":
    main()
