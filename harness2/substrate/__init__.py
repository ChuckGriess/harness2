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

"""The four solver substrates behind one interface (substrate/base.py).

`OpencodeSubstrate` carries OPENCODE_SURFACE and three install modes; `CodexSubstrate`
carries CODEX_SURFACE and one; `CaSubstrate` (the Claude API tool loop) carries CA_SURFACE and
one; `CmaSubstrate` (Claude Managed Agents sessions) reuses CA_SURFACE. Benchmark adapters
supply the launcher body.
"""

from .base import Install, Launcher, RunRequest, Substrate
from .claude_api import CA_SURFACE, CaSubstrate
from .codex import CODEX_SURFACE, CodexSubstrate
from .managed_agents import CmaSubstrate
from .opencode import OPENCODE_SURFACE, OpencodeSubstrate

__all__ = [
    "Install",
    "Launcher",
    "RunRequest",
    "Substrate",
    "OpencodeSubstrate",
    "OPENCODE_SURFACE",
    "CodexSubstrate",
    "CODEX_SURFACE",
    "CaSubstrate",
    "CA_SURFACE",
    "CmaSubstrate",
]
