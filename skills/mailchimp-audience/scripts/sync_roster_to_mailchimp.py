#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""
sync_roster_to_mailchimp.py — push the cached pack roster (and dues status)
into the Mailchimp audience, one member per parent/guardian email.

This replaces the Scoutbook->Mailchimp half of
~/Sites/mailchimp-scout/scout_mailchimp_sync.py, which can no longer run: it
logs into Scoutbook with a username and password, and Scouting America now puts
a CAPTCHA in front of that (the pack-roster skill exists because of this), its
.env names the credentials MYSCOUTING_* while the script requires SCOUTBOOK_*,
and it writes merge fields called "Scout 1"/"ADDRESS" which are not tags on
this audience. Here the roster comes from fetch_roster.py's cache instead, and
the merge field tags are the ones the audience actually has.

What it writes, per parent email:

    FNAME, LNAME     only when Mailchimp's value is empty — see below
    SCOUT{n}NAME     "First Last"
    SCOUT{n}DEN      program level: Lion/Tiger/Wolf/Bear/Webelos/AOL
    SCOUT{n}EXP      registration expiration, M/D/YYYY
    SCOUT{n}BDAY     date of birth, M/D/YYYY
    SCOUT{n}PAID     Yes/No from the dues sheets (fetch_dues.py)
    SCOUT{n}RNEW     renewal status from the roster (Current / Eligible to Renew /
                     Renewed / Opted Out / ...) — distinct from SCOUT{n}EXP, which
                     is only the date the current registration runs out
    SCOUT{n}BSAI     BSA member ID, from the roster (tag is "BSAI" not "BSAID" —
                     Mailchimp truncates merge tags to 10 characters)

for n in 1..3 — the audience has three scout slots. Names are never
overwritten: Mailchimp holds what a parent typed about themselves, while the
roster holds a single "name" string that only splits naively (it would turn
"Mary Jane Smith" into FNAME "Mary" / LNAME "Jane Smith", and on a
two-parent household it would replace one parent's name with the other's).
Empty ones are filled in.

Existing members' subscription status is never touched: unsubscribed people
stay unsubscribed and simply get their merge fields corrected. Only brand-new
emails get a status, from --new-member-status (default "subscribed"), and
--no-create skips creating them at all.

--tag NAME (default "existing") makes sure every roster member carries that
tag, adding it only where it is missing. Tag names are case-sensitive in
Mailchimp, so this matches the audience's existing spelling exactly rather
than creating a near-duplicate tag; --no-tag turns it off.

Audience members with no roster match — alumni, prospects, past families — are
left completely alone.

Dry run by default. Pass --live to actually write.

Exit codes:
    0  success
    2  missing credentials / missing roster cache
    4  Mailchimp API error (message printed to stderr)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mailchimp_audience import (  # noqa: E402  (same-directory helper)
    get_client,
    request,
    subscriber_hash,
)

DATA_DIR = Path(os.environ.get("SCOUTING_SKILLS_DATA_DIR", Path.home() / ".scouting-skills"))
ROSTER_FILE = DATA_DIR / "roster.json"
DUES_FILE = DATA_DIR / "dues.json"

MAX_SCOUT_SLOTS = 3
NAME_FIELDS = ("FNAME", "LNAME")

# Roster dens are unit names ("Aol Den 6 (Male)", "Bear Den 10"); the audience
# has always held the program level, which is what reads well in an email.
DEN_LEVELS = (
    ("aol", "AOL"),
    ("arrow of light", "AOL"),
    ("webelos", "Webelos"),
    ("bear", "Bear"),
    ("wolf", "Wolf"),
    ("tiger", "Tiger"),
    ("lion", "Lion"),
)


def log(msg: str) -> None:
    print(f"[sync_roster_to_mailchimp] {msg}", file=sys.stderr, flush=True)


def den_level(scout: dict) -> str:
    den = (scout.get("den") or "").lower()
    for key, label in DEN_LEVELS:
        if key in den:
            return label
    # A scout with no den yet still has a rank, which is the same vocabulary.
    return scout.get("rank") or ""


def mdy(iso: str) -> str:
    """'2026-12-31' -> '12/31/2026', the format already in the audience."""
    if not iso or not re.match(r"^\d{4}-\d{2}-\d{2}", iso):
        return ""
    year, month, day = iso[:10].split("-")
    return f"{int(month)}/{int(day)}/{year}"


def load_json(path: Path, what: str, required: bool) -> dict | None:
    if not path.exists():
        if required:
            log(f"{path} not found — run the pack-roster skill's {what} first.")
            sys.exit(2)
        return None
    return json.loads(path.read_text())


def family_scouts(roster: dict) -> dict[str, dict]:
    """Group roster scouts by each guardian email (both parents get a member —
    that's how the audience has always been built).

    A scout is added to a given email at most once. Scoutbook often carries
    the same guardian twice on one scout (two person records, same address),
    and without this the scout is appended once per duplicate — burning a
    SCOUT{n} slot on a copy of themselves and, for a family with a real
    second scout, pushing that sibling out of the audience entirely.
    """
    families: dict[str, dict] = {}
    seen: dict[str, set] = {}
    for scout in roster["scouts"]:
        # bsa_id identifies a roster scout; fall back to the name for scouts
        # that have none yet (the paid-but-not-registered ones added later).
        key = scout.get("bsa_id") or (scout["first_name"], scout["last_name"])
        for parent in scout.get("parents") or []:
            email = (parent.get("email") or "").strip().lower()
            if not email:
                continue
            fam = families.setdefault(email, {"parent": parent, "scouts": []})
            if key in seen.setdefault(email, set()):
                continue
            seen[email].add(key)
            fam["scouts"].append(scout)
    return families


def desired_fields(fam: dict, paid_ids: set[str], current: dict) -> tuple[dict, int]:
    parent_name = (fam["parent"].get("name") or "").split()
    fields = {}
    if parent_name:
        if not (current.get("FNAME") or "").strip():
            fields["FNAME"] = parent_name[0]
        if len(parent_name) > 1 and not (current.get("LNAME") or "").strip():
            fields["LNAME"] = " ".join(parent_name[1:])

    scouts = sorted(fam["scouts"], key=lambda s: (s["last_name"], s["first_name"]))
    for slot in range(1, MAX_SCOUT_SLOTS + 1):
        scout = scouts[slot - 1] if len(scouts) >= slot else None
        prefix = f"SCOUT{slot}"
        if scout is None:
            fields[f"{prefix}NAME"] = ""
            fields[f"{prefix}DEN"] = ""
            fields[f"{prefix}EXP"] = ""
            fields[f"{prefix}BDAY"] = ""
            fields[f"{prefix}PAID"] = ""
            fields[f"{prefix}BSAI"] = ""
            fields[f"{prefix}RNEW"] = ""
            continue
        fields[f"{prefix}NAME"] = f"{scout['first_name']} {scout['last_name']}"
        fields[f"{prefix}DEN"] = scout.get("den_level") or den_level(scout)
        fields[f"{prefix}EXP"] = mdy(scout.get("registration_expire", ""))
        fields[f"{prefix}BDAY"] = mdy(scout.get("date_of_birth", ""))
        fields[f"{prefix}BSAI"] = scout.get("bsa_id") or ""
        fields[f"{prefix}RNEW"] = scout.get("renewal_status") or ""
        if scout.get("paid_override") is not None:
            fields[f"{prefix}PAID"] = "Yes" if scout["paid_override"] else "No"
        else:
            fields[f"{prefix}PAID"] = "Yes" if scout["bsa_id"] in paid_ids else "No"
    return fields, len(scouts)


def fetch_members(session, base_url, audience_id) -> dict[str, dict]:
    members, offset = {}, 0
    while True:
        data = request(
            session, "GET", f"{base_url}/lists/{audience_id}/members",
            params={"count": 1000, "offset": offset,
                    "fields": "members.email_address,members.status,"
                              "members.merge_fields,members.tags,total_items"},
        )
        batch = data.get("members", [])
        for m in batch:
            members[m["email_address"].strip().lower()] = {
                "status": m["status"],
                "merge_fields": m.get("merge_fields", {}),
                "tags": [t["name"] for t in m.get("tags", [])],
            }
        offset += len(batch)
        if not batch or offset >= data.get("total_items", 0):
            return members


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="Actually write to Mailchimp (default: dry run)")
    parser.add_argument("--no-create", action="store_true",
                        help="Only update people already on the list; never add anyone")
    parser.add_argument("--new-member-status", default="subscribed",
                        choices=["subscribed", "pending"],
                        help="Status for emails not yet on the list (default: subscribed; "
                             "'pending' sends Mailchimp's confirmation opt-in email)")
    parser.add_argument("--include-unregistered", action="store_true",
                        help="Also add payers whose scouts aren't in Scoutbook yet, using "
                             "the dues form's names (off by default — they have not been "
                             "registered by the pack)")
    parser.add_argument("--tag", default="existing",
                        help="Tag every roster member with this (default: 'existing'); "
                             "case-sensitive, must match the audience's spelling")
    parser.add_argument("--no-tag", action="store_true",
                        help="Don't touch tags at all")
    parser.add_argument("--no-dues", action="store_true",
                        help="Leave SCOUT*PAID alone instead of filling it from dues.json")
    args = parser.parse_args()

    roster = load_json(ROSTER_FILE, "fetch_roster.py", required=True)
    dues = None if args.no_dues else load_json(DUES_FILE, "fetch_dues.py", required=False)

    paid_ids: set[str] = set()
    extras: list[dict] = []
    if dues:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pack-roster" / "scripts"))
        import fetch_dues  # noqa: E402

        match = fetch_dues.match_to_roster(dues["scout_payments"], roster["scouts"])
        paid_ids = {bsa_id for bsa_id, p in match["by_bsa_id"].items() if p["paid"]}
        extras = fetch_dues.unregistered_paid_scouts(match)
        log(f"Dues: {len(paid_ids)} roster scouts paid, {len(extras)} paid but not in Scoutbook")

    families = family_scouts(roster)

    if args.include_unregistered and extras:
        # Group the payment-only scouts under the payer's email, shaped like a
        # roster family so they run through exactly the same field builder.
        for payment in extras:
            email = (payment.get("payer_email") or "").strip().lower()
            if not email:
                continue
            fam = families.setdefault(email, {
                "parent": {"name": payment.get("payer", "")}, "scouts": []})
            fam["scouts"].append({
                "first_name": payment["first_name"],
                "last_name": payment["last_name"],
                "bsa_id": "",
                "den": "",
                "den_level": fetch_dues.den_label(payment.get("den", "")),
                "rank": "",
                "registration_expire": "",
                "date_of_birth": "",
                "paid_override": True,
            })

    session, base_url, audience_id = get_client()
    members = fetch_members(session, base_url, audience_id)
    log(f"Audience has {len(members)} members; roster has {len(families)} parent emails")

    tag_name = None if args.no_tag else args.tag
    needs_tag: list[str] = []
    updates, creates, skipped_creates, unchanged, overflow = [], [], [], [], []
    for email in sorted(families):
        fam = families[email]
        existing = members.get(email)
        current = existing["merge_fields"] if existing else {}
        fields, n_scouts = desired_fields(fam, paid_ids, current)
        if n_scouts > MAX_SCOUT_SLOTS:
            overflow.append((email, n_scouts))
        if existing is None:
            (skipped_creates if args.no_create else creates).append((email, fields))
            continue
        if tag_name and tag_name not in existing["tags"]:
            needs_tag.append(email)
        delta = {k: v for k, v in fields.items() if (current.get(k) or "") != (v or "")}
        if delta:
            updates.append((email, existing["status"], fields, delta))
        else:
            unchanged.append(email)

    print(f"{'LIVE' if args.live else 'DRY RUN'}: "
          f"{len(updates)} to update, {len(creates)} to create, "
          f"{len(unchanged)} with no field changes, "
          f"{len(members) - len(set(members) & set(families))} audience members untouched")
    if tag_name:
        print(f"  tag {tag_name!r}: {len(needs_tag)} existing member(s) missing it, "
              f"{len(creates)} new member(s) will get it")
    for email, status, _fields, delta in updates:
        print(f"  update {email} [{status}]")
        for key in sorted(delta):
            print(f"      {key}: {(members[email]['merge_fields'].get(key) or '')!r} -> {delta[key]!r}")
    for email, fields in creates:
        print(f"  create {email} [{args.new_member_status}] "
              f"{fields.get('FNAME','')} {fields.get('LNAME','')} — {fields.get('SCOUT1NAME','')}")
    for email, fields in skipped_creates:
        print(f"  skip   {email} (not on the list; --no-create)")
    for email in needs_tag:
        print(f"  tag    {email} += {tag_name!r}")
    for email, n in overflow:
        print(f"  NOTE   {email} has {n} scouts; only the first {MAX_SCOUT_SLOTS} fit the audience's fields")

    if not args.live:
        print("\nNothing was written. Re-run with --live to apply.")
        return

    written = 0
    for email, _status, fields, _delta in updates:
        # No "status" key: an unsubscribed member stays unsubscribed.
        request(session, "PUT", f"{base_url}/lists/{audience_id}/members/{subscriber_hash(email)}",
                json={"email_address": email, "status_if_new": args.new_member_status,
                      "merge_fields": fields})
        written += 1
    created = 0
    for email, fields in creates:
        request(session, "PUT", f"{base_url}/lists/{audience_id}/members/{subscriber_hash(email)}",
                json={"email_address": email, "status_if_new": args.new_member_status,
                      "merge_fields": fields})
        created += 1
    tagged = 0
    if tag_name:
        for email in needs_tag + [e for e, _ in creates]:
            request(session, "POST",
                    f"{base_url}/lists/{audience_id}/members/{subscriber_hash(email)}/tags",
                    json={"tags": [{"name": tag_name, "status": "active"}]})
            tagged += 1
    log(f"Updated {written} members, created {created}, tagged {tagged}")
    print(f"\nDone: {written} updated, {created} created, {tagged} tagged {tag_name!r}.")


if __name__ == "__main__":
    main()
