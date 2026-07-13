#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["icalendar>=5.0", "recurring-ical-events>=2.1"]
# ///
"""
fetch_events.py — fetch Pack 97 events from the pack's public Google
Calendar ICS feed.

Strategy: Google serves a live ICS export of the pack calendar at a public
URL (the same feed phone calendar apps subscribe to), so no login is needed.
The feed contains the calendar's full history plus recurring events
(RRULE/EXDATE), so this script expands recurrences with recurring-ical-events
and filters to a date window instead of dumping everything.

The raw ICS is cached so repeat questions don't refetch; the window is
re-expanded locally on every run, so window flags work even on a cache hit.

Usage:
    uv run fetch_events.py [--max-age-hours 6] [--force]
                           [--days-ahead 180] [--days-back 0]

Outputs (in --data-dir, default ~/.scouting-skills/):
    calendar.ics   raw feed as last fetched
    events.json    expanded, window-filtered events with fetched_at timestamp

Exit codes:
    0  success (fresh fetch, or cache was recent enough)
    2  network failure and no usable cached ICS
    3  feed downloaded but could not be parsed as a calendar
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ICS_URL = (
    "https://calendar.google.com/calendar/ical/"
    "holyfamilypack97%40gmail.com/public/basic.ics"
)
PACK_TZ = ZoneInfo("America/Chicago")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[fetch_events] {msg}", flush=True)


def html_to_text(s: str) -> str:
    """Google puts HTML in DESCRIPTION; reduce it to readable text, keeping
    link targets (they're often the only place a signup URL lives)."""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: m.group(1) if m.group(1).strip("/") in m.group(2)
               else f"{m.group(2)} ({m.group(1)})",
               s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def fetch_ics(ics_path: Path, max_age_hours: float, force: bool) -> bool:
    """Ensure a current copy of the feed at ics_path. Returns True if the
    copy is usable, False if there's neither a download nor a cache."""
    if ics_path.exists() and not force:
        age_h = (datetime.now(timezone.utc).timestamp()
                 - ics_path.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            log(f"Cached feed is {age_h:.1f}h old (limit {max_age_hours:.1f}h)"
                " — using cache.")
            return True
    try:
        req = urllib.request.Request(
            ICS_URL, headers={"User-Agent": "pack-calendar-skill/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        ics_path.write_bytes(data)
        log(f"Fetched feed ({len(data)} bytes).")
        return True
    except Exception as exc:  # noqa: BLE001 — any network failure falls back
        if ics_path.exists():
            log(f"Fetch failed ({exc}); falling back to cached feed.")
            return True
        log(f"Fetch failed ({exc}) and no cached feed exists.")
        return False


def normalize_event(ev) -> dict:
    """Flatten one expanded VEVENT occurrence to plain JSON types, with
    datetimes in pack-local time (America/Chicago)."""
    start = ev["DTSTART"].dt
    end = ev.get("DTEND", ev["DTSTART"]).dt
    all_day = not isinstance(start, datetime)
    if all_day:
        # For all-day events DTEND is exclusive; report the last real day.
        end_incl = end - timedelta(days=1) if isinstance(end, date) else end
        start_s, end_s = start.isoformat(), max(start, end_incl).isoformat()
    else:
        start_s = start.astimezone(PACK_TZ).isoformat()
        end_s = end.astimezone(PACK_TZ).isoformat()
    desc = html_to_text(str(ev.get("DESCRIPTION", "")))
    return {
        "summary": str(ev.get("SUMMARY", "(no title)")),
        "start": start_s,
        "end": end_s,
        "all_day": all_day,
        "location": str(ev.get("LOCATION", "")) or None,
        "description": desc or None,
        "meet_link": str(ev.get("X-GOOGLE-CONFERENCE", "")) or None,
        "status": str(ev.get("STATUS", "CONFIRMED")),
        "recurring": "RRULE" in ev or ev.get("RECURRENCE-ID") is not None,
        "uid": str(ev.get("UID", "")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=6)
    ap.add_argument("--force", action="store_true",
                    help="refetch even if the cache is fresh")
    ap.add_argument("--days-ahead", type=int, default=180)
    ap.add_argument("--days-back", type=int, default=0)
    ap.add_argument("--data-dir", type=Path,
                    default=Path.home() / ".scouting-skills")
    args = ap.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    ics_path = args.data_dir / "calendar.ics"
    out_path = args.data_dir / "events.json"

    if not fetch_ics(ics_path, args.max_age_hours, args.force):
        return 2

    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(ics_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        log(f"Could not parse feed as ICS: {exc}")
        log(f"Raw feed kept at {ics_path} — inspect it.")
        return 3

    today = datetime.now(PACK_TZ).date()
    win_start = today - timedelta(days=args.days_back)
    win_end = today + timedelta(days=args.days_ahead)
    occurrences = recurring_ical_events.of(cal).between(win_start, win_end)

    events = sorted(
        (normalize_event(ev) for ev in occurrences),
        key=lambda e: (e["start"], e["summary"]),
    )
    # Cancelled events still appear in the feed; don't show them as plans.
    events = [e for e in events if e["status"] != "CANCELLED"]

    payload = {
        "fetched_at": datetime.fromtimestamp(
            ics_path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds"),
        "source": ICS_URL,
        "timezone": str(PACK_TZ),
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "event_count": len(events),
        "events": events,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log(f"{len(events)} events between {win_start} and {win_end}.")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
