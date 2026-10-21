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

"""List GitHub releases for one topic's watched repos inside a date window.

Usage: python3 scripts/github.py --topic <topic> --since YYYY-MM-DD --until YYYY-MM-DD
"""

from __future__ import annotations

import json

from radar_common import emit, fetch, finish, load_sources, parse_args, to_date, unreachable


def main() -> None:
    a = parse_args(__doc__)
    _, people = load_sources(a.topic)
    tried = failed = 0
    for ent in people:
        for repo in ent["repos"]:
            tried += 1
            url = f"https://api.github.com/repos/{repo}/releases?per_page=50"
            try:
                releases = json.loads(fetch(url))
            except (OSError, ValueError) as exc:
                failed += 1
                unreachable(ent["name"], url, exc)
                continue
            for r in releases if isinstance(releases, list) else []:
                day = to_date(r.get("published_at"))
                if day and a.since <= day <= a.until and not r.get("draft"):
                    title = f"{repo} {r.get('name') or r.get('tag_name')}"
                    emit(day, ent["name"], title, r.get("html_url", ""))
    finish(tried, failed)


if __name__ == "__main__":
    main()
