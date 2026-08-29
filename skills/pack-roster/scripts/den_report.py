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
den_report.py — write a per-den summary tab (with charts) into the pack
roster spreadsheet: scout counts, registration expired/not-expired, dues
paid/not-paid, and male/female, broken down by den. Charts: stacked columns
per den for each breakdown, plus a pie of the pack-wide male/female split.

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
GENDER_COLUMN = "Gender"
RENEWAL_COLUMN = "Renewal Status"
NO_RENEWAL_LABEL = "Not on file"

# Renewal statuses in cycle order, so the pie's slices read in a sensible
# progression instead of alphabetically. Anything the API adds later that
# isn't listed here sorts after these, alphabetically.
RENEWAL_ORDER = [
    "Current", "Eligible to Renew", "Eligible to Renew (unit only)",
    "Renewed", "Opted Out", "Expired",
]
NO_DEN_LABEL = "No Den Assigned"

# Dens whose scouts are real but shouldn't move the pack's numbers: the
# opt-out den (families who've said they aren't continuing) and scouts with no
# den yet. They still get their own rows, below the Total and out of every
# chart, so nobody reads the report and wonders where those scouts went.
# --include-all counts them like any other den.
EXCLUDED_DENS = ["Lion Den 999 OPT OUT DEN", NO_DEN_LABEL]
EXCLUDED_SECTION_LABEL = "Excluded from pack totals and charts"

# Registration chart: red for expired, blue for not-expired (swapped from the
# chart-type default of blue-then-red, at the user's request).
REG_EXPIRED_COLOR = {"red": 0.859, "green": 0.266, "blue": 0.216}
REG_NOT_EXPIRED_COLOR = {"red": 0.259, "green": 0.522, "blue": 0.957}

# Gender chart: blue for male, purple for female — distinct from the red/blue
# registration pair so the two charts don't read as the same breakdown.
MALE_COLOR = {"red": 0.259, "green": 0.522, "blue": 0.957}
FEMALE_COLOR = {"red": 0.616, "green": 0.318, "blue": 0.737}

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


def renewal_sort_key(label: str) -> tuple:
    if label == NO_RENEWAL_LABEL:
        return (2, "")
    if label in RENEWAL_ORDER:
        return (0, RENEWAL_ORDER.index(label))
    return (1, label.lower())


def fetch_renewal_labels(service, spreadsheet_id: str, src: dict) -> list[str]:
    """Distinct renewal-status values among rows with a non-blank First Name,
    in cycle order. Unlike dens this is discovered purely to build the pie's
    label list — blanks (a paid-but-not-in-Scoutbook row) become their own
    slice rather than being dropped, so the slices still sum to the roster."""
    first_col = src["cols"]["First Name"]
    renewal_col = src["cols"][RENEWAL_COLUMN]
    resp = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=[
            f"'{src['title']}'!{first_col}{DATA_START_ROW}:{first_col}{src['row_count']}",
            f"'{src['title']}'!{renewal_col}{DATA_START_ROW}:{renewal_col}{src['row_count']}",
        ],
    ).execute()
    value_ranges = resp.get("valueRanges", [{}, {}])
    first_vals = [r[0] if r else "" for r in value_ranges[0].get("values", [])]
    renewal_vals = [r[0] if r else "" for r in value_ranges[1].get("values", [])]

    statuses: set[str] = set()
    for i, first in enumerate(first_vals):
        if not first:
            continue
        statuses.add(renewal_vals[i] if i < len(renewal_vals) else "")
    return [r or NO_RENEWAL_LABEL for r in sorted(statuses, key=renewal_sort_key)]


def source_range(src: dict, colname: str) -> str:
    col = src["cols"][colname]
    return f"'{src['title']}'!${col}${DATA_START_ROW}:${col}${src['row_count']}"


def den_row_formulas(row_num: int, label: str, src: dict, dues_present: bool,
                     gender_present: bool) -> list:
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

    if gender_present:
        gender_range = source_range(src, GENDER_COLUMN)
        # Both counted explicitly rather than deriving one as Scouts-minus-the-
        # other: rows with no gender on file (a paid-but-not-in-Scoutbook scout
        # has a blank one) would otherwise be silently counted as female.
        male = f'=COUNTIFS({match},{gender_range},"M")'
        female = f'=COUNTIFS({match},{gender_range},"F")'
        row += [male, female]
    return row


def total_row_formulas(data_start: int, data_end: int, dues_present: bool,
                       gender_present: bool) -> list:
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

    if gender_present:
        male_col, female_col = ("G", "H") if dues_present else ("E", "F")
        row += [
            f"=SUM({male_col}{data_start}:{male_col}{data_end})",
            f"=SUM({female_col}{data_start}:{female_col}{data_end})",
        ]
    return row


def build_report(src: dict, den_labels: list[str], dues_present: bool,
                 gender_present: bool,
                 excluded_dens: list[str] | None = None
                 ) -> tuple[list[list], list[str], int, int]:
    """-> (rows, header, num_counted_dens, num_excluded_dens). Rows are ready
    to write starting at A1.

    Excluded dens are laid out *after* the Total row rather than dropped, so
    the Total and the charts (which read the contiguous band of counted den
    rows above the Total) leave them out while the numbers stay visible.
    """
    excluded_dens = excluded_dens or []
    header = ["Den", "Scouts", "Registration Expired", "Registration Not Expired"]
    if dues_present:
        header += [DUES_COLUMN, "Dues Not Paid"]
    if gender_present:
        header += ["Male", "Female"]

    counted = [d for d in den_labels if d not in excluded_dens]
    excluded = [d for d in den_labels if d in excluded_dens]

    den_rows = [
        den_row_formulas(DATA_START_ROW + i, label, src, dues_present, gender_present)
        for i, label in enumerate(counted)
    ]

    if den_rows:
        data_end = DATA_START_ROW + len(counted) - 1
        total_row = total_row_formulas(DATA_START_ROW, data_end, dues_present, gender_present)
    else:
        total_row = (["Total", 0, 0, 0] + ([0, 0] if dues_present else [])
                     + ([0, 0] if gender_present else []))

    # Blank spacer + section label sit between the Total and the excluded
    # rows, so their self-referencing formulas need those two rows counted.
    excluded_start = DATA_START_ROW + len(counted) + 3
    excluded_rows = [
        den_row_formulas(excluded_start + i, label, src, dues_present, gender_present)
        for i, label in enumerate(excluded)
    ]

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    title = (f"Den Report — den list refreshed {now}; "
             f"counts are live formulas reading the {src['title']!r} tab")
    if excluded:
        title += f"; {', '.join(excluded)} excluded from the Total and charts"

    rows = [[title], [], header, *den_rows, total_row]
    if excluded_rows:
        rows += [[], [EXCLUDED_SECTION_LABEL], *excluded_rows]
    return rows, header, len(den_rows), len(excluded_rows)


def exclusion_criteria(src: dict, excluded_dens: list[str]) -> str:
    """Extra COUNTIFS criteria pairs keeping the excluded dens out of a
    pack-wide count, as a string ready to append inside a COUNTIFS call."""
    den_range = source_range(src, "Den")
    parts = []
    for den in excluded_dens:
        if den == NO_DEN_LABEL:
            parts.append(f'{den_range},"<>"')  # non-blank den only
        else:
            parts.append(f'{den_range},"<>{den}"')
    return ("," + ",".join(parts)) if parts else ""


def build_renewal_block(src: dict, labels: list[str], start_col_idx: int,
                        excluded_dens: list[str] | None = None) -> list[list]:
    """A small label/count table for the renewal-status pie, written to the
    right of the den table (renewal status is one multi-valued field, so it
    can't be a pair of columns the way male/female is). Counts are COUNTIFS
    formulas like the rest of the report, so this block stays live too."""
    label_col = col_letter(start_col_idx)
    first_range = source_range(src, "First Name")
    renewal_range = source_range(src, RENEWAL_COLUMN)
    excl = exclusion_criteria(src, excluded_dens or [])

    rows = [[RENEWAL_COLUMN, "Scouts"]]
    for i, label in enumerate(labels):
        row_num = DATA_START_ROW + i
        if label == NO_RENEWAL_LABEL:
            criterion = '""'
        else:
            criterion = f"${label_col}{row_num}"
        rows.append([label,
                     f'=COUNTIFS({first_range},"<>",{renewal_range},{criterion}{excl})'])

    if labels:
        value_col = col_letter(start_col_idx + 1)
        data_end = DATA_START_ROW + len(labels) - 1
        rows.append(["Total", f"=SUM({value_col}{DATA_START_ROW}:{value_col}{data_end})"])
    else:
        rows.append(["Total", 0])
    return rows


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


def pie_chart_request(sheet_id: int, title: str, header_row_idx: int,
                      total_row_idx: int, first_col: int, last_col: int,
                      anchor_row: int, anchor_col: int) -> dict:
    """A pie of the pack-wide totals: labels from the header row, values from
    the Total row — both horizontal one-row ranges, so this reads the same
    live formulas the table shows rather than recomputing anything. Slice
    colors are Sheets' defaults; the pie API has no per-slice color field.
    """
    def band(row_idx: int) -> dict:
        return {
            "sheetId": sheet_id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
            "startColumnIndex": first_col, "endColumnIndex": last_col + 1,
        }
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "pieChart": {
                        "legendPosition": "LABELED_LEGEND",
                        "domain": {"sourceRange": {"sources": [band(header_row_idx)]}},
                        "series": {"sourceRange": {"sources": [band(total_row_idx)]}},
                        "threeDimensional": False,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": sheet_id, "rowIndex": anchor_row,
                                       "columnIndex": anchor_col},
                        "widthPixels": 480, "heightPixels": 320,
                    }
                },
            }
        }
    }


def label_pie_chart_request(sheet_id: int, title: str, label_col: int,
                            first_row_idx: int, last_row_idx: int,
                            anchor_row: int, anchor_col: int) -> dict:
    """A pie over a vertical label column and the value column beside it."""
    def band(col: int) -> dict:
        return {
            "sheetId": sheet_id, "startRowIndex": first_row_idx, "endRowIndex": last_row_idx,
            "startColumnIndex": col, "endColumnIndex": col + 1,
        }
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "pieChart": {
                        "legendPosition": "LABELED_LEGEND",
                        "domain": {"sourceRange": {"sources": [band(label_col)]}},
                        "series": {"sourceRange": {"sources": [band(label_col + 1)]}},
                        "threeDimensional": False,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": sheet_id, "rowIndex": anchor_row,
                                       "columnIndex": anchor_col},
                        "widthPixels": 480, "heightPixels": 320,
                    }
                },
            }
        }
    }


def write_report(service, spreadsheet_id: str, tab: dict, rows: list[list],
                  header: list[str], num_den_rows: int, dues_present: bool,
                  gender_present: bool, renewal_block: list[list] | None = None,
                  num_excluded_dens: int = 0) -> None:
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

    # One blank gutter column after the den table.
    renewal_col_idx = n_cols + 1
    if renewal_block:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{title}'!{col_letter(renewal_col_idx)}3",
            valueInputOption="USER_ENTERED",
            body={"values": renewal_block},
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
        *([{"repeatCell": {
            "range": {"sheetId": sheet_id,
                      "startRowIndex": data_end_row_idx + 2,
                      "endRowIndex": data_end_row_idx + 3,
                      "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "italic": True}}},
            "fields": "userEnteredFormat.textFormat",
        }}] if num_excluded_dens else []),
        *([{"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": header_row_idx,
                      "endRowIndex": header_row_idx + 1,
                      "startColumnIndex": renewal_col_idx,
                      "endColumnIndex": renewal_col_idx + 2},
            "cell": {"userEnteredFormat": bold}, "fields": "userEnteredFormat.textFormat.bold",
        }}] if renewal_block else []),
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

    if gender_present:
        male_col = header.index("Male")
        female_col = header.index("Female")
        # Second band of charts, below the first (the 320px-tall charts above
        # cover roughly 16 rows).
        chart_row = data_end_row_idx + 20
        requests.append(pie_chart_request(
            sheet_id, "Pack Total — Male vs Female",
            header_row_idx, data_end_row_idx, male_col, female_col,
            anchor_row=chart_row, anchor_col=0,
        ))
        requests.append(basic_chart_request(
            sheet_id, 3, "Scouts by Den — Male vs Female",
            header_row_idx, data_end_row_idx, [male_col, female_col],
            anchor_row=chart_row, anchor_col=6,
            series_colors=[MALE_COLOR, FEMALE_COLOR],
        ))

    if renewal_block:
        # header at row 3, then one row per status, then a Total row to exclude
        n_status = len(renewal_block) - 2
        first_row_idx = header_row_idx + 1
        requests.append(label_pie_chart_request(
            sheet_id, "Pack Total — Renewal Status",
            renewal_col_idx, first_row_idx, first_row_idx + n_status,
            anchor_row=data_end_row_idx + 20, anchor_col=12 if gender_present else 0,
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
    parser.add_argument("--include-all", action="store_true",
                        help="Count every den in the Total and charts, including "
                             f"{' and '.join(EXCLUDED_DENS)}")
    parser.add_argument("--no-renewal", action="store_true",
                        help="Skip the renewal-status breakdown and its pie chart "
                             "even if the roster tab has a Renewal Status column")
    parser.add_argument("--no-gender", action="store_true",
                        help="Skip the male/female columns and charts even if the "
                             "roster tab has a Gender column")
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
    gender_present = GENDER_COLUMN in src["cols"] and not args.no_gender
    excluded_dens = [] if args.include_all else EXCLUDED_DENS
    rows, header, num_den_rows, num_excluded_dens = build_report(
        src, den_labels, dues_present, gender_present, excluded_dens)

    renewal_block = None
    if RENEWAL_COLUMN in src["cols"] and not args.no_renewal:
        try:
            renewal_labels = fetch_renewal_labels(service, spreadsheet_id, src)
        except HttpError as e:
            log(f"Google Sheets API error reading the roster tab: {e}")
            sys.exit(4)
        renewal_block = build_renewal_block(src, renewal_labels, len(header) + 1,
                                            excluded_dens)

    if args.dry_run:
        log(f"Would write {num_den_rows} den rows (formulas reading {src['title']!r}) "
            f"to spreadsheet {spreadsheet_id}, tab {args.tab!r}")
        for row in rows:
            print("\t".join(str(c) for c in row))
        if renewal_block:
            log(f"Renewal-status block ({len(renewal_block) - 2} statuses) at column "
                f"{col_letter(len(header) + 1)}:")
            for row in renewal_block:
                print("\t".join(str(c) for c in row))
        return

    try:
        tab = resolve_or_create_tab(service, spreadsheet_id, args.tab)
        write_report(service, spreadsheet_id, tab, rows, header, num_den_rows,
                     dues_present, gender_present, renewal_block, num_excluded_dens)
    except HttpError as e:
        log(f"Google Sheets API error: {e}")
        sys.exit(4)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={tab['sheetId']}"
    counted_note = f"{num_den_rows} dens counted"
    if num_excluded_dens:
        counted_note += f", {num_excluded_dens} excluded from totals/charts"
    log(f"Wrote den report ({counted_note}, live formulas) to {tab['title']!r}: {url}")
    print(url)


if __name__ == "__main__":
    main()
