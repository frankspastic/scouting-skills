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
fetch_dues.py — read the pack's dues-payment spreadsheet (the export of the
online registration/payment form) and work out which scouts' dues are paid.

The payment sheet is one row per *family submission*, not per scout: a payer's
contact info, then repeating "Scout N First Name / Last Name / Den" column
groups, a total, and a payment cell holding the PayPal JSON blob. Columns are
found by header text (e.g. "Scout 2 First Name|name-6-last-name" -> scout 2,
first name), not by position, because the form is rebuilt every year and the
field ids change with it.

Names are joined back to the roster tolerantly: parents type "Will" for
William and "Gabriella" for Gabriela. Exact matches are claimed first, then
each remaining payment name may take a same-last-name roster scout if one
candidate is a clear closest match (prefix or one typo away) — never when two
siblings are equally close. Everything unmatched is reported, not guessed.

More than one sheet can be read at once, because a program year does not
start cleanly: families who pay early go through last year's still-live form
and land in last year's sheet. A sheet reference may therefore carry a cutoff
— "<url-or-id>@2026-06-01" means "only submissions from this sheet on or after
that date" — so the tail of an old sheet can be folded in without dragging the
whole previous year's roster of payers with it.

Auth is the same user OAuth token the roster sheet sync uses
(~/.scouting-skills/google-sheets-token.json).

Output: ~/.scouting-skills/dues.json, plus a human summary on stdout.

Exit codes:
    0  success
    2  missing OAuth client / sheet id config
    4  Google API error (message printed to stderr)
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR", Path.home() / ".scouting-skills"))
ROSTER_FILE = DATA_DIR / "roster.json"
DUES_FILE = DATA_DIR / "dues.json"
ENV_FILE = DATA_DIR / "google-sheets.env"
CLIENT_SECRET_FILE = DATA_DIR / "google-oauth-client.json"
TOKEN_FILE = DATA_DIR / "google-sheets-token.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# How far a first name may be from a roster first name and still be considered
# the same kid (Gabriella -> Gabriela is 1). Kept tight on purpose: a wrong
# match marks the wrong family paid up.
MAX_FIRST_NAME_EDITS = 1
# Only used to rank candidates that already qualified above.
TIEBREAK_EDIT_CAP = 8


def log(msg: str) -> None:
    print(f"[fetch_dues] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# config / auth (mirrors sync_roster_to_sheet.py so each script stands alone)
# --------------------------------------------------------------------------

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


# Submission Date as the form writes it, plus the shapes a human might type
# into the config.
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y")


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_sheet_spec(ref: str) -> dict:
    """'<url-or-id>[@YYYY-MM-DD]' -> {'id', 'gid', 'since'}.

    The optional '@date' suffix keeps only submissions on or after that date —
    how last year's sheet contributes its June-onward tail without also
    contributing last year's payments.
    """
    ref = ref.strip()
    since = None
    head, sep, tail = ref.rpartition("@")
    if sep and head:
        parsed = parse_date(tail)
        if parsed:
            ref, since = head.strip(), parsed
    sheet_id, gid = parse_sheet_ref(ref)
    return {"id": sheet_id, "gid": gid, "since": since}


def resolve_dues_sheets(cli_values: list[str] | None) -> list[dict]:
    """CLI --sheet (repeatable) wins; otherwise DUES_SHEET_ID, which may list
    several comma-separated sheets."""
    if cli_values:
        return [parse_sheet_spec(v) for v in cli_values]
    load_config_file()
    env_value = os.environ.get("DUES_SHEET_ID")
    if not env_value:
        log(f"No --sheet given and DUES_SHEET_ID not set in {ENV_FILE}.")
        log("Dues sheets change every year — pass --sheet <url-or-id> for a "
            "one-off, or set DUES_SHEET_ID=<id> in that file for this year.")
        sys.exit(2)
    return [parse_sheet_spec(v) for v in env_value.split(",") if v.strip()]


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


# --------------------------------------------------------------------------
# parsing the payment sheet
# --------------------------------------------------------------------------

def header_label(raw: str) -> str:
    """'Scout 1 First Name|name-3-first-name' -> 'scout 1 first name'.

    The part after '|' is the form's internal field id, which is reshuffled
    whenever the form is rebuilt; only the human label is dependable.
    """
    return raw.split("|", 1)[0].strip().lower()


def map_columns(header: list[str]) -> dict:
    """Locate the columns we care about by header text."""
    cols: dict = {"scouts": {}, "payer_first": None, "payer_last": None,
                  "payer_email": None, "payer_phone": None, "payment": None,
                  "amount": None, "submitted": None, "submission_id": None}
    for idx, raw in enumerate(header):
        label = header_label(raw)
        m = re.match(r"scout\s*(\d+)\s+(first name|last name|den)", label)
        if m:
            n = int(m.group(1))
            key = {"first name": "first", "last name": "last", "den": "den"}[m.group(2)]
            cols["scouts"].setdefault(n, {})[key] = idx
            continue
        if label in ("first name",) and cols["payer_first"] is None:
            cols["payer_first"] = idx
        elif label in ("last name",) and cols["payer_last"] is None:
            cols["payer_last"] = idx
        elif "email" in label and cols["payer_email"] is None:
            cols["payer_email"] = idx
        elif "phone" in label and cols["payer_phone"] is None:
            cols["payer_phone"] = idx
        elif label in ("paypal", "payment", "payment status") or label.startswith("paypal"):
            cols["payment"] = idx
        elif "total" in label and "cost" in label:
            cols["amount"] = idx
        elif "submission date" in label or label in ("date", "timestamp"):
            cols["submitted"] = idx
        elif "submission id" in label:
            cols["submission_id"] = idx
    return cols


def cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def normalize_phone(raw: str) -> str:
    """Match the roster's 10-digit form: '(512) 466-1578' -> '5124661578'."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


# The form stores dens as slugs. These are program levels, not den units — the
# roster's own dens look like "Wolf Den 4", and which numbered den a new scout
# joins isn't something the payment form knows.
DEN_LABELS = {
    "lion": "Lion", "tiger": "Tiger", "wolf": "Wolf", "bear": "Bear",
    "webelos": "Webelos", "webelos-1": "Webelos",
    "webelos-2": "Arrow of Light", "aol": "Arrow of Light",
    "arrow-of-light": "Arrow of Light",
}


def den_label(slug: str) -> str:
    """'webelos-2' -> 'Arrow of Light'; anything unrecognized is passed through
    so a renamed form option shows up as itself rather than vanishing."""
    slug = (slug or "").strip()
    return DEN_LABELS.get(slug.lower(), slug)


def parse_payment_cell(raw: str) -> tuple[bool, str, str]:
    """-> (paid, status, transaction_id).

    Blank means nobody paid through the form. A JSON blob is the PayPal
    receipt and only counts when it says COMPLETED. Anything else is treated
    as a note someone typed in by hand ("cash", "check #123") and counts as
    paid — the pack does take payment outside the form.
    """
    if not raw:
        return False, "", ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return True, raw, ""
    if not isinstance(data, dict):
        return True, raw, ""
    status = str(data.get("status", "")).strip()
    return status.upper() == "COMPLETED", status or "(no status)", str(data.get("transaction_id", ""))


def parse_payments(values: list[list[str]]) -> tuple[list[dict], list[dict]]:
    """-> (submissions, scout_payments). One submission per row; one scout
    payment per named scout inside it."""
    if not values:
        return [], []
    header, *rows = values
    cols = map_columns(header)
    if not cols["scouts"]:
        log("No 'Scout N First Name' columns found in this sheet's header — "
            "is it really the dues payment form export?")
        log(f"Header seen: {[header_label(h) for h in header]}")
        sys.exit(4)

    submissions, scout_payments = [], []
    for offset, row in enumerate(rows):
        row_num = offset + 2  # 1-based, past the header
        paid, status, txn = parse_payment_cell(cell(row, cols["payment"]))
        payer = " ".join(p for p in (cell(row, cols["payer_first"]),
                                     cell(row, cols["payer_last"])) if p)
        submitted = cell(row, cols["submitted"])
        named = []
        for n in sorted(cols["scouts"]):
            group = cols["scouts"][n]
            first = cell(row, group.get("first"))
            last = cell(row, group.get("last"))
            if not first and not last:
                continue  # unused slot — its den cell may still hold a default
            named.append({
                "first_name": first,
                "last_name": last or cell(row, cols["payer_last"]),
                "den": cell(row, group.get("den")),
            })
        if not named and not paid and not payer:
            continue  # trailing blank row
        submission = {
            "row": row_num,
            "payer": payer,
            "payer_email": cell(row, cols["payer_email"]),
            "payer_phone": normalize_phone(cell(row, cols["payer_phone"])),
            "paid": paid,
            "status": status,
            "transaction_id": txn,
            "amount": cell(row, cols["amount"]),
            "submitted": submitted,
            "submission_id": cell(row, cols["submission_id"]),
            "scouts": named,
        }
        submissions.append(submission)
        for scout in named:
            scout_payments.append({**scout, "paid": paid, "status": status,
                                   "amount": submission["amount"],
                                   "paid_date": submitted, "payer": payer,
                                   "payer_email": submission["payer_email"],
                                   "payer_phone": submission["payer_phone"],
                                   "row": row_num})
    return submissions, scout_payments


# --------------------------------------------------------------------------
# matching payment names to roster names
# --------------------------------------------------------------------------

def normalize_name(value: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]+", "", stripped.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def first_token(value: str) -> str:
    """Given names only — parents write 'Mary Kate' where the roster has 'Mary'."""
    return normalize_name(value).split(" ")[0] if normalize_name(value) else ""


def edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein, short-circuiting once it exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def first_name_distance(payment_first: str, roster_first: str) -> tuple[int, int] | None:
    """How far apart two given names are, or None if they're not the same name.

    Returns (tier, edits): tier 0 identical, tier 1 a nickname/prefix ('Will'
    for 'William') or a single typo. `edits` is the raw character distance and
    only breaks ties — 'Gabriella' is a prefix of neither sibling but is one
    edit from Gabriela and two from Gabriel, so Gabriela wins.
    """
    p, r = first_token(payment_first), first_token(roster_first)
    if not p or not r:
        return None
    if p == r:
        return (0, 0)
    raw = edit_distance(p, r, TIEBREAK_EDIT_CAP)
    if len(p) >= 3 and len(r) >= 3 and (p.startswith(r) or r.startswith(p)):
        return (1, raw)
    return (1, raw) if raw <= MAX_FIRST_NAME_EDITS else None


def match_to_roster(scout_payments: list[dict], roster_scouts: list[dict]) -> dict:
    """Attach each payment to a roster scout by BSA id.

    Two passes: exact name matches claim their scout first, then the leftovers
    get a fuzzy pass restricted to same-last-name scouts, and only when one
    candidate is strictly closer than the rest. Ties are left unmatched — with
    siblings like Gabriel and Gabriela on the roster, a coin flip would mark
    the wrong kid paid.
    """
    by_id = {s["bsa_id"]: s for s in roster_scouts}
    claimed: dict[str, dict] = {}   # bsa_id -> payment
    unmatched: list[dict] = []
    superseded: list[dict] = []     # duplicate submissions for an already-matched scout

    def exact_key(first: str, last: str) -> tuple[str, str]:
        return first_token(first), normalize_name(last)

    exact_index: dict[tuple[str, str], list[dict]] = {}
    for s in roster_scouts:
        exact_index.setdefault(exact_key(s["first_name"], s["last_name"]), []).append(s)

    leftovers = []
    # Paid submissions claim their scout ahead of unpaid ones, and the most
    # recent paid submission ahead of older ones: a family whose first attempt
    # failed and who paid on the retry must not be left looking unpaid.
    ordered = sorted(scout_payments, key=lambda p: (not p["paid"], -p.get("row", 0)))
    for payment in ordered:
        candidates = [s for s in exact_index.get(exact_key(payment["first_name"], payment["last_name"]), [])
                      if s["bsa_id"] not in claimed]
        if len(candidates) == 1:
            claimed[candidates[0]["bsa_id"]] = payment
        else:
            leftovers.append(payment)

    for payment in leftovers:
        last = normalize_name(payment["last_name"])
        candidates = []
        for s in roster_scouts:
            if normalize_name(s["last_name"]) != last:
                continue
            d = first_name_distance(payment["first_name"], s["first_name"])
            if d is not None:
                candidates.append((d, s))
        free = sorted((c for c in candidates if c[1]["bsa_id"] not in claimed),
                      key=lambda t: t[0])
        if not free:
            if candidates:
                # Every scout this could be is already spoken for — a family
                # that submitted twice (failed attempt, then a successful one).
                superseded.append(payment)
            else:
                unmatched.append({**payment, "reason_code": "not_on_roster",
                                  "reason": "no roster scout with that name"})
            continue
        if len(free) == 1 or free[0][0] < free[1][0]:
            claimed[free[0][1]["bsa_id"]] = payment
        else:
            unmatched.append({**payment, "reason_code": "ambiguous",
                              "reason": "ambiguous — two roster scouts match equally well"})

    return {
        "by_bsa_id": claimed,
        "unmatched_payments": unmatched,
        "superseded_payments": superseded,
        "unpaid_roster_scouts": [
            {"first_name": s["first_name"], "last_name": s["last_name"], "den": s.get("den", ""),
             "bsa_id": s["bsa_id"]}
            for s in roster_scouts
            if not (claimed.get(s["bsa_id"]) or {}).get("paid")
        ],
        "roster_scouts_by_id": by_id,
    }


# --------------------------------------------------------------------------
# fetching / reporting
# --------------------------------------------------------------------------

def resolve_target_tab(service, spreadsheet_id: str, gid: int | None, tab_name: str | None) -> dict:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties"
    ).execute()
    sheets = [s["properties"] for s in meta["sheets"]]
    title = meta["properties"]["title"]
    if tab_name:
        for props in sheets:
            if props["title"] == tab_name:
                return {**props, "spreadsheet_title": title}
        log(f"No tab named {tab_name!r} in this spreadsheet.")
        sys.exit(4)
    if gid is not None:
        for props in sheets:
            if props["sheetId"] == gid:
                return {**props, "spreadsheet_title": title}
    return {**sheets[0], "spreadsheet_title": title}


def fetch_dues(service, specs: list[dict], tab_name: str | None = None) -> dict:
    """Read every configured payment sheet and return the merged dues payload
    (also written to dues.json by main()). Shared with sync_roster_to_sheet.py."""
    sources, submissions, scout_payments = [], [], []
    for spec in specs:
        tab = resolve_target_tab(service, spec["id"], spec["gid"], tab_name)
        values = service.spreadsheets().values().get(
            spreadsheetId=spec["id"], range=f"'{tab['title']}'",
        ).execute().get("values", [])
        subs, pays = parse_payments(values)
        label = tab["spreadsheet_title"]

        since = spec["since"]
        kept, too_old, undated = [], 0, 0
        for sub in subs:
            if since:
                submitted = parse_date(sub["submitted"])
                if submitted is None:
                    # With a cutoff in force, a row we cannot date is left out
                    # rather than assumed current — the whole point of the
                    # cutoff is to not import last year's payers.
                    undated += 1
                    continue
                if submitted < since:
                    too_old += 1
                    continue
            sub["source"] = label
            kept.append(sub)
        kept_rows = {sub["row"] for sub in kept}
        for pay in pays:
            if pay["row"] in kept_rows:
                pay["source"] = label
                scout_payments.append(pay)
        submissions.extend(kept)

        if undated:
            log(f"{label}: skipped {undated} row(s) with an unreadable "
                f"Submission Date while filtering on or after {since}")
        sources.append({
            "spreadsheet_id": spec["id"],
            "spreadsheet_title": label,
            "tab": tab["title"],
            "url": f"https://docs.google.com/spreadsheets/d/{spec['id']}/edit#gid={tab['sheetId']}",
            "since": since.isoformat() if since else None,
            "submissions_in_sheet": len(subs),
            "submissions_used": len(kept),
            "skipped_before_cutoff": too_old,
            "skipped_undated": undated,
        })

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "submission_count": len(submissions),
        "paid_submission_count": sum(1 for s in submissions if s["paid"]),
        "scout_payments": scout_payments,
        "submissions": submissions,
    }


def unregistered_paid_scouts(match: dict) -> list[dict]:
    """Scouts whose dues are paid but who aren't in Scoutbook at all.

    Every year a family pays through the form before the pack registers them
    (or the registration is still in a leader's inbox), and they'd otherwise be
    invisible on the roster despite having paid. Only unambiguous misses count:
    an 'ambiguous' payment is a scout already on the roster under a name we
    couldn't pin down, and duplicating them would be worse than leaving it.
    """
    seen, out = set(), []
    for p in match["unmatched_payments"]:
        if not p["paid"] or p.get("reason_code") != "not_on_roster":
            continue
        key = (first_token(p["first_name"]), normalize_name(p["last_name"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def sources_label(dues: dict) -> str:
    """'2026/27 Dues Payments + 2024/25 Dues Payments (since 2025-06-01)'"""
    parts = []
    for src in dues["sources"]:
        part = src["spreadsheet_title"]
        if src["since"]:
            part += f" (since {src['since']})"
        parts.append(part)
    return " + ".join(parts)


def load_roster() -> dict | None:
    if not ROSTER_FILE.exists():
        return None
    return json.loads(ROSTER_FILE.read_text())


def print_summary(dues: dict, roster: dict | None) -> None:
    print("Dues sheets:")
    for src in dues["sources"]:
        window = f", on/after {src['since']}" if src["since"] else ""
        detail = f"{src['submissions_used']} of {src['submissions_in_sheet']} submissions used{window}"
        print(f"  {src['spreadsheet_title']} / {src['tab']}: {detail}")
    print(f"  total: {dues['paid_submission_count']} paid of {dues['submission_count']} "
          f"submissions, covering {len(dues['scout_payments'])} scouts")
    if not roster:
        print(f"\n(No {ROSTER_FILE} yet — run fetch_roster.py to see who still owes.)")
        return

    match = match_to_roster(dues["scout_payments"], roster["scouts"])
    paid_ids = {bid for bid, p in match["by_bsa_id"].items() if p["paid"]}
    print(f"\nRoster ({roster['scout_count']} scouts, fetched {roster['fetched_at']}):")
    print(f"  paid:   {len(paid_ids)}")
    print(f"  unpaid: {len(match['unpaid_roster_scouts'])}")

    by_den: dict[str, list[str]] = {}
    for s in match["unpaid_roster_scouts"]:
        by_den.setdefault(s["den"] or "(no den)", []).append(f"{s['first_name']} {s['last_name']}")
    if by_den:
        print("\nNot yet paid:")
        for den in sorted(by_den):
            print(f"  {den}: {', '.join(sorted(by_den[den]))}")

    extras = unregistered_paid_scouts(match)
    if extras:
        print("\nPaid but not in Scoutbook (sync_roster_to_sheet.py adds these to the sheet):")
        for p in extras:
            print(f"  {p['first_name']} {p['last_name']} ({den_label(p['den']) or 'no den'}) "
                  f"— paid {p['paid_date']} by {p['payer']} <{p['payer_email']}>, "
                  f"{p['source']} row {p['row']}")
    ambiguous = [p for p in match["unmatched_payments"] if p.get("reason_code") == "ambiguous"]
    if ambiguous:
        print("\nPayments that could be either of two roster scouts (resolve by hand):")
        for p in ambiguous:
            print(f"  {p.get('source', '?')} row {p['row']}: {p['first_name']} {p['last_name']} "
                  f"(paid by {p['payer']}) — {p['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", action="append", metavar="REF",
                        help="Dues spreadsheet URL or ID, optionally '<ref>@YYYY-MM-DD' to keep "
                             "only submissions on or after that date. Repeatable. "
                             "(default: DUES_SHEET_ID config)")
    parser.add_argument("--tab", help="Tab name (default: URL's gid, or first tab)")
    parser.add_argument("--json", action="store_true", help="Print the full dues JSON instead of a summary")
    args = parser.parse_args()

    specs = resolve_dues_sheets(args.sheet)

    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        log("google-api-python-client not installed; run this script with `uv run` "
            "so its inline dependencies are picked up.")
        sys.exit(2)

    service = build("sheets", "v4", credentials=get_credentials())
    try:
        dues = fetch_dues(service, specs, args.tab)
    except HttpError as e:
        log(f"Google Sheets API error: {e}")
        sys.exit(4)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DUES_FILE.write_text(json.dumps(dues, indent=2))
    DUES_FILE.chmod(0o600)
    log(f"Wrote {DUES_FILE}")

    if args.json:
        print(json.dumps(dues, indent=2))
    else:
        print_summary(dues, load_roster())


if __name__ == "__main__":
    main()
