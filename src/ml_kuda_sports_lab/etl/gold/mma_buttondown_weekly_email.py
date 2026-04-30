#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Create or send the weekly Fight Prophet Buttondown email.

This module is meant to run after the Sunday dashboard parquet export. It
announces that the Streamlit prediction page has been refreshed and links
readers to the live predictions view.

Safe defaults:
  - BUTTONDOWN_ENABLED must be true, otherwise the job exits successfully.
  - BUTTONDOWN_EMAIL_STATUS defaults to draft.
  - Immediate sends require BUTTONDOWN_EMAIL_STATUS=about_to_send and a
    Buttondown API key. For Buttondown API version 2026-04-01, the first send
    with a new API key also needs BUTTONDOWN_CONFIRM_SEND=true.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import sys
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

DEFAULT_API_URL = "https://api.buttondown.com/v1/emails"
DEFAULT_API_VERSION = "2026-04-01"
DEFAULT_APP_URL = "https://app.fightprophet.com"
DEFAULT_ARCHIVE_URL = "https://buttondown.com/fightprophet"
DEFAULT_TIMEZONE = "America/Bogota"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        print(f"Unknown timezone {name!r}; falling back to UTC.", file=sys.stderr)
        return ZoneInfo("UTC")


def _with_page_param(app_url: str, page: str) -> str:
    parsed = urlparse(app_url.rstrip("/"))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = page
    path = parsed.path or "/"
    return urlunparse(parsed._replace(path=path, query=urlencode(query)))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _build_body(predictions_url: str, run_date: str, generated_at: str) -> str:
    return "\n".join(
        [
            "<!-- buttondown-editor-mode: plaintext -->",
            "# Fight Prophet Weekly",
            "",
            "The Sunday ML process has refreshed the Fight Prophet prediction board.",
            "",
            f"[Open the updated Streamlit predictions page]({predictions_url})",
            "",
            "Inside the app you can compare model probability, market probability, edge bands, and upcoming fight-card context from the latest dashboard export.",
            "",
            f"Run date: {run_date}",
            f"Generated at: {generated_at}",
            "",
            "Information and education only. Not financial or betting advice. Predictions can be wrong.",
        ]
    )


def _build_payload(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    run_date = args.run_date or now.date().isoformat()
    generated_at = now.isoformat(timespec="seconds")
    predictions_url = _with_page_param(args.app_url, args.page)
    subject = args.subject or f"Fight Prophet Weekly: updated predictions for {run_date}"
    slug = args.slug or _slugify(f"fight-prophet-weekly-{run_date}")

    return {
        "subject": subject,
        "slug": slug,
        "description": "Weekly Fight Prophet prediction page update.",
        "canonical_url": predictions_url,
        "body": _build_body(predictions_url, run_date, generated_at),
        "status": args.status,
        "metadata": {
            "source": "ml_kuda_sports_lab.sunday_pipeline",
            "run_date": run_date,
            "generated_at": generated_at,
            "predictions_url": predictions_url,
            "archive_url": args.archive_url,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or send a Buttondown weekly prediction email.")
    parser.add_argument("--api-url", default=os.getenv("BUTTONDOWN_API_URL", DEFAULT_API_URL))
    parser.add_argument("--api-version", default=os.getenv("BUTTONDOWN_API_VERSION", DEFAULT_API_VERSION))
    parser.add_argument("--app-url", default=os.getenv("PUBLIC_APP_URL", DEFAULT_APP_URL))
    parser.add_argument("--archive-url", default=os.getenv("BUTTONDOWN_ARCHIVE_URL", DEFAULT_ARCHIVE_URL))
    parser.add_argument("--page", default=os.getenv("BUTTONDOWN_APP_PAGE", "predictions"))
    parser.add_argument("--timezone", default=os.getenv("NEWSLETTER_TIMEZONE", DEFAULT_TIMEZONE))
    parser.add_argument("--run-date", default=os.getenv("NEWSLETTER_RUN_DATE", ""))
    parser.add_argument("--subject", default=os.getenv("BUTTONDOWN_EMAIL_SUBJECT", ""))
    parser.add_argument("--slug", default=os.getenv("BUTTONDOWN_EMAIL_SLUG", ""))
    parser.add_argument("--status", default=os.getenv("BUTTONDOWN_EMAIL_STATUS", "draft"))
    parser.add_argument(
        "--enabled",
        action="store_true",
        default=_env_bool("BUTTONDOWN_ENABLED", False),
        help="Run the Buttondown API call. Defaults to BUTTONDOWN_ENABLED.",
    )
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        default=_env_bool("BUTTONDOWN_CONFIRM_SEND", False),
        help="Send Buttondown's one-time confirmation header for about_to_send.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=_env_bool("BUTTONDOWN_DRY_RUN", False),
        help="Print the payload instead of calling Buttondown.",
    )
    parser.add_argument(
        "--skip-duplicates",
        action="store_true",
        default=_env_bool("BUTTONDOWN_SKIP_DUPLICATES", True),
        help="Treat Buttondown 409 conflicts as success.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.enabled:
        print("Buttondown weekly email skipped because BUTTONDOWN_ENABLED is not true.")
        return 0

    now = datetime.now(_timezone(args.timezone))
    payload = _build_payload(args, now)

    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    api_key = os.getenv("BUTTONDOWN_API_KEY", "").strip()
    if not api_key:
        print("BUTTONDOWN_API_KEY is required when BUTTONDOWN_ENABLED=true.", file=sys.stderr)
        return 2

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
        "X-API-Version": args.api_version,
    }
    if args.confirm_send:
        headers["X-Buttondown-Live-Dangerously"] = "true"

    response = requests.post(args.api_url, headers=headers, json=payload, timeout=30)
    if response.status_code == 409 and args.skip_duplicates:
        print(f"Buttondown email already exists for slug {payload['slug']!r}; skipping duplicate.")
        return 0

    if response.status_code >= 400:
        print(f"Buttondown API failed with HTTP {response.status_code}: {response.text}", file=sys.stderr)
        if response.status_code == 400 and payload["status"] == "about_to_send" and not args.confirm_send:
            print(
                "If this is the first live send for this API key, set BUTTONDOWN_CONFIRM_SEND=true once.",
                file=sys.stderr,
            )
        return 1

    try:
        data = response.json()
    except ValueError:
        data = {}

    email_url = data.get("absolute_url") or data.get("url") or data.get("id") or "<created>"
    print(f"Buttondown weekly email {payload['status']} successfully: {email_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
