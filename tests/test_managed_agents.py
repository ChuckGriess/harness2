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

"""Regressions for the `cma` substrate (Claude Managed Agents) and the scheduled digest.

A scripted fake client stands in for the SDK; no test reaches a live model or the network.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2 import config
from harness2.benchmarks import radar
from harness2.cli import _common as C
from harness2.cli import radar as radar_cli
from harness2.cli import setup as setup_cli
from harness2.substrate import managed_agents as MA
from tests.test_radar import DIGEST, GOLD, SENTINEL, TASK, FakeJudge, verdict


def ev(i, type_, **kw):
    return {"id": f"sevt_{i}", "type": type_, **kw}


def happy_events():
    return [
        ev(1, "session.status_running"),
        ev(2, "span.model_request_end"),
        ev(3, "agent.tool_use", name="bash",
           input={"command": "cd /mnt/session/uploads/workspace && python3 scripts/hn.py"},
           evaluated_permission="allow"),
        ev(4, "agent.tool_result", tool_use_id="sevt_3",
           content=[{"type": "text", "text": "2026-10-16 | hn | Beam | https://x"}]),
        ev(5, "span.model_request_end"),
        ev(6, "agent.message", content=[{"type": "text", "text": "Digest written."}]),
        ev(7, "session.status_idle", stop_reason={"type": "end_turn"}),
    ]


class FakeCma:
    """Scripted stand-in for the Managed Agents namespaces of anthropic.Anthropic()."""

    def __init__(self, events=None, *, outputs=None, empty_lists=0, fail_create=None,
                 cost_cents="123"):
        self.events = list(events if events is not None else happy_events())
        self.outputs = {"digest.md": DIGEST.encode()} if outputs is None else outputs
        self.empty_lists = empty_lists
        self.fail_create = fail_create
        self.cost_cents = cost_cents
        self.calls: list[tuple[str, dict]] = []
        self.uploaded: dict[str, bytes] = {}
        self.listings: dict[str, list] = {}
        self.created: dict[str, dict] = {}
        events_ns = SimpleNamespace(stream=self._stream, send=self._rec("events.send"),
                                    list=lambda **kw: self._rec_list("events.list", kw,
                                                                     self.events * 2))
        self.beta = SimpleNamespace(
            sessions=SimpleNamespace(create=self._create_session, retrieve=self._retrieve,
                                     events=events_ns),
            files=SimpleNamespace(upload=self._upload, list=self._list_files,
                                  download=self._download),
            agents=self._crud("agents", version=3),
            environments=self._crud("environments"),
            vaults=self._crud("vaults"),
            deployments=self._crud("deployments"),
            deployment_runs=self._crud("deployment_runs"),
        )
        self.beta.vaults.credentials = self._crud("credentials")

    def _rec(self, name, ret=None):
        def f(*a, **kw):
            self.calls.append((name, {"args": a, **kw}))
            return ret if ret is not None else {}
        return f

    def _rec_list(self, name, kw, rows):
        self.calls.append((name, kw))
        return list(rows)

    def _crud(self, kind, **extra):
        def create(**kw):
            self.calls.append((f"{kind}.create", kw))
            obj = {"id": f"{kind}_{len(self.calls)}", **extra, **kw}
            self.created.setdefault(kind, {})[obj["id"]] = obj
            return obj

        def update(obj_id, **kw):
            self.calls.append((f"{kind}.update", {"id": obj_id, **kw}))
            return {"id": obj_id, **extra, **kw}

        return SimpleNamespace(
            create=create, update=update,
            list=lambda **kw: self._rec_list(f"{kind}.list", kw, self.listings.get(kind, [])),
        )

    def _create_session(self, **kw):
        self.calls.append(("sessions.create", kw))
        if self.fail_create:
            raise self.fail_create
        return {"id": "sesn_1", "status": "idle"}

    @contextlib.contextmanager
    def _stream(self, **kw):
        self.calls.append(("events.stream", kw))
        yield iter(self.events)

    def _retrieve(self, **kw):
        return {"id": kw["session_id"],
                "usage": {"list_cost": {"amount": self.cost_cents, "currency": "USD"}}}

    def _upload(self, *, file):
        name, fh = file
        fid = f"file_{len(self.uploaded) + 1}"
        self.uploaded[fid] = fh.read()
        self.calls.append(("files.upload", {"name": name}))
        return {"id": fid}

    def _list_files(self, **kw):
        """Like the real listing: the session's uploaded inputs, not downloadable, plus outputs."""
        self.calls.append(("files.list", kw))
        inputs = [{"id": fid, "filename": f"in{fid}", "downloadable": False}
                  for fid in self.uploaded]
        if self.empty_lists:
            self.empty_lists -= 1
            return inputs
        return inputs + [{"id": f"out_{n}", "filename": n, "downloadable": True}
                         for n in self.outputs]

    def _download(self, file_id):
        if not file_id.startswith("out_"):
            raise ConnectionError(f"File `{file_id}` is not downloadable (file_not_downloadable)")
        return self.outputs[file_id.removeprefix("out_")]

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


class CmaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gold_dir = self.root / "gold"
        (self.gold_dir / TASK).mkdir(parents=True)
        (self.gold_dir / TASK / "gold.json").write_text(json.dumps({"items": GOLD}))

    def adapter(self, client, judge_client=None):
        return radar.RadarAdapter(substrate="cma", client=client, judge_client=judge_client,
                                  runs_root=self.root / "runs", gold_dir=self.gold_dir)

    def rollout(self, client, timeout=60):
        a = self.adapter(client)
        install = a._install(None)
        self.addCleanup(lambda: __import__("shutil").rmtree(install.staging_dir, True))
        rd = self.root / "rd"
        res = a.substrate.rollout("TASK", install, rd, timeout=timeout)
        return res, rd

    def test_rollout_contract_caching_and_gold_isolation(self):
        client = FakeCma()
        judge_client = FakeJudge(verdict(
            [{"id": "g1", "covered": True}, {"id": "g2", "covered": False},
             {"id": "g3", "covered": False}],
            [{"title": "Beam", "valid": True, "reason": "dated, sourced"}]))
        a = self.adapter(client, judge_client)
        rd = a.execute(TASK, None, "r1")

        self.assertEqual(a.deliverable_text(rd), DIGEST)
        self.assertTrue(a.delivered(rd))
        result = json.loads((rd / "result.json").read_text())
        self.assertEqual((result["ok"], result["cost_usd"], result["turns"], result["session_id"]),
                         (True, 1.23, 2, "sesn_1"))
        self.assertEqual(a.execute(TASK, None, "r1"), rd)
        self.assertEqual(len(client.named("sessions.create")), 1, "completed rollout is reused")

        create = client.named("sessions.create")[0]
        self.assertEqual(create["budget"]["max_list_cost"],
                         {"amount": str(config.CA_MAX_USD_CENTS), "currency": "USD"})
        self.assertNotIn("vault_ids", create, "no credential may reach an optimisation sandbox")
        self.assertEqual(create["agent"]["type"], "agent_with_overrides")
        self.assertTrue(create["agent"]["system"].endswith(MA.RUNTIME_NOTE))
        self.assertIn("own bash call", MA.RUNTIME_NOTE)
        self.assertIn("never a guessed", MA.RUNTIME_NOTE)
        mounts = {r["mount_path"] for r in create["resources"]}
        self.assertTrue({"/workspace/watchlist.md", "/workspace/sources.json",
                         "/workspace/scripts/hn.py", "/workspace/systemprompt.md"} <= mounts)
        self.assertFalse(any("_composed" in m or m.endswith("README.md") for m in mounts))

        got = a.score(rd, TASK)
        self.assertEqual((got["n_passed"], got["n_criteria"]), (2, 4))
        evidence = judge_client.prompts[0]["messages"][0]["content"]
        self.assertIn("[ACTION] bash(", evidence)
        self.assertIn("[OBSERVATION] 2026-10-16 | hn | Beam", evidence)

        for blob in client.uploaded.values():
            self.assertNotIn(SENTINEL.encode(), blob)
        for name, kw in client.calls:
            with self.subTest(call=name):
                self.assertNotIn(SENTINEL, json.dumps(kw, default=str))
        for p in rd.rglob("*"):
            if p.is_file():
                self.assertNotIn(SENTINEL, p.read_text(errors="replace"))

    def test_identical_files_upload_once_across_rollouts(self):
        client = FakeCma()
        a = self.adapter(client)
        a.execute(TASK, None, "r1")
        first = len(client.uploaded)
        a.execute(TASK, None, "r2")
        self.assertGreater(first, 0)
        self.assertEqual(len(client.uploaded), first)
        self.assertEqual(len(client.named("sessions.create")), 2)

    def test_transport_failure_is_raised_and_not_cached(self):
        a = self.adapter(FakeCma(fail_create=ConnectionError("connection reset")))
        with self.assertRaises(RuntimeError) as cm:
            a.execute(TASK, None, "r2")
        self.assertIn("not cached", str(cm.exception))
        self.assertFalse((self.root / "runs/results/r2/.adapt_complete").exists())

    def test_turn_and_search_caps_interrupt_once(self):
        turns = [ev(i, "span.model_request_end") for i in range(1, 4)]
        idle = ev(9, "session.status_idle", stop_reason={"type": "end_turn"})
        with patch.object(config, "CA_MAX_TURNS", 2):
            client = FakeCma(turns + [idle])
            res, _ = self.rollout(client)
        self.assertEqual((res.stop, res.turns), ("max_turns", 3))
        sends = [e for kw in client.named("events.send") for e in kw["events"]]
        self.assertEqual([e["type"] for e in sends].count("user.interrupt"), 1)

        searches = [ev(i, "agent.tool_use", name="web_search", input={"query": "x"})
                    for i in range(1, 3)]
        with patch.object(config, "CA_MAX_SEARCHES", 1):
            res, _ = self.rollout(FakeCma(searches + [idle]))
        self.assertEqual(res.stop, "max_searches")

    def test_wall_timeout_interrupts(self):
        res, _ = self.rollout(FakeCma(), timeout=-1)
        self.assertEqual(res.stop, "timeout")

    def test_paused_tool_is_denied_and_budget_stop_is_an_error(self):
        events = [
            ev(1, "agent.tool_use", name="bash", input={"command": "rm -rf /"},
               evaluated_permission="ask"),
            ev(2, "session.status_idle", stop_reason={"type": "requires_action",
                                                      "event_ids": ["sevt_1"]}),
            ev(3, "session.status_idle", stop_reason={"type": "budget_reached"}),
        ]
        client = FakeCma(events)
        res, rd = self.rollout(client)
        confirm = [e for kw in client.named("events.send") for e in kw["events"]
                   if e["type"] == "user.tool_confirmation"]
        self.assertEqual([(c["tool_use_id"], c["result"]) for c in confirm], [("sevt_1", "deny")])
        self.assertEqual((res.ok, res.stop), (False, "budget_reached"))
        self.assertFalse(json.loads((rd / "result.json").read_text())["error"]
                         .startswith("API call failed"), "an agent-side stop is cacheable")

    def test_output_listing_lag_is_retried(self):
        with patch.object(MA.time, "sleep") as nap:
            _, rd = self.rollout(FakeCma(empty_lists=2))
        self.assertEqual(nap.call_count, 2)
        self.assertEqual((rd / "workspace/output/digest.md").read_text(), DIGEST)

    def test_bytecode_is_not_uploaded_and_cost_survives_a_late_failure(self):
        a = self.adapter(FakeCma())
        install = a._install(None)
        self.addCleanup(lambda: __import__("shutil").rmtree(install.staging_dir, True))
        cache = install.staging_dir / "scripts" / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "hn.cpython-312.pyc").write_bytes(b"\x00bytecode")
        client = FakeCma()
        client._download = lambda file_id: (_ for _ in ()).throw(ConnectionError("reset"))
        client.beta.files.download = client._download
        res = MA.CmaSubstrate(client=client, cache_dir=self.root).rollout(
            "TASK", install, self.root / "rd2", timeout=60)
        mounts = {r["mount_path"] for r in client.named("sessions.create")[0]["resources"]}
        self.assertFalse(any("__pycache__" in m or m.endswith(".pyc") for m in mounts))
        self.assertTrue(res.error.startswith("API call failed"))
        self.assertEqual(res.cost_usd, 1.23, "a failure after the session ran keeps its cost")

    def test_render_events_grammar_dedupe_and_truncation(self):
        path = self.root / "t.jsonl"
        MA._write_jsonl(path, happy_events() + [ev(8, "session.error", error="boom")])
        out = MA.render_events(path, obs_trunc=1000, max_chars=10_000)
        self.assertEqual(out.splitlines()[0][:14], "[ACTION] bash(")
        self.assertIn("[ASSISTANT] Digest written.", out)
        self.assertIn("[ERROR] boom", out)
        short = MA.render_events(path, obs_trunc=1000, max_chars=20)
        self.assertIn("render truncation", short)
        self.assertEqual(len(MA._all_events(FakeCma(), "sesn_1")), len(happy_events()))

    def test_preflight_names_the_missing_setup_ids(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HARNESS2_CMA_AGENT_ID", "HARNESS2_CMA_ENVIRONMENT_ID")}
        env["ANTHROPIC_API_KEY"] = "test-key"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(config.ConfigError) as cm:
                MA.CmaSubstrate().preflight()
        self.assertIn("harness2-setup radar --substrate cma", str(cm.exception))

    def test_cma_is_radar_only(self):
        self.assertEqual(C.make_adapter("radar", None, "cma").substrate.name, "cma")
        with self.assertRaises(ValueError):
            C.make_adapter("jb", None, "cma")

    def test_setup_is_idempotent_and_unrestricted(self):
        client = FakeCma()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup_cli.main(["radar", "--substrate", "cma"], client=client), 0)
        env = client.named("environments.create")[0]
        self.assertEqual(env["config"]["networking"], {"type": "unrestricted"})
        agent = client.named("agents.create")[0]
        self.assertEqual(agent["model"], {"id": config.CA_MODEL, "effort": config.CA_EFFORT})
        client.listings = {k: list(v.values()) for k, v in client.created.items()}
        with contextlib.redirect_stdout(io.StringIO()):
            setup_cli.main(["radar", "--substrate", "cma"], client=client)
        self.assertEqual(len(client.named("agents.create")), 1)
        self.assertEqual(len(client.named("agents.update")), 1)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                setup_cli.main(["jb", "--substrate", "cma"])


class DigestDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_vault_setup_never_echoes_the_token(self):
        token = "xoxb-SECRET-test-token"
        client = FakeCma()
        out = io.StringIO()
        with patch.dict(os.environ, {"HARNESS2_RADAR_SLACK_BOT_TOKEN": token}):
            with contextlib.redirect_stdout(out):
                self.assertEqual(radar_cli.main(["--vault-setup"], client=client), 0)
        self.assertNotIn(token, out.getvalue())
        cred = client.named("credentials.create")[0]["auth"]
        self.assertEqual((cred["secret_name"], cred["secret_value"]), ("SLACK_BOT_TOKEN", token))
        self.assertEqual(cred["networking"], {"type": "limited", "allowed_hosts": ["slack.com"]})
        self.assertEqual(cred["injection_location"], {"header": True})

    def test_promote_pins_version_and_attaches_vault_budget_schedule(self):
        client = FakeCma()
        env = {"HARNESS2_CMA_VAULT_ID": "vlt_1", "HARNESS2_RADAR_SLACK_CHANNEL": "C123"}
        argv = ["--promote", str(radar.DATA / "base"), "--topics", "frontier-applications",
                "--cron", "30 6 * * 1-5", "--tz", "UTC", "--out", str(self.root)]
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(radar_cli.main(argv, client=client), 0)

        agent = client.named("agents.create")[0]
        self.assertEqual(agent["metadata"], {"harness2": "radar-digest"})
        self.assertEqual(agent["tools"][0]["default_config"]["permission_policy"],
                         {"type": "always_allow"})
        self.assertIn("channel C123", agent["system"])
        self.assertIn(MA.RUNTIME_NOTE, agent["system"])

        dep = client.named("deployments.create")
        self.assertEqual(len(dep), 1)
        dep = dep[0]
        self.assertEqual(dep["agent"]["version"], 3)
        self.assertEqual(dep["vault_ids"], ["vlt_1"])
        self.assertEqual(dep["budget"]["max_list_cost"]["amount"], str(config.CA_MAX_USD_CENTS))
        self.assertEqual(dep["schedule"],
                         {"type": "cron", "expression": "30 6 * * 1-5", "timezone": "UTC"})
        prompt = dep["initial_events"][0]["content"][0]["text"]
        self.assertIn("the 7 days ending today", prompt)
        self.assertIn("Topic: frontier-applications", prompt)

        client.listings["deployments"] = [{"id": "depl_1", "metadata": {
            "harness2": "radar-digest", "topic": "frontier-applications"}}]
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            radar_cli.main(argv, client=client)
        self.assertEqual(len(client.named("deployments.create")), 1, "re-promote updates")
        self.assertEqual(client.named("deployments.update")[0]["id"], "depl_1")

    def test_promote_requires_vault_and_channel_before_any_call(self):
        client = FakeCma()
        env = {k: v for k, v in os.environ.items()
               if k not in ("HARNESS2_CMA_VAULT_ID", "HARNESS2_RADAR_SLACK_CHANNEL")}
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(radar_cli.main(["--promote", str(radar.DATA / "base")],
                                            client=client), 1)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
