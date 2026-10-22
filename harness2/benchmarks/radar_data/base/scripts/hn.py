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

"""List the most-discussed Hacker News stories for one topic's queries inside a date window.

Usage: python3 scripts/hn.py --topic <topic> --since YYYY-MM-DD --until YYYY-MM-DD
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, time, timezone

from radar_common import emit, fetch, finish, load_sources, parse_args, to_date, unreachable


def main() -> None:
    a = parse_args(__doc__)
    topic, _ = load_sources(a.topic)
    lo = int(datetime.combine(a.since, time.min, timezone.utc).timestamp())
    hi = int(datetime.combine(a.until, time.max, timezone.utc).timestamp())
    tried = failed = 0
    seen = set()
    for q in topic.get("hn", []):
        tried += 1
        params = urllib.parse.urlencode({
            "query": q, "tags": "story", "hitsPerPage": 15,
            "numericFilters": f"created_at_i>={lo},created_at_i<={hi},points>=20",
        })
        url = f"https://hn.algolia.com/api/v1/search?{params}"
        try:
            hits = json.loads(fetch(url)).get("hits", [])
        except (OSError, ValueError) as exc:
            failed += 1
            unreachable("Hacker News", url, exc)
            continue
        for h in hits:
            if h.get("objectID") in seen:
                continue
            seen.add(h.get("objectID"))
            day = to_date(h.get("created_at"))
            link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            if day:
                emit(day, f"HN {h.get('points', 0)} pts", h.get("title", ""), link)
    finish(tried, failed)


if __name__ == "__main__":
    main()
