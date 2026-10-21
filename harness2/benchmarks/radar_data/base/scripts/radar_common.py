# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared helpers for the radar fetcher scripts: window parsing, sources.json, HTTP, output.

Every fetcher prints one line per item, `YYYY-MM-DD | source | title | url`, and one
`! unreachable: ...` line per source that failed, so a partial outage is visible to the
agent instead of silently shrinking coverage. A fetcher exits 1 only when every source failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

UA = "harness2-radar/0.1 (+research digest)"
TIMEOUT = 20


def parse_args(doc: str) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=doc)
    p.add_argument("--topic", required=True)
    p.add_argument("--since", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    a = p.parse_args()
    if a.since > a.until:
        p.error("--since must not be after --until")
    return a


def load_sources(topic: str) -> tuple[dict, list[dict]]:
    """(topic config, entities following that topic) from sources.json in the workspace root."""
    data = json.loads(Path("sources.json").read_text(encoding="utf-8"))
    if topic not in data["topics"]:
        sys.exit(f"unknown topic {topic!r}; expected one of {sorted(data['topics'])}")
    return data["topics"][topic], [e for e in data["entities"] if topic in e["topics"]]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(20_000_000)


def to_date(raw: str | None) -> date | None:
    """ISO-8601 or RFC-822 timestamp -> UTC date; None when absent or unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


def emit(day: date, source: str, title: str, url: str) -> None:
    title = " ".join(str(title).split())
    print(f"{day.isoformat()} | {source} | {title} | {url}")


def unreachable(source: str, url: str, exc: Exception) -> None:
    print(f"! unreachable: {source} {url} ({type(exc).__name__}: {str(exc)[:120]})")


def finish(tried: int, failed: int) -> None:
    if tried and failed == tried:
        sys.exit(1)
