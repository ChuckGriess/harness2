# Tools

- run_script feeds.py | github.py | hn.py | arxiv.py with args
  ["--topic", "<topic>", "--since", "YYYY-MM-DD", "--until", "YYYY-MM-DD"].
  Output lines are `date | source | title | url`. `! unreachable:` lines name sources that
  failed; check those with web_fetch instead. feeds.py also lists page-only sources and
  search-only voices for the topic.
- web_fetch opens a URL already in the conversation (from a script, a search result or the
  watchlist). Use it to confirm dates and read release notes.
- web_search finds announcements that no feed carries, and posts by search-only voices.
  Include the person or organization and the month in the query.
- write_file writes only under output/. The deliverable is output/digest.md.

# Digest format

# AI Radar: <topic>, <since> to <until>

## Items
- **<title>** (<YYYY-MM-DD>, <person or organization>): <one line on why it matters to a
  practitioner>. Sources: <url>[, <url>]

## Checked, nothing new in window
- <source name>
