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
sync_roster_to_sheet.py — push the cached pack roster (from fetch_roster.py)
into a Google Sheet, overwriting one tab with a fresh, formatted snapshot.

Scouts whose dues are paid but who aren't in Scoutbook yet are added as extra
rows, filled in from what the payment form knows and flagged in Registration
Status, so a paid family is never simply missing from the roster.

Auth: a dedicated OAuth "Desktop app" client (scouting-skills only, not
shared with any other project) at ~/.scouting-skills/google-oauth-client.json.
First run opens a browser for the user to consent as themselves (scope:
spreadsheets only); the resulting token is cached at
~/.scouting-skills/google-sheets-token.json and silently refreshed after
that — no browser needed on subsequent runs. Because auth is "as the user",
not a service account, no per-sheet sharing step is needed: it can write to
any sheet the user already owns or has edit access to.

The target spreadsheet defaults to ROSTER_SHEET_ID in
~/.scouting-skills/google-sheets.env (KEY=VALUE, same convention as
mailchimp.env), overridable with --sheet (accepts a raw ID or a full
docs.google.com URL — a URL's gid, if present, also picks the target tab).

A "Dues Paid" column is filled in from the dues-payment form exports
(DUES_SHEET_ID in the same env file, or --dues-sheet) via fetch_dues.py.
More than one sheet may be listed, each optionally with an "@YYYY-MM-DD"
cutoff, because early payers land in the previous year's sheet. The column is
opt-out rather than required: with no dues sheet configured, or with
--no-dues, the roster is written without it instead of with a column of
misleading "No"s.

Exit codes:
    0  success
    2  missing OAuth client / sheet id config
    3  roster.json missing (run fetch_roster.py first)
    4  Google API error (message printed to stderr)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import fetch_dues

DATA_DIR = Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR", Path.home() / ".scouting-skills"))
ROSTER_FILE = DATA_DIR / "roster.json"
ENV_FILE = DATA_DIR / "google-sheets.env"
CLIENT_SECRET_FILE = DATA_DIR / "google-oauth-client.json"
TOKEN_FILE = DATA_DIR / "google-sheets-token.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DUES_COLUMN = "Dues Paid"
HEADER = [
    "First Name", "Last Name", "Den", "Rank", DUES_COLUMN, "BSA ID",
    "Date of Birth", "Registration Expires", "Registration Status",
    "Parent/Guardian Names", "Parent Emails", "Parent Phones",
]


def log(msg: str) -> None:
    print(f"[sync_roster_to_sheet] {msg}", file=sys.stderr, flush=True)


def load_config_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def parse_sheet_ref(ref: str) -> tuple[str, int | None]:
    """Accept either a raw spreadsheet ID or a full Sheets URL. Returns
    (spreadsheet_id, gid) — gid is None unless the URL specified one."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", ref)
    sheet_id = m.group(1) if m else ref
    gm = re.search(r"[#&?]gid=(\d+)", ref)
    gid = int(gm.group(1)) if gm else None
    return sheet_id, gid


def resolve_sheet_id(cli_value: str | None) -> tuple[str, int | None]:
    if cli_value:
        return parse_sheet_ref(cli_value)
    load_config_file()
    env_value = os.environ.get("ROSTER_SHEET_ID")
    if not env_value:
        log(f"No --sheet given and ROSTER_SHEET_ID not set in {ENV_FILE}.")
        log("Pass --sheet <url-or-id>, or add ROSTER_SHEET_ID=<id> to that file.")
        sys.exit(2)
    return parse_sheet_ref(env_value)


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log("Refreshing cached Google token...")
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET_FILE.exists():
                log(f"No OAuth client at {CLIENT_SECRET_FILE}.")
                log("Create a Desktop-app OAuth client in Google Cloud Console "
                    "(Sheets API enabled) and save its downloaded JSON there.")
                sys.exit(2)
            log("Opening a browser to authorize access to Google Sheets...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)
    return creds


def load_roster() -> dict:
    if not ROSTER_FILE.exists():
        log(f"{ROSTER_FILE} not found. Run fetch_roster.py first.")
        sys.exit(3)
    return json.loads(ROSTER_FILE.read_text())


def scout_sort_key(den: str, last: str, first: str) -> tuple:
    # "\uffff" parks den-less scouts at the end rather than the top.
    return (den or "\uffff", last, first)


def roster_row(s: dict, dues_cell: list[str]) -> list:
    parents = s.get("parents") or []
    return [
        s["first_name"], s["last_name"], s.get("den", ""), s.get("rank", ""),
        *dues_cell,
        s["bsa_id"], s.get("date_of_birth", ""), s.get("registration_expire", ""),
        s.get("registration_status", ""),
        "; ".join(p["name"] for p in parents),
        "; ".join(p.get("email", "") for p in parents if p.get("email")),
        "; ".join(p.get("phone", "") for p in parents if p.get("phone")),
    ]


def unregistered_row(payment: dict) -> list:
    """A paid scout who isn't in Scoutbook, as a roster row.

    Only the fields the payment form actually knows are filled: name, program
    level, and the payer as the guardian contact. BSA ID, birthday and
    registration dates stay blank because inventing them would make an
    unregistered scout look registered — which is exactly the thing someone
    reading this row needs to notice and fix.
    """
    return [
        payment["first_name"], payment["last_name"],
        fetch_dues.den_label(payment.get("den", "")), "",
        "Yes",
        "", "", "",
        f"Not in Scoutbook (dues paid {payment['paid_date']})",
        payment.get("payer", ""), payment.get("payer_email", ""),
        payment.get("payer_phone", ""),
    ]


def build_rows(roster: dict, dues: dict | None) -> tuple[list[list], list[str]]:
    """-> (rows, header). The header drops the dues column when there is no
    dues data, so the sheet never shows an unpaid-looking blank column."""
    header = list(HEADER) if dues else [h for h in HEADER if h != DUES_COLUMN]
    paid_ids: set[str] = set()
    extras: list[dict] = []
    if dues:
        match = fetch_dues.match_to_roster(dues["scout_payments"], roster["scouts"])
        paid_ids = {bsa_id for bsa_id, p in match["by_bsa_id"].items() if p["paid"]}
        extras = fetch_dues.unregistered_paid_scouts(match)
        for p in extras:
            log(f"Paid but not in Scoutbook, adding to the sheet: {p['first_name']} "
                f"{p['last_name']} (paid {p['paid_date']} by {p['payer']}, "
                f"{p['source']} row {p['row']})")
        for p in match["unmatched_payments"]:
            if p.get("reason_code") == "ambiguous":
                log(f"Dues: {p.get('source', '?')} row {p['row']}: {p['first_name']} "
                    f"{p['last_name']} (paid by {p['payer']}) — {p['reason']}")

    entries = [
        (scout_sort_key(s.get("den", ""), s["last_name"], s["first_name"]),
         roster_row(s, ["Yes" if s["bsa_id"] in paid_ids else "No"] if dues else []))
        for s in roster["scouts"]
    ]
    entries += [
        (scout_sort_key(fetch_dues.den_label(p.get("den", "")), p["last_name"], p["first_name"]),
         unregistered_row(p))
        for p in extras
    ]
    rows = [row for _, row in sorted(entries, key=lambda e: e[0])]

    title = f"Pack Roster — updated {roster.get('fetched_at', '')}"
    if dues:
        title += f" · dues from {fetch_dues.sources_label(dues)} as of {dues['fetched_at']}"
    return [[title], [], header, *rows], header


def resolve_target_tab(service, spreadsheet_id: str, gid: int | None, tab_name: str | None) -> dict:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    sheets = [s["properties"] for s in meta["sheets"]]
    if tab_name:
        for props in sheets:
            if props["title"] == tab_name:
                return props
        log(f"No tab named {tab_name!r} in this spreadsheet.")
        sys.exit(4)
    if gid is not None:
        for props in sheets:
            if props["sheetId"] == gid:
                return props
    return sheets[0]


def write_roster(service, spreadsheet_id: str, tab: dict, rows: list[list],
                 header: list[str]) -> None:
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
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": bold}, "fields": "userEnteredFormat.textFormat.bold",
            }},
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                          "endColumnIndex": n_cols},
                "cell": {"userEnteredFormat": bold}, "fields": "userEnteredFormat.textFormat.bold",
            }},
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 3}},
                "fields": "gridProperties.frozenRowCount",
            }},
        ]},
    ).execute()


def resolve_dues_specs(cli_values: list[str] | None, no_dues: bool) -> list[dict] | None:
    """Which dues sheets to read, or None to write the roster without the column."""
    if no_dues:
        return None
    if cli_values:
        return [fetch_dues.parse_sheet_spec(v) for v in cli_values]
    load_config_file()
    env_value = os.environ.get("DUES_SHEET_ID")
    if not env_value:
        log(f"No DUES_SHEET_ID in {ENV_FILE} and no --dues-sheet; writing the "
            f"roster without the {DUES_COLUMN!r} column.")
        log("Set DUES_SHEET_ID to this year's payment-form sheet to include it.")
        return None
    return [fetch_dues.parse_sheet_spec(v) for v in env_value.split(",") if v.strip()]


def build_service():
    try:
        from googleapiclient.discovery import build
    except ImportError:
        log("google-api-python-client not installed; run this script with `uv run` "
            "so its inline dependencies are picked up.")
        sys.exit(2)
    return build("sheets", "v4", credentials=get_credentials())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", help="Spreadsheet URL or ID (default: ROSTER_SHEET_ID config)")
    parser.add_argument("--tab", help="Tab/sheet name to overwrite (default: URL's gid, or first tab)")
    parser.add_argument("--dues-sheet", action="append", metavar="REF",
                        help="Dues payment sheet URL or ID, optionally '<ref>@YYYY-MM-DD' to keep "
                             "only submissions on or after that date. Repeatable. "
                             "(default: DUES_SHEET_ID config)")
    parser.add_argument("--no-dues", action="store_true",
                        help=f"Skip the {DUES_COLUMN!r} column even if a dues sheet is configured")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written, don't write")
    args = parser.parse_args()

    spreadsheet_id, gid = resolve_sheet_id(args.sheet)
    dues_specs = resolve_dues_specs(args.dues_sheet, args.no_dues)
    roster = load_roster()

    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        log("google-api-python-client not installed; run this script with `uv run` "
            "so its inline dependencies are picked up.")
        sys.exit(2)

    # The dues sheet is read even on a dry run — it is a read-only call, and a
    # preview that omitted the column would not be a preview of the real write.
    service = None
    dues = None
    if dues_specs:
        service = build_service()
        try:
            dues = fetch_dues.fetch_dues(service, dues_specs)
        except HttpError as e:
            log(f"Google Sheets API error reading the dues sheet: {e}")
            sys.exit(4)
        fetch_dues.DUES_FILE.write_text(json.dumps(dues, indent=2))
        fetch_dues.DUES_FILE.chmod(0o600)
        log(f"Dues: {dues['paid_submission_count']} paid submissions covering "
            f"{len(dues['scout_payments'])} scouts, from "
            f"{fetch_dues.sources_label(dues)}")

    rows, header = build_rows(roster, dues)

    if args.dry_run:
        log(f"Would write {len(rows) - 3} scouts to spreadsheet {spreadsheet_id}")
        for row in rows:
            print("\t".join(str(c) for c in row))
        return

    if service is None:
        service = build_service()

    try:
        tab = resolve_target_tab(service, spreadsheet_id, gid, args.tab)
        write_roster(service, spreadsheet_id, tab, rows, header)
    except HttpError as e:
        log(f"Google Sheets API error: {e}")
        sys.exit(4)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={tab['sheetId']}"
    log(f"Wrote {len(rows) - 3} scouts to {tab['title']!r}: {url}")
    print(url)


if __name__ == "__main__":
    main()
