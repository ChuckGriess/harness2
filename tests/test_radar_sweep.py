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

"""Regressions for the claude_api improver channel and the radar/ca CLI cell."""

import contextlib
import copy
import importlib
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2 import config
from harness2.benchmarks import radar
from harness2.cli import _common as C
from harness2.cli import run_sweep
from harness2.core import schedule
from harness2.core.proposer import Proposer, Reply
from harness2.core.spec import ChannelSpec, resolve_channel_model

MODEL = "claude-sonnet-5-5"
TASK = "2026-10-12/open-models-and-tooling"


def _resp(content, stop):
    return {"model": MODEL, "content": content, "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 1}}


class RuleClient:
    """Thread-safe fake: picks its reply from the conversation so far, never from call order."""

    def __init__(self, rule):
        self.rule = rule
        self.calls = []
        self._lock = threading.Lock()
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        with self._lock:
            self.calls.append(copy.deepcopy(kw))
        return self.rule(kw["messages"])


def executor_rule(messages):
    if len(messages) == 1:
        digest = "# AI Radar\n## Items\n- **Beam** (2026-10-16, HF): open model. https://hf.co/b\n"
        return _resp([{"type": "tool_use", "id": "w", "name": "write_file",
                       "input": {"path": "output/digest.md", "content": digest}}], "tool_use")
    return _resp([{"type": "text", "text": "done"}], "end_turn")


class RadarStubProposer(Proposer):
    """Deterministic improver for budget tests: one guardrail edit, first-candidate selection."""

    def call(self, mission, context_dir, label, **kwargs) -> Reply:
        self.missions.append((label, mission + kwargs.get("system", "")))
        ctx = Path(context_dir)
        workspace = None
        if label.startswith("select."):
            chosen = sorted(p.stem for p in (ctx / "candidates").glob("*.md"))[0]
            text = f"<why>First candidate.</why>\n```choice\n{chosen}\n```"
        elif label.startswith("experience."):
            text = "NO_ENTRY"
        else:
            workspace = ctx / "harness"
            g = workspace / "guardrails.md"
            g.write_text(g.read_text() + "- Prefer primary sources over aggregators.\n")
            text = "<notes>Added a primary-source rule.</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


class ClaudeApiChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_channel_takes_bare_anthropic_id(self):
        spec = resolve_channel_model("claude_api", MODEL)
        self.assertEqual((spec.kind, spec.model), ("claude_api", MODEL))
        with self.assertRaises(ValueError):
            resolve_channel_model("claude_api", "anthropic/claude-sonnet-5-5")
        self.assertEqual(radar.radar_spec().channels["proposer"].kind, "claude_api")

    def test_improver_edits_harness_and_cannot_write_elsewhere(self):
        ctx = self.root / "ctx"
        (ctx / "harness").mkdir(parents=True)
        (ctx / "harness" / "guardrails.md").write_text("- Cite.\n")
        (ctx / "task.md").write_text("task")

        def rule(messages):
            if len(messages) == 1:
                return _resp([
                    {"type": "tool_use", "id": "a", "name": "write_file",
                     "input": {"path": "context/harness/guardrails.md", "content": "- Cite twice.\n"}},
                    {"type": "tool_use", "id": "b", "name": "write_file",
                     "input": {"path": "context/task.md", "content": "tampered"}},
                ], "tool_use")
            return _resp([{"type": "text", "text": "<notes>tightened citing</notes>"}], "end_turn")

        client = RuleClient(rule)
        prop = Proposer(ChannelSpec("claude_api", MODEL), audit_dir=self.root / "audit",
                        client=client)
        reply = prop.call("Improve the harness.", ctx, "improve.t.agg1", system="be careful",
                          harvest="context/harness")
        self.assertEqual(reply.text, "<notes>tightened citing</notes>")
        self.assertEqual((reply.workspace / "guardrails.md").read_text(), "- Cite twice.\n")
        self.assertGreater(reply.cost_usd, 0)
        results = client.calls[1]["messages"][-1]["content"]
        self.assertEqual([r.get("is_error") for r in results], [None, True])
        self.assertEqual((ctx / "task.md").read_text(), "task")
        first = client.calls[0]
        self.assertIn("be careful", first["system"])
        self.assertNotIn("web_search", [t["name"] for t in first["tools"]])

    def test_capped_improver_session_is_not_a_reply(self):
        forever = RuleClient(lambda m: _resp(
            [{"type": "tool_use", "id": "x", "name": "list_dir", "input": {"path": "."}}],
            "tool_use"))
        prop = Proposer(ChannelSpec("claude_api", MODEL, steps=2), client=forever, attempts=1)
        with self.assertRaises(Exception) as cm:
            prop.call("go", None, "improve.t.agg1")
        self.assertIn("turn cap reached (2)", str(cm.exception))


class RadarSweepTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_recursion_budget_on_radar(self):
        for k in (1, 2):
            with self.subTest(k=k):
                client = RuleClient(executor_rule)
                a = radar.RadarAdapter(client=client, runs_root=self.root / f"runs{k}")
                # The adapter's own channel, so _selector_for reuses this stub rather than
                # building a live Proposer for the selector channel.
                prop = RadarStubProposer(a.spec.channels["proposer"])
                prop.missions = []
                result = schedule.run_domain(
                    "open-models-and-tooling", [TASK], k, adapter=a, prop=prop,
                    runs_root=a.spec.runs_root, library_root=self.root / f"lib{k}",
                )
                self.assertEqual((result["done"], result["failed"]), (1, 0))
                summary = json.loads(
                    (a.spec.runs_root / f"radar-ca-agg{k}" / TASK.replace("/", "__") / "summary.json"
                     ).read_text())
                for mode in ("agg", "seq"):
                    ledger = summary["modes"][mode]["ledger"]
                    self.assertEqual((ledger["R"], ledger["P"]), (4 * k, 3 * k))
                improve = [m for label, m in prop.missions
                           if not label.startswith(("select.", "experience."))]
                self.assertTrue(improve)
                for m in improve:
                    self.assertIn("`load_skill`", m)
                    self.assertNotIn("plugin/<name>.ts", m)
                    self.assertIsNone(re.search(r"\{[a-z_]+\}", m), "unrendered placeholder")

    def test_dry_run_makes_no_model_calls(self):
        boom = SimpleNamespace(Anthropic=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("dry-run built a client")))
        out = io.StringIO()
        with patch.object(config, "require_anthropic", return_value=boom), \
                contextlib.redirect_stdout(out):
            rc = run_sweep.main(["--bench", "radar", "--substrate", "ca", "--k", "1",
                                 "--dry-run", "--max-tasks", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("radar-ca-agg1", out.getvalue())
        self.assertIn("# 7 tasks over 7 domains", out.getvalue())

    def test_identity_records_ca_models_and_blocks_a_model_change(self):
        a = radar.RadarAdapter(runs_root=self.root / "runs")
        args = SimpleNamespace(substrate="ca", split="all", max_tasks=1, only_domains="",
                               seed=None)
        ns = self.root / "ns"
        run_sweep._check_identity(a, args, ns)
        ident = json.loads((ns / "config.json").read_text())
        self.assertEqual(ident["executor"], config.CA_MODEL)
        self.assertEqual(ident["improver"], a.spec.channels["proposer"].model)
        self.assertEqual(ident["judge"], config.RADAR_JUDGE_MODEL)
        with patch.object(config, "CA_MODEL", "claude-opus-5-5"):
            with self.assertRaises(ValueError):
                run_sweep._check_identity(a, args, ns)

    def test_preflight_demands_gold_for_judging(self):
        from harness2 import preflight

        env = {"ANTHROPIC_API_KEY": "sk-test-not-real"}
        with patch.object(config, "require_anthropic"), patch.dict("os.environ", env):
            no_gold = radar.RadarAdapter(runs_root=self.root / "r")
            no_gold.gold_dir = None
            self.assertEqual(preflight.check(no_gold), [])
            errs = preflight.check(no_gold, judge=True)
            self.assertTrue(any("HARNESS2_RADAR_GOLD_DIR is required" in e for e in errs))
            gold = self.root / "gold" / TASK
            gold.mkdir(parents=True)
            (gold / "gold.json").write_text(json.dumps({"items": [{"id": "g1", "title": "x"}]}))
            ok = radar.RadarAdapter(runs_root=self.root / "r", gold_dir=self.root / "gold")
            self.assertEqual(preflight.check(ok, judge=True), [])
            self.assertEqual(ok.graded_tasks(), [TASK])

    def test_cli_cell_wiring(self):
        self.assertIn("radar", C.BENCHES)
        self.assertIn("ca", C.SUBSTRATES)
        self.assertIsInstance(C.make_adapter("radar", None, "ca"), radar.RadarAdapter)
        with self.assertRaises(ValueError):
            C.make_adapter("radar", None, "oc")
        self.assertEqual(C.judge_model_for(C.make_adapter("radar", None, "ca")),
                         config.RADAR_JUDGE_MODEL)

    def test_review_roles_default_to_opus_and_the_executor_to_sonnet(self):
        # An empty variable reads as unset, and load_dotenv's setdefault keeps it empty.
        unset = {v: "" for v in ("HARNESS2_CA_MODEL", "HARNESS2_CA_IMPROVER_MODEL",
                                 "HARNESS2_JUDGE_MODEL")}
        self.addCleanup(importlib.reload, config)
        with patch.dict("os.environ", unset):
            importlib.reload(config)
            got = (config.CA_MODEL, config.CA_IMPROVER_MODEL,
                   config.PROPOSER_MODEL_BY_CHANNEL["claude_api"], config.RADAR_JUDGE_MODEL)
        self.assertEqual(got, ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5",
                               "claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
