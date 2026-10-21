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

"""Write today's AI Radar digest with a harness, and deliver it only when asked.

Runs one `ca` rollout per watchlist topic over the window ending --until (default today) and
writes <out>/digests/<until>.md. This is not a benchmark run: there is no recursion, no gold and
no judge. Delivery is outward-facing, so it happens only with --deliver, only from this host
code (the agent has no send tool), and every setting it needs is checked before the first model
call. --send delivers an existing digest without running the agent, e.g. email from the host
when the container has no SMTP egress.

    harness2-radar                                   # all topics, last 7 days, base harness
    harness2-radar --harness runs/radar/adopted/x    # an adopted harness tree
    harness2-radar --deliver slack                   # also post the digest to Slack
    harness2-radar --send runs/radar/digests/2026-10-18.md --deliver email
"""

from __future__ import annotations

import argparse
import json
import shutil
import smtplib
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

from .. import config
from ..benchmarks import radar
from ..substrate.claude_api import CaSubstrate

_SLACK_LIMIT = 12_000  # cumulative markdown-block cap per Slack message
_EMAIL_VARS = ("HARNESS2_RADAR_SMTP_HOST", "HARNESS2_RADAR_SMTP_USER",
               "HARNESS2_RADAR_SMTP_PASSWORD", "HARNESS2_RADAR_EMAIL_FROM",
               "HARNESS2_RADAR_EMAIL_TO")


def window(until: date | None, days: int) -> tuple[date, date]:
    """The inclusive `days`-day window ending `until` (default today)."""
    end = until or date.today()
    return end - timedelta(days=days - 1), end


def _require_delivery(kind: str) -> None:
    if kind == "slack":
        config.require("HARNESS2_RADAR_SLACK_WEBHOOK", needed_by="Slack delivery")
    else:
        for name in _EMAIL_VARS:
            config.require(name, needed_by="email delivery")


def _slack_chunks(text: str, limit: int = _SLACK_LIMIT) -> list[str]:
    """Split at topic headings, then lines, so each message stays under Slack's markdown cap."""
    sections, cur = [], []
    for ln in text.splitlines():
        if ln.startswith("## ") and cur:
            sections.append("\n".join(cur))
            cur = []
        cur.append(ln)
    sections.append("\n".join(cur))
    pieces = []
    for sec in sections:
        if len(sec) <= limit:
            pieces.append(sec)
            continue
        for ln in sec.splitlines():
            pieces.extend(ln[i:i + limit] for i in range(0, max(len(ln), 1), limit))
    chunks = [""]
    for piece in pieces:
        joined = f"{chunks[-1]}\n{piece}" if chunks[-1] else piece
        if len(joined) <= limit:
            chunks[-1] = joined
        else:
            chunks.append(piece)
    return [c for c in chunks if c.strip()]


def _send_slack(text: str, subject: str) -> None:
    """Post as Slack markdown blocks, one message per chunk; `text` is the notification line."""
    url = config.require("HARNESS2_RADAR_SLACK_WEBHOOK", needed_by="Slack delivery")
    chunks = _slack_chunks(text)
    for i, chunk in enumerate(chunks, 1):
        note = subject if len(chunks) == 1 else f"{subject} ({i}/{len(chunks)})"
        payload = {"text": note, "blocks": [{"type": "markdown", "text": chunk}]}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"Slack webhook answered HTTP {r.status} on message {i}")


def _send_email(text: str, subject: str) -> None:
    host, user, password, sender, to = (
        config.require(n, needed_by="email delivery") for n in _EMAIL_VARS
    )
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    with smtplib.SMTP_SSL(host, config.env_int("HARNESS2_RADAR_SMTP_PORT", 465), timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def deliver(kind: str, text: str, subject: str) -> None:
    """Print what is about to go out, then send it. The caller already asked for this."""
    print(f"about to send {len(text)} chars via {kind}: {subject}\n---\n{text[:1500]}\n---")
    _send_slack(text, subject) if kind == "slack" else _send_email(text, subject)
    print(f"sent via {kind}")


def _demote(md: str) -> str:
    """Nest a topic digest's headings one level under the combined digest's."""
    return "\n".join("#" + ln if ln.startswith("#") else ln for ln in md.splitlines())


def build(args, client=None) -> tuple[Path, str]:
    """Run every topic, write the combined digest, return (path, text)."""
    since, until = window(args.until, args.days)
    adapter = radar.RadarAdapter()
    sub = CaSubstrate(client=client)
    sub.preflight()
    topics = args.topics.split(",") if args.topics else radar.topics()
    unknown = sorted(set(topics) - set(radar.topics()))
    if unknown:
        raise ValueError(f"unknown topic(s) {unknown}; expected some of {radar.topics()}")
    tree = Path(args.harness) if args.harness else adapter.base_harness()
    install = sub.materialise(tree, adapter.spec, adapter.workspace_files())
    out = Path(args.out)

    def one(topic: str):
        rd = out / "runs" / str(until) / topic
        res = sub.rollout(radar.render_prompt(topic, since, until), install, rd,
                          timeout=args.timeout)
        return topic, res, adapter.deliverable_text(rd)

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = list(pool.map(one, topics))
    finally:
        shutil.rmtree(install.staging_dir, ignore_errors=True)

    cost = sum(r.cost_usd for _, r, _ in results)
    parts = [f"# AI Radar: {since} to {until}\n"]
    for topic, res, digest in results:
        parts.append(f"## {topic}\n")
        parts.append(_demote(digest.strip()) if digest.strip() else
                     f"(no digest written: {res.error or 'empty output'})")
    failed = [f"{t} ({r.error})" for t, r, _ in results if r.error]
    parts.append(f"\n---\nHarness: {tree}\nEstimated cost: ${cost:.4f} (Sonnet tokens plus "
                 f"web search fees, from config.CLAUDE_PRICES)")
    if failed:
        parts.append("Topics that stopped early: " + "; ".join(failed))
    text = "\n\n".join(parts) + "\n"
    path = out / "digests" / f"{until}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"digest: {path}  ({len(topics)} topics, estimated ${cost:.4f})")
    return path, text


def main(argv: list[str] | None = None, *, client=None) -> int:
    p = argparse.ArgumentParser(prog="harness2-radar", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topics", default="", help="comma list; default every watchlist topic")
    p.add_argument("--until", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--harness", default="", help="harness tree dir; default the base harness")
    p.add_argument("--out", default=str(config.RUNS / "radar"))
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--timeout", type=int, default=1800, help="seconds per topic")
    p.add_argument("--deliver", choices=("slack", "email"), default=None)
    p.add_argument("--send", default="", help="deliver this existing digest; runs no agent")
    args = p.parse_args(argv)
    try:
        if args.send and not args.deliver:
            raise ValueError("--send needs --deliver slack|email")
        if args.deliver:
            _require_delivery(args.deliver)
        if args.send:
            path = Path(args.send)
            text = path.read_text(encoding="utf-8")
        else:
            path, text = build(args, client)
        if args.deliver:
            deliver(args.deliver, text, f"AI Radar {path.stem}")
    except (config.ConfigError, RuntimeError, ValueError, OSError) as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
