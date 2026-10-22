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

"""List arXiv papers matching one topic's query, submitted inside a date window.

Usage: python3 scripts/arxiv.py --topic <topic> --since YYYY-MM-DD --until YYYY-MM-DD
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from radar_common import emit, fetch, finish, load_sources, parse_args, to_date, unreachable

ATOM = "{http://www.w3.org/2005/Atom}"


def main() -> None:
    a = parse_args(__doc__)
    topic, _ = load_sources(a.topic)
    query = topic.get("arxiv", "")
    if not query:
        print(f"# no arXiv query configured for {a.topic}")
        return
    window = f"submittedDate:[{a.since:%Y%m%d}0000 TO {a.until:%Y%m%d}2359]"
    params = urllib.parse.urlencode({
        "search_query": f"({query}) AND {window}", "max_results": 25,
        "sortBy": "submittedDate", "sortOrder": "descending",
    })
    url = f"https://export.arxiv.org/api/query?{params}"
    try:
        root = ET.fromstring(fetch(url))
    except (OSError, ET.ParseError, ValueError) as exc:
        unreachable("arXiv", url, exc)
        finish(1, 1)
        return
    for e in root.iter(f"{ATOM}entry"):
        day = to_date(e.findtext(f"{ATOM}published"))
        if day and a.since <= day <= a.until:
            emit(day, "arXiv", e.findtext(f"{ATOM}title", ""), e.findtext(f"{ATOM}id", ""))


if __name__ == "__main__":
    main()
