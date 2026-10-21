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

"""List RSS/Atom feed items (blogs, Substacks, YouTube) for one topic inside a date window.

Usage: python3 scripts/feeds.py --topic <topic> --since YYYY-MM-DD --until YYYY-MM-DD
Also lists the topic's page-only sources (check those with web_fetch) and search-only voices.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from radar_common import emit, fetch, finish, load_sources, parse_args, to_date, unreachable

ATOM = "{http://www.w3.org/2005/Atom}"


def entries(root: ET.Element) -> list[tuple[str, str, str | None]]:
    """(title, link, timestamp) for every RSS item or Atom entry under root."""
    out = []
    for it in root.iter("item"):
        out.append((it.findtext("title", ""), it.findtext("link", ""), it.findtext("pubDate")))
    for e in root.iter(f"{ATOM}entry"):
        link = ""
        for ln in e.findall(f"{ATOM}link"):
            if ln.get("rel", "alternate") == "alternate":
                link = ln.get("href", "")
                break
        stamp = e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")
        out.append((e.findtext(f"{ATOM}title", ""), link, stamp))
    return out


def main() -> None:
    a = parse_args(__doc__)
    _, people = load_sources(a.topic)
    tried = failed = 0
    for ent in people:
        for url in ent["feeds"]:
            tried += 1
            try:
                root = ET.fromstring(fetch(url))
            except (OSError, ET.ParseError, ValueError) as exc:
                failed += 1
                unreachable(ent["name"], url, exc)
                continue
            for title, link, stamp in entries(root):
                day = to_date(stamp)
                if day and a.since <= day <= a.until:
                    emit(day, ent["name"], title, link.strip())
    pages = [f"{e['name']}: {u}" for e in people for u in e["pages"]]
    voices = [f"{e['name']}: {s}" for e in people for s in e["search_only"]]
    if pages:
        print("# pages to check with web_fetch:\n" + "\n".join(pages))
    if voices:
        print("# search-only (use web_search):\n" + "\n".join(voices))
    finish(tried, failed)


if __name__ == "__main__":
    main()
