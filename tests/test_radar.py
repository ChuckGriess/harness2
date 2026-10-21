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

"""Regressions for the AI Radar benchmark: gold isolation, judge failures, rollout caching."""

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness2.benchmarks import radar
from harness2.core import context, judge, report, tree
from harness2.core.spec import RunView
from harness2.substrate.claude_api import CA_SURFACE
from tests.test_claude_api import FakeClient, resp, tool_use

TASK = "2026-10-12/open-models-and-tooling"
SENTINEL = "GOLD-SENTINEL-7f3a"
GOLD = [
    {"id": "g1", "title": f"Beam open model {SENTINEL}", "url": "https://huggingface.co/blog/x"},
    {"id": "g2", "title": "Apertus 1.5", "url": "https://huggingface.co/blog/y"},
    {"id": "g3", "title": "tinker-cookbook nightly", "url": "https://github.com/z"},
]
DIGEST = (
    "# AI Radar\n## Items\n- **Beam** (2026-10-16, Hugging Face): open 501B model. "
    "Sources: https://huggingface.co/blog/reflection-beam\n"
)


def rollout_script():
    return [
        resp([tool_use("t1", "list_dir", {"path": "."})], "tool_use"),
        resp([tool_use("t2", "write_file", {"path": "output/digest.md", "content": DIGEST})],
             "tool_use"),
        resp([{"type": "text", "text": "done"}]),
    ]


def verdict(gold_marks, items):
    body = json.dumps({"gold": gold_marks, "items": items})
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": body}]}


class FakeJudge:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.prompts.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class RadarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gold_dir = self.root / "gold"
        (self.gold_dir / TASK).mkdir(parents=True)
        (self.gold_dir / TASK / "gold.json").write_text(json.dumps({"items": GOLD}))

    def adapter(self, client=None, judge_client=None, gold=True):
        return radar.RadarAdapter(
            client=client, judge_client=judge_client, runs_root=self.root / "runs",
            gold_dir=self.gold_dir if gold else self.root / "no-gold",
        )

    def test_task_table_and_watchlist_consistency(self):
        tasks = radar.RadarAdapter().tasks()
        self.assertEqual(len(tasks), 21)
        self.assertEqual(radar.topics(), radar.RadarAdapter().reference_domain_order())
        data = json.loads((radar.DATA / "sources.json").read_text())
        watch = (radar.DATA / "watchlist.md").read_text()
        listed = re.findall(r"^- (\S+):", watch.split("## Topics", 1)[1], re.M)
        self.assertEqual(listed, list(data["topics"]))
        for ent in data["entities"]:
            with self.subTest(entity=ent["name"]):
                self.assertIn(ent["name"], watch)
                self.assertTrue(ent["feeds"] or ent["repos"] or ent["pages"] or ent["search_only"])
                self.assertTrue(set(ent["topics"]) <= set(data["topics"]))
        for name in re.findall(r"^- \*\*(.+?)\*\*", watch, re.M):
            for part in re.split(r" / |, | \(", name.removeprefix("Frontier labs (")):
                part = part.rstrip(")")
                if part and part not in ("Mira Murati",):
                    with self.subTest(watchlist=part):
                        self.assertTrue(any(part in e["name"] or e["name"] in part
                                            for e in data["entities"]), part)

    def test_base_harness_is_valid(self):
        a = radar.RadarAdapter()
        base = tree.read_tree(CA_SURFACE, a.base_harness())
        tree.validate(CA_SURFACE, base, allow_subagents=False,
                      reserved_scripts=tree.RESERVED_SCRIPTS)
        a.validate_tree(base)
        bad = dict(base, **{"scripts/hn.py": "import requests\nimport radar_common\n"})
        with self.assertRaises(ValueError) as cm:
            a.validate_tree(bad)
        self.assertIn("imports 'requests'", str(cm.exception))
        self.assertNotIn("radar_common", str(cm.exception))

    def test_rollout_caches_and_gold_never_reaches_model_context(self):
        client = FakeClient(rollout_script())
        judge_client = FakeJudge(verdict(
            [{"id": "g1", "covered": True}, {"id": "g2", "covered": False},
             {"id": "g3", "covered": False}],
            [{"title": "Beam", "valid": True, "reason": "dated, sourced"}]))
        a = self.adapter(client, judge_client)
        rd = a.execute(TASK, None, "r1")
        self.assertTrue(a.delivered(rd))
        self.assertEqual(a.execute(TASK, None, "r1"), rd)
        self.assertEqual(len(client.calls), 3, "a completed rollout must be reused, not re-run")

        got = a.score(rd, TASK)
        self.assertEqual((got["n_passed"], got["n_criteria"]), (2, 4))
        self.assertEqual((got["recall"], got["precision"]), (0.3333, 1.0))
        self.assertIn(SENTINEL, judge_client.prompts[0]["messages"][0]["content"])

        rv = RunView(label="base", run_id="r1", run_dir=rd, ok=True, delivered=True)
        ctx = self.root / "ctx"
        context.build(ctx, a, TASK, [rv], harness_dir=a.base_harness())
        seen = [p for p in [*ctx.rglob("*"), *rd.rglob("*")] if p.is_file()]
        self.assertTrue(seen)
        for p in seen:
            with self.subTest(path=str(p.relative_to(self.root))):
                self.assertNotIn(SENTINEL, p.read_text(errors="replace"))
                self.assertNotEqual(p.name, "gold.json")
        for call in client.calls:
            self.assertNotIn(SENTINEL, json.dumps(call))

        leak = self.root / "leaky"
        leak.mkdir()
        (leak / "gold.json").write_text("{}")
        with self.assertRaises(ValueError):
            context.assert_no_leak_paths(leak, a.spec.leak_globs)

    def test_transport_failure_is_raised_and_not_cached(self):
        a = self.adapter(FakeClient([RuntimeError("connection reset")]))
        with self.assertRaises(RuntimeError) as cm:
            a.execute(TASK, None, "r2")
        self.assertIn("not cached", str(cm.exception))
        self.assertFalse((self.root / "runs/results/r2/.adapt_complete").exists())

    def test_judge_failure_is_none_never_zero(self):
        a0 = self.adapter(FakeClient(rollout_script()))
        rd = a0.execute(TASK, None, "r3")
        cases = {
            "no gold": (self.adapter(gold=False), None),
            "judge raises": (self.adapter(judge_client=FakeJudge(RuntimeError("503"))), None),
            "refusal": (self.adapter(judge_client=FakeJudge(
                {"stop_reason": "refusal", "content": []})), None),
            "not json": (self.adapter(judge_client=FakeJudge(
                {"stop_reason": "end_turn", "content": [{"type": "text", "text": "nope"}]})),
                None),
            "wrong gold ids": (self.adapter(judge_client=FakeJudge(verdict(
                [{"id": "g1", "covered": True}], []))), None),
        }
        for label, (a, _) in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(a.score(rd, TASK))
                row = judge.judge_one(a, TASK, "agg", 1, "base", "r3", rd)
                self.assertFalse(row.graded)
                self.assertIsNone(row.frac)

    def test_empty_digest_is_a_real_zero(self):
        rd = self.root / "empty-run"
        (rd / "workspace/output").mkdir(parents=True)
        got = self.adapter(judge_client=FakeJudge(AssertionError("judge must not run"))).score(
            rd, TASK)
        self.assertEqual((got["n_passed"], got["n_criteria"]), (0, 3))

    def test_core_never_imports_the_judge_or_benchmarks(self):
        core = Path(radar.__file__).resolve().parents[1] / "core"
        for f in sorted(core.glob("*.py")):
            src = f.read_text()
            with self.subTest(module=f.name):
                self.assertNotRegex(src, r"(from|import)\s+\.\.benchmarks|radar")
                if f.name not in ("judge.py", "report.py"):  # both run after selection
                    self.assertNotRegex(src, r"from \.judge import|import judge\b")

    def test_report_excludes_ungraded_from_denominator(self):
        rows = [judge.Scored("t1", "agg", 1, "agg1", "r", False, n_passed=3, n_criteria=4),
                judge.Scored("t2", "agg", 1, "agg1", "r", False, error="judge failed")]
        (s,) = report.summarise(rows)
        self.assertEqual((s.n, s.n_ungraded, s.score), (1, 1, 75.0))


class FetcherTests(unittest.TestCase):
    def load(self, name):
        scripts = radar.DATA / "base" / "scripts"
        sys.path.insert(0, str(scripts))
        self.addCleanup(sys.path.remove, str(scripts))
        spec = importlib.util.spec_from_file_location(f"radar_{name}", scripts / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_feed_parsing_and_dates(self):
        feeds = self.load("feeds")
        import xml.etree.ElementTree as ET

        rss = ET.fromstring(
            "<rss><channel><item><title>A</title><link>https://a</link>"
            "<pubDate>Thu, 15 Oct 2026 23:30:00 -0700</pubDate></item></channel></rss>")
        atom = ET.fromstring(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>'
            '<link rel="alternate" href="https://b"/><published>2026-10-12T00:00:00Z</published>'
            "</entry></feed>")
        self.assertEqual(feeds.entries(rss), [("A", "https://a", "Thu, 15 Oct 2026 23:30:00 -0700")])
        self.assertEqual(feeds.entries(atom), [("B", "https://b", "2026-10-12T00:00:00Z")])
        common = self.load("radar_common")
        self.assertEqual(str(common.to_date("Thu, 15 Oct 2026 23:30:00 -0700")), "2026-10-16")
        self.assertIsNone(common.to_date("not a date"))


if __name__ == "__main__":
    unittest.main()
