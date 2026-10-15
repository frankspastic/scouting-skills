#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-api-python-client>=2.100",
#     "google-auth>=2.23",
#     "google-auth-oauthlib>=1.1",
# ]
# ///
"""
den_progress_to_sheet.py — write a den's adventure-by-scout grid (cells say
Complete / Incomplete / Not Started) to a tab in a Google Sheet, replacing
that tab's contents on reruns (--replace).

Uses the same Google login as pack-roster's sync_roster_to_sheet.py
(~/.scouting-skills/google-sheets-token.json).

Usage:
    uv run den_progress_to_sheet.py --den "Webelos Den 7" \\
        --sheet <spreadsheet id or URL> [--tab "Webelos Advancement"]
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pack-roster" / "scripts"))
import sync_roster_to_sheet as sync  # noqa: E402  (shared Google auth)

from den_progress import adventure_grid, analyze, load_den, local_date, status_word  # noqa: E402

COLORS = {  # cell text -> background (0-1 RGB)
    "Complete": (0.72, 0.88, 0.72),
    "Incomplete": (1.0, 0.90, 0.60),
    "Not Started": (0.93, 0.93, 0.93),
}


def log(msg: str) -> None:
    print(f"[den_progress_to_sheet] {msg}", file=sys.stderr, flush=True)


def quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def spreadsheet_id(ref: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", ref)
    return m.group(1) if m else ref


def build_rows(data: dict) -> tuple[list[list], int]:
    """Sheet rows, and the 0-based index of the header row."""
    info = analyze(data)
    grid = adventure_grid(info, status_word)
    title = f"{data['den']}: {info['rank']} rank progress"
    if info["version"]:
        title += f" ({info['version']})"
    rows = [
        [title],
        [f"From Scoutbook Plus, as of {local_date(data['fetched_at'])}. The rank needs "
         f"all {len(info['required'])} required adventures plus "
         f"{info['electives_needed']} electives."],
        ["Adventure", "Type"] + [s["name"] for s in info["scouts"]],
    ]
    for name, required, cells in grid:
        rows.append([name, "Required" if required else "Elective"] + cells)
    if not any(not req for _, req, _ in grid):
        rows.append(["No electives started yet", "Elective"])
    return rows, 2


def tab_for(service, sid: str, title: str, replace: bool) -> int:
    meta = service.spreadsheets().get(
        spreadsheetId=sid, fields="sheets.properties").execute()
    for sh in meta["sheets"]:
        if sh["properties"]["title"] == title:
            if not replace:
                log(f'Tab "{title}" already exists. Pass --replace to overwrite it, '
                    "or --tab to pick another name.")
                sys.exit(1)
            return sh["properties"]["sheetId"]
    reply = service.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
        {"addSheet": {"properties": {"title": title}}}]}).execute()
    log(f'Created tab "{title}".')
    return reply["replies"][0]["addSheet"]["properties"]["sheetId"]


def write(service, sid: str, tab: str, sheet_id: int, rows: list[list],
          header_idx: int) -> None:
    ncols = max(len(r) for r in rows)
    nrows = len(rows)
    # Clear old values, formatting and conditional rules on this tab.
    meta = service.spreadsheets().get(
        spreadsheetId=sid, fields="sheets.properties.sheetId,sheets.conditionalFormats").execute()
    old_rules = next((sh.get("conditionalFormats", []) for sh in meta["sheets"]
                      if sh["properties"]["sheetId"] == sheet_id), [])
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
            for _ in old_rules]
    reqs.append({"updateCells": {"range": {"sheetId": sheet_id},
                                 "fields": "userEnteredValue,userEnteredFormat"}})
    service.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{quote_tab(tab)}!A1",
        valueInputOption="RAW", body={"values": rows}).execute()

    def rng(r0, r1, c0, c1):
        return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1}

    body_rng = rng(header_idx + 1, nrows, 2, ncols)
    reqs = [
        {"repeatCell": {"range": rng(0, 1, 0, 1),
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": rng(1, 2, 0, 1),
                        "cell": {"userEnteredFormat": {"textFormat": {"italic": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": rng(header_idx, header_idx + 1, 0, ncols),
                        "cell": {"userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85},
                            "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE",
                            "horizontalAlignment": "CENTER"}},
                        "fields": "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor,"
                                  "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment,"
                                  "userEnteredFormat.horizontalAlignment"}},
        {"repeatCell": {"range": body_rng,
                        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                        "fields": "userEnteredFormat.horizontalAlignment"}},
        {"repeatCell": {"range": rng(header_idx + 1, nrows, 0, 1),
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"updateBorders": {"range": rng(header_idx, nrows, 0, ncols),
                           **{side: {"style": "SOLID", "color": {"red": 0.75, "green": 0.75, "blue": 0.75}}
                              for side in ("top", "bottom", "left", "right",
                                           "innerHorizontal", "innerVertical")}}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
            "frozenRowCount": header_idx + 1, "frozenColumnCount": 1}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                 "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                 "startIndex": 1, "endIndex": 2},
                                       "properties": {"pixelSize": 80}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                 "startIndex": 2, "endIndex": ncols},
                                       "properties": {"pixelSize": 100}, "fields": "pixelSize"}},
    ]
    # Conditional colors, so hand edits to a cell recolor it too.
    for word, (r, g, b) in COLORS.items():
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [body_rng],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": word}]},
                            "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}})
    service.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--den", required=True)
    ap.add_argument("--sheet", required=True, help="spreadsheet ID or URL")
    ap.add_argument("--tab", default=None,
                    help='tab name (default "<Rank> Advancement", e.g. "Webelos Advancement")')
    ap.add_argument("--replace", action="store_true",
                    help="overwrite the tab if it already exists (needed on reruns)")
    ap.add_argument("--dry-run", action="store_true", help="print the rows; don't write")
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR",
                                                Path.home() / ".scouting-skills")))
    args = ap.parse_args()

    data = load_den(args.data_dir, args.den)
    rows, header_idx = build_rows(data)
    tab = args.tab or f"{data['den_rank']} Advancement"
    if args.dry_run:
        for r in rows:
            print("\t".join(r))
        return

    sid = spreadsheet_id(args.sheet)
    service = sync.build_service()
    sheet_id = tab_for(service, sid, tab, args.replace)
    write(service, sid, tab, sheet_id, rows, header_idx)
    log(f'Wrote {len(rows) - header_idx - 1} adventure rows to "{tab}" '
        f"(https://docs.google.com/spreadsheets/d/{sid}/edit#gid={sheet_id}).")


if __name__ == "__main__":
    main()
