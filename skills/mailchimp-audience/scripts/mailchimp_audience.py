#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""
mailchimp_audience.py — read and maintain the pack's Mailchimp audience
directly via the Mailchimp Marketing API (no browser needed — it's a plain
REST API with key auth).

Credentials (MAILCHIMP_API_KEY, MAILCHIMP_SERVER_PREFIX, MAILCHIMP_AUDIENCE_ID)
are read from the environment, falling back to ~/.scouting-skills/mailchimp.env
(KEY=VALUE lines) if not already set — same credentials scout_mailchimp_sync.py
uses, so this operates on the same audience.

Subcommands:
    summary                          audience-level stats (counts, rates)
    tags                             tags in use, with member counts
    list [--status S] [--tag T]      list members (paginated)
    get --email E                    one member's status/merge fields/tags
    search --query Q                 free-text search across the audience
    upsert --email E [...]           add or update a member (status,
                                      merge fields, tags)
    tag --email E [--add ...] [--remove ...]   add/remove tags on a member
    unsubscribe --email E            set a member's status to unsubscribed

Exit codes:
    0  success
    2  missing credentials
    4  Mailchimp API error (message printed to stderr)
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

CONFIG_FILE = Path(
    os.environ.get("SCOUTING_SKILLS_DATA_DIR", Path.home() / ".scouting-skills")
) / "mailchimp.env"


def log(msg: str) -> None:
    print(f"[mailchimp_audience] {msg}", file=sys.stderr, flush=True)


def load_config_file() -> None:
    if not CONFIG_FILE.exists():
        return
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def get_client():
    load_config_file()
    api_key = os.environ.get("MAILCHIMP_API_KEY")
    server = os.environ.get("MAILCHIMP_SERVER_PREFIX")
    audience_id = os.environ.get("MAILCHIMP_AUDIENCE_ID")
    missing = [
        name
        for name, val in (
            ("MAILCHIMP_API_KEY", api_key),
            ("MAILCHIMP_SERVER_PREFIX", server),
            ("MAILCHIMP_AUDIENCE_ID", audience_id),
        )
        if not val
    ]
    if missing:
        log(f"Missing config: {missing}")
        log(f"Set them in the environment or in {CONFIG_FILE}")
        sys.exit(2)
    base_url = f"https://{server}.api.mailchimp.com/3.0"
    session = requests.Session()
    session.auth = ("anystring", api_key)
    return session, base_url, audience_id


def request(session, method: str, url: str, **kwargs):
    resp = session.request(method, url, timeout=30, **kwargs)
    if not resp.ok:
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except ValueError:
            pass
        log(f"{method} {url} -> {resp.status_code}: {detail}")
        sys.exit(4)
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def subscriber_hash(email: str) -> str:
    return hashlib.md5(email.strip().lower().encode()).hexdigest()


def emit(obj) -> None:
    print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_summary(session, base_url, audience_id, args) -> None:
    data = request(session, "GET", f"{base_url}/lists/{audience_id}")
    stats = data.get("stats", {})
    emit({
        "list_name": data.get("name"),
        "member_count": stats.get("member_count"),
        "unsubscribe_count": stats.get("unsubscribe_count"),
        "cleaned_count": stats.get("cleaned_count"),
        "pending_count": stats.get("pending_count"),
        "avg_open_rate": stats.get("open_rate"),
        "avg_click_rate": stats.get("click_rate"),
        "campaign_count": data.get("stats", {}).get("campaign_count"),
    })


def cmd_tags(session, base_url, audience_id, args) -> None:
    data = request(
        session, "GET", f"{base_url}/lists/{audience_id}/segments",
        params={"type": "static", "count": 1000},
    )
    tags = [
        {"name": seg["name"], "member_count": seg["member_count"]}
        for seg in data.get("segments", [])
    ]
    tags.sort(key=lambda t: t["name"])
    emit(tags)


def cmd_fields(session, base_url, audience_id, args) -> None:
    data = request(
        session, "GET", f"{base_url}/lists/{audience_id}/merge-fields",
        params={"count": 1000},
    )
    fields = [
        {
            "tag": f["tag"],
            "name": f["name"],
            "type": f["type"],
            "required": f["required"],
            "default_value": f.get("default_value", ""),
        }
        for f in data.get("merge_fields", [])
    ]
    emit(fields)


def cmd_add_field(session, base_url, audience_id, args) -> None:
    """Create a merge field on the audience. Idempotent by tag: an existing
    field with the same tag is left alone rather than duplicated, since
    Mailchimp happily creates a second field with the same name and the
    member data then splits across the two."""
    existing = request(
        session, "GET", f"{base_url}/lists/{audience_id}/merge-fields",
        params={"count": 1000},
    ).get("merge_fields", [])
    by_tag = {f["tag"]: f for f in existing}

    if args.tag in by_tag:
        f = by_tag[args.tag]
        log(f"Merge field {args.tag} already exists ({f['name']!r}, {f['type']}) — leaving it alone")
        emit({"tag": f["tag"], "name": f["name"], "type": f["type"], "created": False})
        return

    body = {"tag": args.tag, "name": args.name, "type": args.type, "required": False}
    if args.public is False:
        body["public"] = False
    data = request(
        session, "POST", f"{base_url}/lists/{audience_id}/merge-fields", json=body
    )
    log(f"Created merge field {data['tag']} ({data['name']!r}, {data['type']})")
    emit({"tag": data["tag"], "name": data["name"], "type": data["type"], "created": True})


def cmd_list(session, base_url, audience_id, args) -> None:
    params = {
        "count": args.limit,
        "offset": args.offset,
        "fields": "members.email_address,members.status,members.merge_fields,"
                  "members.tags,members.last_changed,total_items",
    }
    if args.status:
        params["status"] = args.status
    data = request(
        session, "GET", f"{base_url}/lists/{audience_id}/members", params=params
    )
    members = data.get("members", [])
    if args.tag:
        members = [
            m for m in members
            if any(t["name"].lower() == args.tag.lower() for t in m.get("tags", []))
        ]
    emit({
        "total_items": data.get("total_items"),
        "returned": len(members),
        "members": [
            {
                "email": m["email_address"],
                "status": m["status"],
                "merge_fields": m.get("merge_fields", {}),
                "tags": [t["name"] for t in m.get("tags", [])],
            }
            for m in members
        ],
    })


def cmd_get(session, base_url, audience_id, args) -> None:
    h = subscriber_hash(args.email)
    data = request(session, "GET", f"{base_url}/lists/{audience_id}/members/{h}")
    emit({
        "email": data["email_address"],
        "status": data["status"],
        "merge_fields": data.get("merge_fields", {}),
        "tags": [t["name"] for t in data.get("tags", [])],
        "last_changed": data.get("last_changed"),
    })


def cmd_search(session, base_url, audience_id, args) -> None:
    data = request(
        session, "GET", f"{base_url}/search-members",
        params={"query": args.query, "list_id": audience_id},
    )
    exact = data.get("exact_matches", {}).get("members", [])
    fuzzy = data.get("full_search", {}).get("members", [])
    seen = {m["id"] for m in exact}
    results = exact + [m for m in fuzzy if m["id"] not in seen]
    emit([
        {
            "email": m["email_address"],
            "status": m["status"],
            "merge_fields": m.get("merge_fields", {}),
        }
        for m in results
    ])


def cmd_upsert(session, base_url, audience_id, args) -> None:
    h = subscriber_hash(args.email)
    body = {
        "email_address": args.email,
        "status_if_new": args.status or "subscribed",
    }
    if args.status:
        body["status"] = args.status
    if args.merge_fields:
        body["merge_fields"] = json.loads(args.merge_fields)
    data = request(
        session, "PUT", f"{base_url}/lists/{audience_id}/members/{h}", json=body
    )
    if args.tags:
        tag_names = [t.strip() for t in args.tags.split(",") if t.strip()]
        request(
            session, "POST", f"{base_url}/lists/{audience_id}/members/{h}/tags",
            json={"tags": [{"name": t, "status": "active"} for t in tag_names]},
        )
    log(f"Upserted {args.email} (status: {data.get('status')})")
    emit({"email": data["email_address"], "status": data.get("status")})


def cmd_tag(session, base_url, audience_id, args) -> None:
    if not args.add and not args.remove:
        log("Nothing to do — pass --add and/or --remove")
        sys.exit(2)
    h = subscriber_hash(args.email)
    tags = []
    for t in (args.add or "").split(","):
        if t.strip():
            tags.append({"name": t.strip(), "status": "active"})
    for t in (args.remove or "").split(","):
        if t.strip():
            tags.append({"name": t.strip(), "status": "inactive"})
    request(
        session, "POST", f"{base_url}/lists/{audience_id}/members/{h}/tags",
        json={"tags": tags},
    )
    log(f"Updated tags for {args.email}: {tags}")


def cmd_unsubscribe(session, base_url, audience_id, args) -> None:
    h = subscriber_hash(args.email)
    data = request(
        session, "PATCH", f"{base_url}/lists/{audience_id}/members/{h}",
        json={"status": "unsubscribed"},
    )
    log(f"Unsubscribed {args.email} (status: {data.get('status')})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="audience-level stats")
    sub.add_parser("tags", help="tags in use, with member counts")
    sub.add_parser("fields", help="merge field definitions (name/type/required)")

    p = sub.add_parser("add-field", help="create a merge field on the audience")
    p.add_argument("--tag", required=True,
                   help="merge tag, max 10 chars (e.g. SCOUT1RNEW)")
    p.add_argument("--name", required=True, help="display name (e.g. 'Scout 1 Renewal Status')")
    p.add_argument("--type", default="text",
                   choices=["text", "number", "address", "phone", "date",
                            "url", "imageurl", "radio", "dropdown", "birthday", "zip"],
                   help="field type (default: text)")
    p.add_argument("--public", action="store_true", default=False,
                   help="show on signup forms (default: hidden, like the other SCOUT* fields)")

    p = sub.add_parser("list", help="list members")
    p.add_argument("--status", choices=["subscribed", "unsubscribed", "cleaned", "pending"])
    p.add_argument("--tag")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--offset", type=int, default=0)

    p = sub.add_parser("get", help="one member's details")
    p.add_argument("--email", required=True)

    p = sub.add_parser("search", help="free-text search across the audience")
    p.add_argument("--query", required=True)

    p = sub.add_parser("upsert", help="add or update a member")
    p.add_argument("--email", required=True)
    p.add_argument("--status", choices=["subscribed", "unsubscribed", "cleaned", "pending"])
    p.add_argument("--merge-fields", help="JSON object, e.g. '{\"FNAME\":\"Jane\"}'")
    p.add_argument("--tags", help="comma-separated tags to set active")

    p = sub.add_parser("tag", help="add/remove tags on a member")
    p.add_argument("--email", required=True)
    p.add_argument("--add", help="comma-separated tags to add")
    p.add_argument("--remove", help="comma-separated tags to remove")

    p = sub.add_parser("unsubscribe", help="set a member's status to unsubscribed")
    p.add_argument("--email", required=True)

    args = ap.parse_args()
    session, base_url, audience_id = get_client()

    handlers = {
        "summary": cmd_summary,
        "tags": cmd_tags,
        "fields": cmd_fields,
        "add-field": cmd_add_field,
        "list": cmd_list,
        "get": cmd_get,
        "search": cmd_search,
        "upsert": cmd_upsert,
        "tag": cmd_tag,
        "unsubscribe": cmd_unsubscribe,
    }
    handlers[args.command](session, base_url, audience_id, args)


if __name__ == "__main__":
    main()
