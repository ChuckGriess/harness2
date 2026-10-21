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

"""Regressions for the Claude API tool loop and the `ca` substrate, against a scripted client."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2 import claude_loop as CL
from harness2 import config
from harness2.core import tree
from harness2.substrate.claude_api import CA_SURFACE, CaSubstrate

MODEL = "claude-sonnet-5-5"
ECHO = "import sys\nprint(' '.join(sys.argv[1:]))\n"
ENVDUMP = "import os\nprint(os.environ.get('ANTHROPIC_API_KEY', 'none'))\n"


class FakeClient:
    """Stands in for anthropic.Anthropic: returns scripted responses, records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        r = self.responses.pop(0) if self.responses else self.default()
        if isinstance(r, Exception):
            raise r
        return r

    def default(self):
        raise AssertionError("FakeClient ran out of scripted responses")


def resp(content, stop="end_turn", usage=None, **extra):
    return {"model": MODEL, "content": content, "stop_reason": stop,
            "usage": usage or {"input_tokens": 10, "output_tokens": 1}, **extra}


def tool_use(tid, name, args):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


class ClaudeLoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ws = self.root / "ws"
        (self.ws / "output").mkdir(parents=True)
        (self.ws / "scripts").mkdir()
        (self.ws / "scripts" / "echo.py").write_text(ECHO)
        (self.ws / "scripts" / "envdump.py").write_text(ENVDUMP)
        self.box = CL.ToolBox(self.ws, writable="output",
                              scripts=frozenset({"echo.py", "envdump.py"}))

    def loop(self, client, caps=None):
        return CL.run(client, model=MODEL, system="sys", prompt="task", box=self.box,
                      caps=caps or CL.Caps(10, 5.0, 60.0), capture=self.root / "cap.jsonl")

    def test_tool_sandbox_rejects_escapes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s3cret")
        (self.ws / "link").symlink_to(outside, target_is_directory=True)
        before = sorted(p.name for p in outside.iterdir())
        bad = [
            ("read_file", {"path": "../outside/secret.txt"}),
            ("read_file", {"path": str(outside / "secret.txt")}),
            ("read_file", {"path": "link/secret.txt"}),
            ("write_file", {"path": "notes.md", "content": "x"}),
            ("write_file", {"path": "output/../notes.md", "content": "x"}),
            ("write_file", {"path": "link/pwned.txt", "content": "x"}),
            ("run_script", {"name": "evil.py", "args": []}),
            ("run_script", {"name": "../scripts/echo.py", "args": []}),
            ("load_skill", {"name": "../x"}),
            ("bash", {"command": "id"}),
        ]
        for name, args in bad:
            with self.subTest(tool=name, args=args):
                out = CL._tool_result(self.box, tool_use("t", name, args))
                self.assertTrue(out.get("is_error"), out)
                self.assertNotIn("s3cret", out["content"])
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)
        self.assertFalse((self.ws / "notes.md").exists())

    def test_scripts_run_without_credentials(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-not-real"}):
            out = CL._tool_result(self.box, tool_use("t", "run_script",
                                                     {"name": "envdump.py", "args": []}))
        self.assertNotIn("is_error", out)
        self.assertEqual(out["content"].strip(), "none")

    def test_stop_reasons(self):
        cases = {
            "refusal": ([resp([], "refusal", stop_details={"category": "cyber"})], False,
                        "refusal (category=cyber)"),
            "max_tokens": ([resp([{"type": "text", "text": "part"}], "max_tokens")], False,
                           "stop_reason=max_tokens"),
            "pause_turn": ([resp([], "pause_turn"), resp([{"type": "text", "text": "ok"}])],
                           True, ""),
        }
        for label, (script, ok, err) in cases.items():
            with self.subTest(stop=label):
                client = FakeClient(script)
                res = self.loop(client)
                self.assertEqual(res.ok, ok)
                self.assertIn(err, res.error)
                self.assertEqual(len(client.calls), len(script))
        self.assertEqual(client.calls[1]["messages"][-1]["role"], "assistant")

    def test_caps_stop_runaway(self):
        forever = FakeClient([])
        forever.default = lambda: resp([tool_use("t", "list_dir", {"path": "."})], "tool_use")
        res = self.loop(forever, CL.Caps(3, 5.0, 60.0))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "turn cap reached (3)")
        self.assertEqual(len(forever.calls), 3)
        end = json.loads((self.root / "cap.jsonl").read_text().splitlines()[-1])
        self.assertEqual((end["event"], end["ok"], end["turns"]), ("end", False, 3))

        dear = FakeClient([])
        dear.default = lambda: resp([tool_use("t", "list_dir", {"path": "."})], "tool_use",
                                    {"input_tokens": 0, "output_tokens": 1_000_000})
        res = self.loop(dear, CL.Caps(10, 1.0, 60.0))
        self.assertEqual(len(dear.calls), 1)
        self.assertIn("cost cap reached ($10.00 >= $1.00)", res.error)

    def test_api_failure_is_recorded_not_raised(self):
        res = self.loop(FakeClient([RuntimeError("boom")]))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "API call failed: RuntimeError: boom")

    def test_unlisted_model_priced_at_dearest_rate(self):
        self.assertEqual(CL.usage_cost("claude-unlisted", {"output_tokens": 1_000_000}), 20.0)
        self.assertEqual(
            CL.usage_cost(MODEL, {"server_tool_use": {"web_search_requests": 3}}), 0.03
        )


class CaSubstrateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = self.root / "harness"
        tree.write_tree(CA_SURFACE, {
            "systemprompt.md": "You track AI releases.\n",
            "guardrails.md": "- Cite every item.\n",
            "LongTermMEMORY.md": "",
            "tool_descriptions/tool_guidance.md": "",
            "skills/triage/SKILL.md": "---\nname: triage\ndescription: sorting items\n---\nSort.\n",
            "scripts/echo.py": ECHO,
        }, self.harness, stubs=False)
        self.spec = SimpleNamespace(install_mode="workspace_agents_md")

    def test_rollout_end_to_end(self):
        script = [
            resp([
                {"type": "server_tool_use", "id": "s1", "name": "web_search",
                 "input": {"query": "tinker release"}},
                {"type": "web_search_tool_result", "tool_use_id": "s1",
                 "content": [{"type": "web_search_result", "title": "T", "url": "https://t"}]},
                tool_use("t1", "run_script", {"name": "echo.py", "args": ["hi"]}),
            ], "tool_use", {"input_tokens": 1000, "output_tokens": 200,
                            "server_tool_use": {"web_search_requests": 1}}),
            resp([tool_use("t2", "write_file",
                           {"path": "output/digest.md", "content": "# Digest\n- x https://t\n"})],
                 "tool_use", {"input_tokens": 2000, "output_tokens": 100,
                              "cache_read_input_tokens": 1000}),
            resp([{"type": "text", "text": "done"}], "end_turn",
                 {"input_tokens": 100, "output_tokens": 10}),
        ]
        client = FakeClient(script)
        sub = CaSubstrate(client=client, model=MODEL)
        install = sub.materialise(self.harness, self.spec, {})
        self.addCleanup(lambda: __import__("shutil").rmtree(install.staging_dir, True))
        run_dir = self.root / "run"
        res = sub.rollout("Cover the week.", install, run_dir, timeout=60)

        self.assertTrue(res.ok, res.error)
        self.assertEqual((run_dir / "workspace/output/digest.md").read_text(),
                         "# Digest\n- x https://t\n")
        expected = ((1000 * 2 + 200 * 10) / 1e6 + 0.01
                    + (2000 * 2 + 100 * 10 + 1000 * 0.10) / 1e6
                    + (100 * 2 + 10 * 10) / 1e6)
        self.assertAlmostEqual(res.cost_usd, expected, places=9)
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual((result["ok"], result["turns"]), (True, 3))

        first = client.calls[0]
        self.assertEqual((first["model"], first["fallbacks"]), (MODEL, "default"))
        self.assertIn(CL.FALLBACK_BETA, first["betas"])
        self.assertIn("You track AI releases.", first["system"])
        self.assertIn("`triage`: sorting items", first["system"])
        self.assertIn("echo.py", first["system"])
        self.assertEqual(
            [t["name"] for t in first["tools"]],
            ["web_search", "web_fetch", "read_file", "list_dir", "write_file", "load_skill",
             "run_script"],
        )
        echoed = client.calls[1]["messages"][-1]["content"][0]
        self.assertEqual((echoed["content"].strip(), echoed.get("is_error")), ("hi", None))

        traj = sub.read_trajectory(run_dir / "trajectory.jsonl", obs_trunc=10_000,
                                   max_chars=100_000)
        self.assertIn('[ACTION] web_search({"query": "tinker release"})', traj)
        self.assertIn("[OBSERVATION] T — https://t", traj)
        self.assertIn('[ACTION] run_script({"name": "echo.py", "args": ["hi"]})', traj)
        self.assertIn("[OBSERVATION] hi", traj)
        self.assertIn("[ASSISTANT] done", traj)

    def test_server_tool_error_renders_as_error(self):
        client = FakeClient([resp([
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "s1",
             "content": {"type": "web_search_tool_result_error",
                         "error_code": "max_uses_exceeded"}},
            {"type": "text", "text": "no results"},
        ])])
        sub = CaSubstrate(client=client, model=MODEL)
        install = sub.materialise(self.harness, self.spec, {})
        self.addCleanup(lambda: __import__("shutil").rmtree(install.staging_dir, True))
        sub.rollout("q", install, self.root / "run", timeout=60)
        traj = sub.read_trajectory(self.root / "run/trajectory.jsonl", obs_trunc=1000,
                                   max_chars=10_000)
        self.assertIn("[ERROR] web_search: max_uses_exceeded", traj)
        self.assertNotIn("[OBSERVATION]", traj)

    def test_materialise_rejects_foreign_install_mode(self):
        with self.assertRaises(ValueError):
            CaSubstrate().materialise(self.harness, SimpleNamespace(install_mode="codex_root"), {})

    def test_sdk_is_optional(self):
        with patch.dict(sys.modules, {"anthropic": None}):
            with self.assertRaises(config.ConfigError) as cm:
                config.require_anthropic()
        self.assertIn("uv pip install -e '.[claude]'", str(cm.exception))
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, harness2.claude_loop, harness2.substrate; "
             "print('anthropic' in sys.modules)"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(probe.stdout.strip(), "False")

    def test_preflight_names_missing_credential(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with patch.dict(os.environ, env, clear=True), patch.object(config, "require_anthropic"):
            with self.assertRaises(RuntimeError) as cm:
                CaSubstrate().preflight()
        self.assertIn("ANTHROPIC_API_KEY is required", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
