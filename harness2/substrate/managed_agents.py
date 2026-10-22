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

"""CmaSubstrate: Claude Managed Agents sessions as a solver substrate.

Same harness tree, same CA_SURFACE and the same on-disk rollout contract as `ca`
(trajectory.jsonl, result.json, workspace/output/), so a benchmark adapter scores both alike;
only where the loop runs changes. Anthropic runs the agent loop and a per-session cloud
sandbox; this module owns the data plane only. The agent and environment are created once by
`harness2-setup radar --substrate cma` and referenced by id, never created per rollout.

Each rollout carries its candidate harness as a per-session `agent_with_overrides` system
prompt (no agent-version churn during recursion) plus RUNTIME_NOTE, a fixed, non-editable
mapping from the harness's tool vocabulary to the managed toolset, so one base harness serves
both substrates. Workspace files are uploaded and mounted read-only; the deliverable is
written to /mnt/session/outputs and downloaded into run_dir/workspace/output.

Caps: USD is a platform-enforced session budget; turns, web searches/fetches and wall time are
counted client-side and end the session with `user.interrupt`. Tool calls that pause for
approval are denied (nobody watches a rollout). A transport failure is recorded as
"API call failed: ..." so the adapter raises instead of caching it. Rollout sessions never
carry vault_ids: no credential reaches an optimisation sandbox.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from .. import config
from ..core.tree import SurfaceSpec
from .base import Install, Launcher
from .claude_api import (
    _TRUNC_FMT,
    CA_SURFACE,
    OUTPUT,
    RESULT,
    TRAJECTORY,
    WORKSPACE,
    CaSubstrate,
    _clip,
)

MA_BETA = "managed-agents-2026-04-01"
UPLOADS = "/mnt/session/uploads/workspace"
OUTPUTS = "/mnt/session/outputs"
FILE_CACHE = "cma-files.json"
_OUTPUT_LIST_TRIES = 3

RUNTIME_NOTE = f"""# RUNTIME (managed sandbox)
Your workspace files are mounted read-only at {UPLOADS}. The harness above names tools from
another runtime; here they map as follows:
- `run_script <name> <args>`: run `cd {UPLOADS} && python3 scripts/<name> <args>` with bash.
- `load_skill <name>`: read {UPLOADS}/skills/<name>/SKILL.md.
- `read_file` / `list_dir`: read, glob or grep under {UPLOADS}.
- `write_file output/<path>`: write {OUTPUTS}/<path>. The deliverable output/digest.md is
  {OUTPUTS}/digest.md; only files under {OUTPUTS} are collected.
- web_search and web_fetch work as described, with one limit: web_fetch opens only URLs shown
  in an earlier successful tool result (script output or a search result), never a guessed
  one. Run each script in its own bash call, so one failing script cannot hide the URLs the
  others printed.
"""


class CmaSubstrate(CaSubstrate):
    """Managed Agents sessions behind the one Substrate interface; reuses ca's surface."""

    name = "cma"

    def __init__(
        self,
        launcher: Launcher | None = None,
        *,
        client=None,
        model: str = config.CA_MODEL,
        agent_id: str | None = None,
        environment_id: str | None = None,
        cache_dir: Path | None = None,
    ):
        super().__init__(launcher, client=client, model=model, server_tools=())
        self.agent_id = agent_id or config.CMA_AGENT_ID
        self.environment_id = environment_id or config.CMA_ENVIRONMENT_ID
        self._cache_path = Path(cache_dir or config.RUNS) / FILE_CACHE
        self._cache_lock = threading.Lock()

    def surface(self) -> SurfaceSpec:
        return CA_SURFACE

    # ---- per-rollout data plane -------------------------------------------------------

    def rollout(self, prompt: str, install: Install, run_dir: Path, *, timeout: int):
        """Run one managed session; same on-disk contract and return shape as ca's rollout."""
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        ws = run_dir / WORKSPACE
        if ws.exists():
            shutil.rmtree(ws)
        (ws / OUTPUT).mkdir(parents=True)
        system = (install.staging_dir / "_composed" / "system.md").read_text(encoding="utf-8")
        client = self._client or config.require_anthropic().Anthropic()
        st = SimpleNamespace(turns=0, searches=0, fetches=0, stop="", error="", sid="")
        try:
            resources = self._upload(client, install.staging_dir)
            session = as_dict(client.beta.sessions.create(
                agent={"type": "agent_with_overrides", "id": self.agent_id,
                       "system": f"{system}\n\n---\n\n{RUNTIME_NOTE}"},
                environment_id=self.environment_id,
                resources=resources,
                budget={"type": "limit",
                        "max_list_cost": {"amount": str(config.CA_MAX_USD_CENTS),
                                          "currency": "USD"}},
                title=f"harness2 {run_dir.name}",
                metadata={"harness2": "radar-rollout", "run": run_dir.name[:500]},
            ))
            st.sid = session["id"]
            print(f"  [cma] {run_dir.name}: {_console(st.sid)}", flush=True)
            self._drive(client, st, prompt, deadline=time.monotonic() + float(timeout))
            events = _all_events(client, st.sid)
            _write_jsonl(run_dir / TRAJECTORY, events)
            self._collect(client, st.sid, ws / OUTPUT)
        except Exception as e:  # transport and API errors are not agent behaviour
            if not _is_api_error(e):
                raise
            st.error = f"API call failed: {type(e).__name__}: {e}"
            if st.sid and not (run_dir / TRAJECTORY).exists():
                _write_jsonl(run_dir / TRAJECTORY, [{"type": "harness2.error", "error": st.error}])
        cost = self._list_cost(client, st.sid)
        res = SimpleNamespace(
            ok=not st.error, error=st.error or None, stop=st.stop or "end_turn",
            turns=st.turns, cost_usd=cost, text="",
        )
        (run_dir / RESULT).write_text(
            json.dumps(
                {"ok": res.ok, "error": res.error, "stop": res.stop, "turns": res.turns,
                 "cost_usd": round(cost, 6), "model": self.model, "session_id": st.sid},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return res

    @staticmethod
    def _list_cost(client, sid: str) -> float:
        """The session's reported list cost in USD, even after a failure; 0.0 if unreadable."""
        if not sid:
            return 0.0
        try:
            usage = as_dict(client.beta.sessions.retrieve(session_id=sid)).get("usage") or {}
        except Exception as e:
            if not _is_api_error(e):
                raise
            print(f"  [cma] could not read the cost of {sid}: {type(e).__name__}", flush=True)
            return 0.0
        return int((usage.get("list_cost") or {}).get("amount") or 0) / 100

    def _drive(self, client, st, prompt: str, *, deadline: float) -> None:
        """Stream-first: open the stream, send the task, enforce the client-side caps."""
        events = client.beta.sessions.events
        interrupted = False

        def interrupt(reason: str) -> None:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                st.stop = reason
                events.send(session_id=st.sid, events=[{"type": "user.interrupt"}])

        with events.stream(session_id=st.sid) as stream:
            events.send(session_id=st.sid, events=[
                {"type": "user.message", "content": [{"type": "text", "text": prompt}]},
            ])
            for raw in stream:
                ev = as_dict(raw)
                t = ev.get("type", "")
                if time.monotonic() > deadline:
                    interrupt("timeout")
                if t == "span.model_request_end":
                    st.turns += 1
                    if st.turns >= config.CA_MAX_TURNS:
                        interrupt("max_turns")
                elif t == "agent.tool_use":
                    if ev.get("name") == "web_search":
                        st.searches += 1
                        if st.searches > config.CA_MAX_SEARCHES:
                            interrupt("max_searches")
                    elif ev.get("name") == "web_fetch":
                        st.fetches += 1
                        if st.fetches > config.CA_MAX_FETCHES:
                            interrupt("max_fetches")
                    if ev.get("evaluated_permission") == "ask":
                        events.send(session_id=st.sid, events=[{
                            "type": "user.tool_confirmation", "tool_use_id": ev["id"],
                            "result": "deny",
                            "deny_message": "No operator is watching this run; tool denied.",
                        }])
                elif t == "session.error":
                    st.error = st.error or f"session error: {_clip(ev.get('error'), 300)}"
                elif t == "session.status_idle":
                    reason = (ev.get("stop_reason") or {}).get("type", "")
                    if reason == "requires_action":
                        continue
                    if reason in ("budget_reached", "refusal") and not st.stop:
                        st.stop = reason
                        st.error = st.error or f"session stopped: {reason}"
                    break
                elif t == "session.status_terminated":
                    st.stop = st.stop or "terminated"
                    break

    def _upload(self, client, staged: Path) -> list[dict]:
        """Upload every workspace file once per content hash; mount under /workspace."""
        resources = []
        for p in sorted(staged.rglob("*")):
            rel = p.relative_to(staged).as_posix()
            if (not p.is_file() or rel.startswith("_composed/") or p.name == "README.md"
                    or "__pycache__" in p.parts or p.suffix == ".pyc"):
                continue
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            with self._cache_lock:
                cache = _read_cache(self._cache_path)
                fid = cache.get(digest)
            if fid is None:
                with p.open("rb") as fh:
                    fid = as_dict(client.beta.files.upload(file=(p.name, fh)))["id"]
                with self._cache_lock:
                    cache = _read_cache(self._cache_path)
                    cache[digest] = fid
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
            resources.append({"type": "file", "file_id": fid, "mount_path": f"/workspace/{rel}"})
        return resources

    def _collect(self, client, sid: str, out: Path) -> None:
        """Download /mnt/session/outputs into `out`; retry the brief post-idle indexing lag.

        The session's listing also holds the uploaded inputs, marked not downloadable; only
        files the agent produced are downloadable, so that flag selects the outputs.
        """
        files = []
        for attempt in range(_OUTPUT_LIST_TRIES):
            files = [f for f in map(as_dict, client.beta.files.list(scope_id=sid, betas=[MA_BETA]))
                     if f.get("downloadable", True)]
            if files:
                break
            time.sleep(1.5 * (attempt + 1))
        for f in files:
            rel = Path(str(f.get("filename", f["id"])).lstrip("/"))
            if ".." in rel.parts:
                continue
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            body = client.beta.files.download(f["id"])
            data = body.read() if hasattr(body, "read") else body
            dest.write_bytes(data if isinstance(data, bytes) else str(data).encode())

    # ---- read side ----------------------------------------------------------------------

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400
    ) -> str:
        return render_events(
            Path(stream_path), obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def preflight(self) -> None:
        """SDK extra, a credential NAME, and the setup ids; calls no model."""
        super().preflight()
        for name in ("HARNESS2_CMA_AGENT_ID", "HARNESS2_CMA_ENVIRONMENT_ID"):
            config.require(name, needed_by="the cma substrate (run harness2-setup radar "
                                          "--substrate cma once to create them)")


def as_dict(obj) -> dict:
    """SDK model or plain dict -> dict (the fake clients in tests return dicts)."""
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict", "model_dump"):
        if hasattr(obj, attr):
            return getattr(obj, attr)()
    return dict(vars(obj))


def _is_api_error(e: Exception) -> bool:
    try:
        anthropic = config.require_anthropic()
    except config.ConfigError:
        return isinstance(e, (ConnectionError, TimeoutError))
    return isinstance(e, (anthropic.APIError, ConnectionError, TimeoutError))


def _all_events(client, sid: str) -> list[dict]:
    """The complete, de-duplicated event history; the stream may have dropped some."""
    seen, out = set(), []
    for raw in client.beta.sessions.events.list(session_id=sid):
        ev = as_dict(raw)
        if ev.get("id") in seen:
            continue
        seen.add(ev.get("id"))
        out.append(ev)
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, default=str, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _console(sid: str) -> str:
    ws = config.env("HARNESS2_CMA_WORKSPACE", "default")
    return f"https://platform.claude.com/workspaces/{ws}/sessions/{sid}"


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, default=str)
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(str(b.get("text", "")))
        elif b.get("type") in ("search_result", "web_search_result"):
            parts.append(f"{b.get('title', '')} — {b.get('url', b.get('source', ''))}")
        else:
            parts.append(json.dumps(b, default=str, ensure_ascii=False))
    return "\n".join(parts)


def render_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400) -> str:
    """Render a managed session's event log into the shared [ACTION]/[OBSERVATION] grammar."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(failed to read trajectory: {e})"
    lines: list[str] = []
    for ln in raw.splitlines():
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type", "")
        if t == "agent.message":
            text = _block_text(ev.get("content"))
            if text.strip():
                lines.append(f"[ASSISTANT] {_clip(text, obs_trunc)}")
        elif t == "agent.thinking":
            text = _block_text(ev.get("content")) or str(ev.get("thinking", ""))
            if text.strip():
                lines.append(f"[THINKING] {_clip(text, obs_trunc)}")
        elif t in ("agent.tool_use", "agent.mcp_tool_use"):
            args = json.dumps(ev.get("input"), default=str, ensure_ascii=False)
            lines.append(f"[ACTION] {ev.get('name', 'tool')}({_clip(args, args_trunc)})")
        elif t in ("agent.tool_result", "agent.mcp_tool_result"):
            tag = "[OBSERVATION ERROR]" if ev.get("is_error") else "[OBSERVATION]"
            lines.append(f"{tag} {_clip(_block_text(ev.get('content')), obs_trunc)}")
        elif t in ("session.error", "harness2.error"):
            lines.append(f"[ERROR] {_clip(ev.get('error'), 300)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        dropped = len(rendered) - max_chars
        rendered = rendered[:max_chars] + "\n" + _TRUNC_FMT.format(n=dropped)
    return rendered


def setup_resources(client, *, kind: str = "radar-executor", system: str | None = None,
                    permission: str = "auto") -> tuple[dict, dict]:
    """Create or update the cma agent and environment, found by metadata. Control plane only.

    Returns (agent, environment) as dicts. Idempotent: a second run updates in place (a new
    agent version), it never creates a duplicate.
    """
    env_body = {"name": f"harness2-{kind}",
                "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
                "metadata": {"harness2": kind}}
    agent_body = {
        "name": f"harness2-{kind}",
        "model": {"id": config.CA_MODEL, "effort": config.CA_EFFORT},
        "tools": [{
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": permission}},
            "configs": [{"name": "web_fetch", "enabled": True, "max_content_tokens": 20_000}],
        }],
        "metadata": {"harness2": kind},
    }
    if system is not None:
        agent_body["system"] = system
    env = _find(client.beta.environments.list(), kind)
    env = (as_dict(client.beta.environments.update(env["id"], **env_body))
           if env else as_dict(client.beta.environments.create(**env_body)))
    agent = _find(client.beta.agents.list(), kind)
    agent = (as_dict(client.beta.agents.update(agent["id"], **agent_body))
             if agent else as_dict(client.beta.agents.create(**agent_body)))
    return agent, env


def _find(listing, kind: str) -> dict | None:
    for raw in listing:
        obj = as_dict(raw)
        if (obj.get("metadata") or {}).get("harness2") == kind and not obj.get("archived_at"):
            return obj
    return None
