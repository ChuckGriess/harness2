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

"""Regressions for harness2-radar: digests are written locally; delivery happens only on request."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness2 import config
from harness2.benchmarks import radar
from harness2.cli import radar as cli
from tests.test_radar_sweep import RuleClient, executor_rule

TOPICS = "open-models-and-tooling,frontier-applications"
NO_DELIVERY_ENV = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS2_RADAR_")}
NO_DELIVERY_ENV["ANTHROPIC_API_KEY"] = "sk-test-not-real"


def no_send(*a, **k):
    raise AssertionError("a sender was reached without --deliver")


class RadarCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)

    def run_cli(self, argv, client=None, env=None):
        out = io.StringIO()
        with patch.dict(os.environ, env or NO_DELIVERY_ENV, clear=True), \
                patch.object(config, "require_anthropic"), \
                contextlib.redirect_stdout(out):
            rc = cli.main(["--out", str(self.out), *argv], client=client)
        return rc, out.getvalue()

    def test_digest_written_and_nothing_sent_without_deliver(self):
        client = RuleClient(executor_rule)
        with patch("urllib.request.urlopen", no_send), patch("smtplib.SMTP_SSL", no_send):
            rc, out = self.run_cli(["--topics", TOPICS, "--until", "2026-10-18"], client)
        self.assertEqual(rc, 0, out)
        digest = (self.out / "digests" / "2026-10-18.md").read_text()
        self.assertIn("2026-10-12 to 2026-10-18", digest)
        self.assertEqual(digest.count("## Items"), 2)
        self.assertIn("open-models-and-tooling", digest)
        self.assertRegex(digest, r"Estimated cost: \$0\.\d{4}")
        self.assertEqual(len(client.calls), 4)
        first = client.calls[0]["messages"][0]["content"]
        self.assertIn("Window: 2026-10-12 to 2026-10-18 (inclusive)", first)
        for topic in TOPICS.split(","):
            self.assertTrue((self.out / "runs" / "2026-10-18" / topic / "result.json").is_file())

    def test_missing_delivery_settings_fail_before_any_model_call(self):
        client = RuleClient(no_send)
        for kind, var in (("slack", "HARNESS2_RADAR_SLACK_WEBHOOK"),
                          ("email", "HARNESS2_RADAR_SMTP_HOST")):
            with self.subTest(kind=kind):
                rc, out = self.run_cli(["--topics", TOPICS, "--deliver", kind], client)
                self.assertEqual(rc, 1)
                self.assertIn(var, out)
        self.assertEqual(client.calls, [])

    def test_slack_delivery_sends_the_digest_once(self):
        client = RuleClient(executor_rule)
        env = dict(NO_DELIVERY_ENV, HARNESS2_RADAR_SLACK_WEBHOOK="https://hooks.slack.com/x")
        sent = MagicMock()
        sent.return_value.__enter__.return_value.status = 200
        with patch("urllib.request.urlopen", sent), patch("smtplib.SMTP_SSL", no_send):
            rc, out = self.run_cli(["--topics", "frontier-applications", "--until", "2026-10-18",
                                    "--deliver", "slack"], client, env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sent.call_count, 1)
        req = sent.call_args.args[0]
        self.assertEqual(req.full_url, "https://hooks.slack.com/x")
        self.assertIn("frontier-applications", json.loads(req.data)["text"])
        self.assertIn("about to send", out)

    def test_send_existing_digest_by_email_without_running_the_agent(self):
        digest = self.out / "d.md"
        digest.write_text("# AI Radar\n- item https://x\n")
        env = dict(NO_DELIVERY_ENV, HARNESS2_RADAR_SMTP_HOST="smtp.example.com",
                   HARNESS2_RADAR_SMTP_USER="u", HARNESS2_RADAR_SMTP_PASSWORD="p",
                   HARNESS2_RADAR_EMAIL_FROM="radar@example.com",
                   HARNESS2_RADAR_EMAIL_TO="chuck@example.com")
        smtp = MagicMock()
        with patch("smtplib.SMTP_SSL", smtp), patch("urllib.request.urlopen", no_send):
            rc, out = self.run_cli(["--send", str(digest), "--deliver", "email"],
                                   RuleClient(no_send), env)
        self.assertEqual(rc, 0, out)
        smtp.assert_called_once_with("smtp.example.com", 465, timeout=30)
        msg = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual((msg["To"], msg["From"]), ("chuck@example.com", "radar@example.com"))
        self.assertIn("item https://x", msg.get_content())
        self.assertIsNone(re.search(r"\bp\b", out), "the SMTP password must never be printed")

    def test_default_window_is_seven_days_ending_today(self):
        since, until = cli.window(None, 7)
        self.assertEqual((until - since).days, 6)
        self.assertEqual(radar.render_prompt("x", since, until).count("Window:"), 1)


if __name__ == "__main__":
    unittest.main()
