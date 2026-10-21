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

"""One Claude API tool loop, shared by the `ca` substrate and the `claude_api` improver channel.

The model reaches the filesystem only through `ToolBox`, which confines every path to one
workspace, writes and deletes only under its `writable` prefix, and runs only the scripts it was built
with, under an environment that carries no credentials. The loop never raises on a model or
transport failure: it stops, records why in `LoopResult.error`, and leaves the capture on disk.
Turn, wall-clock and USD caps each stop it. Cost is an estimate from `config.CLAUDE_PRICES`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .core import tree as HT

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16_000
READ_CAP = 100_000
SCRIPT_OUT_CAP = 50_000
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_\-]*")
_SCRIPT_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
               "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE")


class ToolError(Exception):
    """A tool call the model got wrong; returned to it as an is_error tool_result."""


def _schema(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_PATH = {"type": "string", "description": "Path relative to the workspace root."}

TOOL_SPECS = {
    "read_file": (
        "Read a UTF-8 text file in the workspace.",
        _schema({"path": _PATH}, ["path"]),
    ),
    "list_dir": (
        "List a workspace directory ('.' for the root). Directories end with '/'.",
        _schema({"path": _PATH}, ["path"]),
    ),
    "write_file": (
        "Create or overwrite a text file. Only paths under the writable directory are accepted.",
        _schema({"path": _PATH, "content": {"type": "string"}}, ["path", "content"]),
    ),
    "delete_file": (
        "Delete a file. Only paths under the writable directory are accepted.",
        _schema({"path": _PATH}, ["path"]),
    ),
    "load_skill": (
        "Load the full instructions of a named skill.",
        _schema({"name": {"type": "string"}}, ["name"]),
    ),
    "run_script": (
        "Run one helper script from scripts/ with string arguments and return its stdout.",
        _schema(
            {
                "name": {"type": "string", "description": "Script file name, e.g. rss.py"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            ["name", "args"],
        ),
    ),
}


@dataclass(frozen=True)
class ToolBox:
    """The client tools one loop may call, all confined to `workspace`."""

    workspace: Path
    writable: str | None = None
    scripts: frozenset[str] = frozenset()
    skills_dir: str = "skills"
    script_timeout: int = config.CA_SCRIPT_TIMEOUT

    def names(self) -> list[str]:
        out = ["read_file", "list_dir"]
        if self.writable:
            out += ["write_file", "delete_file"]
        if (Path(self.workspace) / self.skills_dir).is_dir():
            out.append("load_skill")
        if self.scripts:
            out.append("run_script")
        return out

    def schemas(self) -> list[dict]:
        return [
            {"name": n, "description": TOOL_SPECS[n][0], "input_schema": TOOL_SPECS[n][1],
             "strict": True}
            for n in self.names()
        ]

    def call(self, name: str, args: dict) -> str:
        if name not in self.names():
            raise ToolError(f"unknown tool {name!r}; available: {', '.join(self.names())}")
        if not isinstance(args, dict):
            raise ToolError(f"{name} takes a JSON object, got {type(args).__name__}")
        return getattr(self, f"_{name}")(args)

    def _root(self) -> Path:
        return Path(self.workspace).resolve()

    def _resolve(self, rel) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path must be a non-empty string")
        try:
            norm = HT.normalize_rel(rel)
        except ValueError as e:
            raise ToolError(f"{e}; use a path relative to the workspace root") from None
        root = self._root()
        p = (root / norm).resolve()
        if p != root and root not in p.parents:
            raise ToolError(f"path escapes the workspace: {rel!r}")
        return p

    def _read_file(self, args: dict) -> str:
        p = self._resolve(args.get("path"))
        if not p.is_file():
            raise ToolError(f"no such file: {args.get('path')!r}")
        text = p.read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= READ_CAP else text[:READ_CAP] + "\n…(truncated)"

    def _list_dir(self, args: dict) -> str:
        p = self._resolve(args.get("path") or ".")
        if not p.is_dir():
            raise ToolError(f"no such directory: {args.get('path')!r}")
        return "\n".join(c.name + ("/" if c.is_dir() else "") for c in sorted(p.iterdir()))

    def _writable(self, tool: str, rel) -> Path:
        p = self._resolve(rel)
        if (self._root() / self.writable).resolve() not in p.parents:
            raise ToolError(f"{tool} only works under {self.writable}/; got {rel!r}")
        return p

    def _write_file(self, args: dict) -> str:
        p = self._writable("write_file", args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {p.relative_to(self._root())}"

    def _delete_file(self, args: dict) -> str:
        p = self._writable("delete_file", args.get("path"))
        if not p.is_file():
            raise ToolError(f"no such file: {args.get('path')!r}")
        p.unlink()
        return f"deleted {p.relative_to(self._root())}"

    def _load_skill(self, args: dict) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not _SKILL_NAME.fullmatch(name):
            raise ToolError(f"invalid skill name {name!r}")
        p = self._resolve(f"{self.skills_dir}/{name}/SKILL.md")
        if not p.is_file():
            raise ToolError(f"no skill named {name!r}")
        return p.read_text(encoding="utf-8", errors="replace")

    def _run_script(self, args: dict) -> str:
        name, argv = args.get("name"), args.get("args", [])
        if name not in self.scripts:
            raise ToolError(
                f"unknown script {name!r}; available: {', '.join(sorted(self.scripts)) or 'none'}"
            )
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ToolError("args must be a list of strings")
        root = self._root()
        env = {k: os.environ[k] for k in _SCRIPT_ENV if k in os.environ}
        try:
            p = subprocess.run(
                [sys.executable, str(root / "scripts" / name), *argv],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.script_timeout,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"{name} timed out after {self.script_timeout}s") from None
        except OSError as e:
            raise ToolError(f"{name} could not start: {e}") from None
        if p.returncode != 0:
            raise ToolError(f"{name} exited {p.returncode}: {(p.stderr or '')[-2000:]}")
        out = p.stdout or ""
        return out if len(out) <= SCRIPT_OUT_CAP else out[:SCRIPT_OUT_CAP] + "\n…(truncated)"


@dataclass(frozen=True)
class Caps:
    max_turns: int
    max_usd: float
    wall_s: float


@dataclass
class LoopResult:
    text: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    stop: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def usage_cost(model: str, usage: dict) -> float:
    """Estimated USD for one response's usage, web-search request fees included."""
    rates = config.CLAUDE_PRICES.get(model) or tuple(
        max(r[i] for r in config.CLAUDE_PRICES.values()) for i in range(4)
    )
    tokens = (
        usage.get("input_tokens") or 0,
        usage.get("output_tokens") or 0,
        usage.get("cache_creation_input_tokens") or 0,
        usage.get("cache_read_input_tokens") or 0,
    )
    usd = sum(t * r for t, r in zip(tokens, rates)) / 1_000_000
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return usd + searches * config.WEB_SEARCH_USD


def _as_dict(resp) -> dict:
    if isinstance(resp, dict):
        return resp
    return resp.to_dict() if hasattr(resp, "to_dict") else resp.model_dump(exclude_none=True)


def _tool_result(box: ToolBox, block: dict) -> dict:
    try:
        out, err = box.call(block.get("name", ""), block.get("input") or {}), False
    except ToolError as e:
        out, err = str(e), True
    res = {"type": "tool_result", "tool_use_id": block.get("id", ""), "content": out}
    if err:
        res["is_error"] = True
    return res


def run(
    client,
    *,
    model: str,
    system: str,
    prompt: str,
    box: ToolBox,
    caps: Caps,
    capture: Path,
    server_tools: tuple[dict, ...] = (),
    effort: str = config.CA_EFFORT,
) -> LoopResult:
    """Drive one conversation to end_turn or a cap, appending every turn to `capture` (JSONL)."""
    res = LoopResult()
    deadline = time.monotonic() + caps.wall_s
    messages: list[dict] = [{"role": "user", "content": prompt}]
    tools = [*server_tools, *box.schemas()]
    capture = Path(capture)
    capture.parent.mkdir(parents=True, exist_ok=True)
    with capture.open("w", encoding="utf-8") as log:

        def emit(rec: dict) -> None:
            log.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            log.flush()

        while True:
            if res.turns >= caps.max_turns:
                res.error = f"turn cap reached ({caps.max_turns})"
                break
            if time.monotonic() > deadline:
                res.error = f"wall-clock cap reached ({caps.wall_s:.0f}s)"
                break
            if res.cost_usd >= caps.max_usd:
                res.error = f"cost cap reached (${res.cost_usd:.2f} >= ${caps.max_usd:.2f})"
                break
            try:
                resp = _as_dict(
                    client.beta.messages.create(
                        model=model,
                        max_tokens=MAX_TOKENS,
                        system=system,
                        messages=messages,
                        tools=tools,
                        output_config={"effort": effort},
                        betas=[FALLBACK_BETA],
                        fallbacks="default",
                    )
                )
            except Exception as e:  # a failed call ends this rollout, never the sweep
                res.error = f"API call failed: {type(e).__name__}: {str(e)[:300]}"
                break
            res.turns += 1
            served_by = resp.get("model") or model
            res.cost_usd += usage_cost(served_by, resp.get("usage") or {})
            content = resp.get("content") or []
            res.stop = str(resp.get("stop_reason"))
            emit({"role": "assistant", "model": served_by, "stop_reason": res.stop,
                  "content": content})
            messages.append({"role": "assistant", "content": content})
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            if text.strip():
                res.text = text
            if res.stop == "end_turn":
                break
            if res.stop == "pause_turn":
                continue
            if res.stop == "tool_use":
                results = [_tool_result(box, b) for b in content if b.get("type") == "tool_use"]
                if not results:
                    res.error = "stop_reason tool_use without a client tool call"
                    break
                emit({"role": "user", "content": results})
                messages.append({"role": "user", "content": results})
                continue
            if res.stop == "refusal":
                category = (resp.get("stop_details") or {}).get("category")
                res.error = f"refusal (category={category})"
                break
            res.error = f"stopped early: stop_reason={res.stop}"
            break
        emit({"event": "end", "ok": res.ok, "stop": res.stop, "error": res.error,
              "turns": res.turns, "cost_usd": round(res.cost_usd, 6)})
    return res
