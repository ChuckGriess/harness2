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

# AI Radar sandbox image: harness2 with the [claude] extra, run as a non-root user.
# It carries no secrets: credentials arrive at run time through compose's env_file.
# Build from the repository root (docker compose -f docker/compose.yaml build).

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 radar

WORKDIR /opt/harness2
COPY pyproject.toml README.md MANIFEST.in LICENSE ./
COPY harness2 ./harness2
COPY scripts/radar_gold.py ./scripts/radar_gold.py
RUN pip install '.[claude]' && rm -rf build

ENV HOME=/tmp \
    HARNESS2_RUNS_DIR=/work/runs
WORKDIR /work
USER radar
CMD ["harness2-check", "--bench", "radar", "--substrate", "ca"]
