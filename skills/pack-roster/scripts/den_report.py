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
den_report.py — write a per-den summary tab (with bar charts) into the pack
roster spreadsheet: scout counts, registration expired/not-expired, and dues
paid/not-paid, broken down by den.

Unlike a normal sync, the numbers themselves are *live spreadsheet formulas*
(COUNTIFS) reading the roster tab (whichever tab isn't this report — normally
the one sync_roster_to_sheet.py writes), not values computed here and pasted
in. Only the *set of dens* (which rows to create, and their sort order) is
computed by this script, from the roster tab's Den column, each time it
runs — the counts themselves recalculate on their own afterwards as the
roster tab changes, with no need to rerun this script. Rerunning it is still
useful when a den has been added/removed/renamed, since that changes which
rows exist.

Reuses sync_roster_to_sheet.py's auth/config (same OAuth token, same
ROSTER_SHEET_ID in ~/.scouting-skills/google-sheets.env). Does not touch
roster.json or dues.json — the roster tab already has everything needed
(including a Dues Paid column, if sync_roster_to_sheet.py was run with dues
configured), so this script only ever reads the spreadsheet itself.

Exit codes:
    0  success
    2  missing OAuth client / sheet id config
    3  no other tab found to read roster data from, or that tab is missing
       expected columns (Den / First Name / Registration Expires)
    4  Google API error (message printed to stderr)
"""

import argparse
import datetime
import sys

import sync_roster_to_sheet as sync

DEFAULT_TAB = "Den Report"

# Both this report and sync_roster_to_sheet.py write title row, blank row,
# header row, then data — so row 4 is where data starts on either tab.
DATA_START_ROW = 4

REQUIRED_SOURCE_COLUMNS = ["First Name", "Den", "Registration Expires"]
DUES_COLUMN = "Dues Paid"
NO_DEN_LABEL = "No Den Assigned"

# Registration chart: red for expired, blue for not-expired (swapped from the
# chart-type default of blue-then-red, at the user's request).
REG_EXPIRED_COLOR = {"red": 0.859, "green": 0.266, "blue": 0.216}
REG_NOT_EXPIRED_COLOR = {"red": 0.259, "green": 0.522, "blue": 0.957}

# Program order for sorting dens; anything unrecognized sorts after these,
# alphabetically, with no-den scouts last of all.
PROGRAM_ORDER = ["lion", "tiger", "wolf", "bear", "webelos", "aol", "arrow"]


def log(msg: str) -> None:
    print(f"[den_report] {msg}", file=sys.stderr, flush=True)


def den_sort_key(den: str) -> tuple:
    if not den:
        return (2, "￿")
    first_word = den.strip().split()[0].lower()
    try:
        return (0, PROGRAM_ORDER.index(first_word), den)
    except ValueError:
        return (1, den)


def col_letter(idx: int) -> str:
    """0-based column index -> spreadsheet column letters (0 -> 'A')."""
    n = idx + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def resolve_source_tab(service, spreadsheet_id: str, exclude_title: str) -> dict:
    """Find the roster tab to build formulas against: the lowest-index tab
    that isn't this report's own tab. Reads its header row (row 3) live
    rather than assuming a fixed column layout, since sync_roster_to_sheet.py
    may or may not include the Dues Paid column."""
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    candidates = sorted(
        (s["properties"] for s in meta["sheets"] if s["properties"]["title"] != exclude_title),
        key=lambda p: p["index"],
    )
    if not candidates:
        log(f"No tab other than {exclude_title!r} found in this spreadsheet to read roster data from.")
        sys.exit(3)
    props = candidates[0]
    title = props["title"]
    row_count = max(props["gridProperties"]["rowCount"], DATA_START_ROW)

    header_resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{title}'!A3:Z3"
    ).execute()
    header_row = header_resp.get("values") or [[]]
    header_row = header_row[0] if header_row else []
    cols = {name: col_letter(i) for i, name in enumerate(header_row) if name}

    missing = [c for c in REQUIRED_SOURCE_COLUMNS if c not in cols]
    if missing:
        log(f"Tab {title!r}'s header row (row 3) is missing expected column(s) {missing}. "
            f"Run sync_roster_to_sheet.py first, or pass --tab to point at the right report tab.")
        sys.exit(3)

    return {"title": title, "cols": cols, "row_count": row_count}


def fetch_den_labels(service, spreadsheet_id: str, src: dict) -> list[str]:
    """Distinct den labels among rows with a non-blank First Name, in
    display order (blank -> "No Den Assigned", parked at the end)."""
    first_col = src["cols"]["First Name"]
    den_col = src["cols"]["Den"]
    resp = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=[
            f"'{src['title']}'!{first_col}{DATA_START_ROW}:{first_col}{src['row_count']}",
            f"'{src['title']}'!{den_col}{DATA_START_ROW}:{den_col}{src['row_count']}",
        ],
    ).execute()
    value_ranges = resp.get("valueRanges", [{}, {}])
    first_vals = [r[0] if r else "" for r in value_ranges[0].get("values", [])]
    den_vals = [r[0] if r else "" for r in value_ranges[1].get("values", [])]

    dens: set[str] = set()
    for i, first in enumerate(first_vals):
        if not first:
            continue
        dens.add(den_vals[i] if i < len(den_vals) else "")
    return [d or NO_DEN_LABEL for d in sorted(dens, key=den_sort_key)]


def source_range(src: dict, colname: str) -> str:
    col = src["cols"][colname]
    return f"'{src['title']}'!${col}${DATA_START_ROW}:${col}${src['row_count']}"


def den_row_formulas(row_num: int, label: str, src: dict, dues_present: bool) -> list:
    if label == NO_DEN_LABEL:
        match = f'{source_range(src, "First Name")},"<>",{source_range(src, "Den")},""'
    else:
        # Matches against this row's own label cell ($A{row_num}) rather than
        # a literal, so hand-editing the label keeps the formula in sync.
        match = f'{source_range(src, "Den")},$A{row_num}'

    reg_range = source_range(src, "Registration Expires")
    scouts = f"=COUNTIFS({match})"
    expired = f'=COUNTIFS({match},{reg_range},"<"&TODAY(),{reg_range},"<>")'
    not_expired = f"=B{row_num}-C{row_num}"
    row = [label, scouts, expired, not_expired]

    if dues_present:
        dues_range = source_range(src, DUES_COLUMN)
        paid = f'=COUNTIFS({match},{dues_range},"Yes")'
        not_paid = f"=B{row_num}-E{row_num}"
        row += [paid, not_paid]
    return row


def total_row_formulas(data_start: int, data_end: int, dues_present: bool) -> list:
    total_row_num = data_end + 1
    row = [
        "Total",
        f"=SUM(B{data_start}:B{data_end})",
        f"=SUM(C{data_start}:C{data_end})",
        f"=B{total_row_num}-C{total_row_num}",
    ]
    if dues_present:
        row += [
            f"=SUM(E{data_start}:E{data_end})",
            f"=B{total_row_num}-E{total_row_num}",
        ]
    return row


def build_report(src: dict, den_labels: list[str], dues_present: bool) -> tuple[list[list], list[str], int]:
    """-> (rows, header, num_den_rows). Rows are ready to write starting at A1."""
    header = ["Den", "Scouts", "Registration Expired", "Registration Not Expired"]
    if dues_present:
        header += [DUES_COLUMN, "Dues Not Paid"]

    den_rows = [
        den_row_formulas(DATA_START_ROW + i, label, src, dues_present)
        for i, label in enumerate(den_labels)
    ]

    if den_rows:
        data_end = DATA_START_ROW + len(den_labels) - 1
        total_row = total_row_formulas(DATA_START_ROW, data_end, dues_present)
    else:
        total_row = ["Total", 0, 0, 0] + ([0, 0] if dues_present else [])

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    title = (f"Den Report — den list refreshed {now}; "
             f"counts are live formulas reading the {src['title']!r} tab")

    rows = [[title], [], header, *den_rows, total_row]
    return rows, header, len(den_rows)


def resolve_or_create_tab(service, spreadsheet_id: str, tab_name: str) -> dict:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets(properties,charts.chartId)"
    ).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == tab_name:
            for chart in sheet.get("charts", []):
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": [
                        {"deleteEmbeddedObject": {"objectId": chart["chartId"]}}
                    ]},
                ).execute()
            return sheet["properties"]

    log(f"Tab {tab_name!r} not found; creating it.")
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]


def basic_chart_request(sheet_id: int, chart_id_hint: int, title: str,
                         header_row_idx: int, data_end_row_idx: int,
                         series_cols: list[int], anchor_row: int, anchor_col: int,
                         series_colors: list[dict] | None = None) -> dict:
    domain_range = {
        "sheetId": sheet_id, "startRowIndex": header_row_idx, "endRowIndex": data_end_row_idx,
        "startColumnIndex": 0, "endColumnIndex": 1,
    }
    series = [
        {
            "series": {"sourceRange": {"sources": [{
                "sheetId": sheet_id, "startRowIndex": header_row_idx, "endRowIndex": data_end_row_idx,
                "startColumnIndex": col, "endColumnIndex": col + 1,
            }]}},
            "targetAxis": "LEFT_AXIS",
            **({"colorStyle": {"rgbColor": series_colors[i]}} if series_colors else {}),
        }
        for i, col in enumerate(series_cols)
    ]
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "basicChart": {
                        "chartType": "COLUMN",
                        "legendPosition": "BOTTOM_LEGEND",
                        "stackedType": "STACKED",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Den"},
                            {"position": "LEFT_AXIS", "title": "Scouts"},
                        ],
                        "domains": [{"domain": {"sourceRange": {"sources": [domain_range]}}}],
                        "series": series,
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": sheet_id, "rowIndex": anchor_row, "columnIndex": anchor_col},
                        "widthPixels": 480, "heightPixels": 320,
                    }
                },
            }
        }
    }


def write_report(service, spreadsheet_id: str, tab: dict, rows: list[list],
                  header: list[str], num_den_rows: int, dues_present: bool) -> None:
    title = tab["title"]
    sheet_id = tab["sheetId"]
    n_cols = max(len(header), 1)

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{title}'"
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"'{title}'!A1",
        valueInputOption="USER_ENTERED", body={"values": rows},
    ).execute()

    bold = {"textFormat": {"bold": True}}
    header_row_idx = 2  # 0-indexed row 3
    data_end_row_idx = header_row_idx + 1 + num_den_rows  # exclusive, excludes Total row

    requests = [
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": bold}, "fields": "userEnteredFormat.textFormat.bold",
        }},
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": header_row_idx, "endRowIndex": header_row_idx + 1,
                      "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": bold}, "fields": "userEnteredFormat.textFormat.bold",
        }},
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": data_end_row_idx, "endRowIndex": data_end_row_idx + 1,
                      "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": bold}, "fields": "userEnteredFormat.textFormat.bold",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 3}},
            "fields": "gridProperties.frozenRowCount",
        }},
        basic_chart_request(
            sheet_id, 1, "Scouts by Den — Registration Status",
            header_row_idx, data_end_row_idx, [2, 3],
            anchor_row=data_end_row_idx + 3, anchor_col=0,
            series_colors=[REG_EXPIRED_COLOR, REG_NOT_EXPIRED_COLOR],
        ),
    ]
    if dues_present:
        requests.append(basic_chart_request(
            sheet_id, 2, "Scouts by Den — Dues Paid Status",
            header_row_idx, data_end_row_idx, [4, 5],
            anchor_row=data_end_row_idx + 3, anchor_col=6,
        ))

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", help="Spreadsheet URL or ID (default: ROSTER_SHEET_ID config)")
    parser.add_argument("--tab", default=DEFAULT_TAB, help=f"Tab name to write (default: {DEFAULT_TAB!r})")
    parser.add_argument("--no-dues", action="store_true",
                        help="Skip the dues-paid columns even if the roster tab has a Dues Paid column")
    parser.add_argument("--dry-run", action="store_true", help="Print the formulas, don't write")
    args = parser.parse_args()

    spreadsheet_id, _gid = sync.resolve_sheet_id(args.sheet)

    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        log("google-api-python-client not installed; run this script with `uv run` "
            "so its inline dependencies are picked up.")
        sys.exit(2)

    service = sync.build_service()

    try:
        src = resolve_source_tab(service, spreadsheet_id, args.tab)
        den_labels = fetch_den_labels(service, spreadsheet_id, src)
    except HttpError as e:
        log(f"Google Sheets API error reading the roster tab: {e}")
        sys.exit(4)

    dues_present = DUES_COLUMN in src["cols"] and not args.no_dues
    rows, header, num_den_rows = build_report(src, den_labels, dues_present)

    if args.dry_run:
        log(f"Would write {num_den_rows} den rows (formulas reading {src['title']!r}) "
            f"to spreadsheet {spreadsheet_id}, tab {args.tab!r}")
        for row in rows:
            print("\t".join(str(c) for c in row))
        return

    try:
        tab = resolve_or_create_tab(service, spreadsheet_id, args.tab)
        write_report(service, spreadsheet_id, tab, rows, header, num_den_rows, dues_present)
    except HttpError as e:
        log(f"Google Sheets API error: {e}")
        sys.exit(4)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={tab['sheetId']}"
    log(f"Wrote den report ({num_den_rows} dens, live formulas) to {tab['title']!r}: {url}")
    print(url)


if __name__ == "__main__":
    main()
