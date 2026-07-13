---
name: pack-calendar
description: Fetch and report Cub Scout Pack 97 events from the pack's public Google Calendar ICS feed. Use this whenever the user asks about pack events, meetings, campouts, deadlines, or schedules — "what's coming up", "when is the next pack meeting / committee meeting / campout", "what's on the calendar in October", "do we have anything this weekend", "when does popcorn selling start" — even if they don't say "calendar". Also use it to cross-check dates when planning pack communications or events.
---

# Pack Calendar

Retrieves upcoming Pack 97 events from the pack's public Google Calendar
ICS feed and answers schedule questions from it.

## How it works

`scripts/fetch_events.py` downloads the calendar's public ICS export — the
same live feed that phone calendar subscriptions use, so no login is needed
and newly added events appear on the next fetch (Google caches the public
feed on their side, so very recent edits can lag by a few hours). The feed
contains the pack's full history back to 2023 plus recurring events, so the
script expands recurrences (RRULE/EXDATE) and filters to a date window
rather than dumping everything.

The raw feed is cached in `~/.scouting-skills/calendar.ics` for 6 hours by
default; the window filter is applied locally on every run, so window flags
work even on a cache hit.

## Workflow

1. Run the fetch (from this skill's directory):

   ```bash
   uv run scripts/fetch_events.py --days-ahead 180
   ```

   - Pick the window from the question: "this weekend" needs only
     `--days-ahead 7`; "when is the Pinewood Derby" may need 365. For past
     events ("when was the fall campout?") add `--days-back N`.
   - The user wants guaranteed-fresh data? Add `--force`.

2. Read `~/.scouting-skills/events.json`. Shape:

   ```json
   {
     "fetched_at": "2026-07-07T12:00:00+00:00",
     "timezone": "America/Chicago",
     "window": {"start": "2026-07-07", "end": "2027-01-03"},
     "event_count": 24,
     "events": [
       {"summary": "PACK MEETING", "start": "2026-09-20T15:00:00-05:00",
        "end": "2026-09-20T16:00:00-05:00", "all_day": false,
        "location": "…", "description": "…", "meet_link": "…",
        "status": "CONFIRMED", "recurring": true, "uid": "…"}
     ]
   }
   ```

   Times are already in pack-local time (America/Chicago). For all-day
   events `start`/`end` are dates and `end` is the last day (inclusive),
   not the ICS exclusive end.

3. Answer the user's actual question from the data. When they asked for the
   schedule itself, default to a compact markdown table:

   | Date | Time | Event | Location |
   |------|------|-------|----------|

   - Format dates like "Sun Sep 20" and times like "3:00 PM"; write
     "all day" for all-day events.
   - Multi-day events (campouts, USS Lexington): one row, "Oct 3–4".
   - Trips often have both an umbrella all-day event and timed itinerary
     sub-events on the same days (e.g. "USS Lexington" + "Arrival/Check
     In"); group these rather than listing them as unrelated rows.
   - Committee meetings are online — surface the `meet_link`.
   - Close with when the data was fetched (`fetched_at`).

   For narrower questions ("when is the next pack meeting?"), answer
   directly — don't dump the full table.

4. This is a public calendar feed, so the data is not sensitive, but event
   descriptions sometimes embed Google Meet dial-in PINs — leave those out
   of anything shared beyond the user.

## Troubleshooting

- **Exit code 2** — download failed and no cached feed exists. Check
  network; the feed URL is pinned in the script (`ICS_URL`). If Google
  changed the calendar's address, get the new one from
  https://pack97.com/calendar/ ("Subscribe via iOS" link) and update
  `ICS_URL`.
- **Exit code 3** — feed downloaded but isn't parseable ICS (the raw file
  is kept at `~/.scouting-skills/calendar.ics` — inspect it; Google may be
  serving an error page).
- **Event seems missing** — widen the window (`--days-back`,
  `--days-ahead`); remember brand-new events can take a few hours to show
  up in Google's public feed, and cancelled events are filtered out on
  purpose.
- **"TBD" entries** — the pack really does put placeholder events like
  "TBD: INFO SESSION" on the calendar; report them as tentative rather
  than skipping them.
- **Cancellations in the title** — properly-cancelled events are filtered
  out by the script, but the pack sometimes cancels by renaming instead
  (e.g. "[CANCELLED] SPRING CAMPOUT"). Treat any summary containing
  CANCELLED/CANCELED as not happening; only mention it if the user asks
  about that event specifically.
