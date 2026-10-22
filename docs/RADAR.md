# AI Radar

A research agent that keeps you current on AI through a watchlist of people, organizations
and topics, run on the Claude API (`ca` substrate; Sonnet 5.5 writes digests, Opus 5.5 improves, selects
and judges by default) and improved by
Harness² against the `radar` benchmark. It writes a cited Markdown digest; it sends nothing
unless you ask it to.

Design and plan: `docs/agex/specs/2026-10-20-ai-radar-claude-sandbox-design.html` and
`docs/agex/plans/2026-10-20-ai-radar-claude-sandbox-plan.html`.

## What is where

| Path | What it is |
|---|---|
| `harness2/benchmarks/radar_data/watchlist.md` | Your watchlist and the seven topics. Edit it by hand; the improver never can |
| `harness2/benchmarks/radar_data/sources.json` | Feeds, repos, pages and search-only voices per person or organization |
| `harness2/benchmarks/radar_data/radar_tasks.json` | The closed weeks that make up the benchmark (tasks = weeks x topics) |
| `harness2/benchmarks/radar_data/base/` | The base harness: prompts, triage skill, stdlib fetcher scripts |
| `docker/` | The sandbox: image, compose file, squid egress allowlist |

Adding a source: add the URL to `sources.json`, add its host to
`docker/proxy/allowlist.txt` (a test fails until you do), and run the tests.

## Daily digests

```bash
harness2-radar                                  # every topic, last 7 days, base harness
harness2-radar --topics frontier-applications --until 2026-10-18
harness2-radar --harness <adopted harness dir>  # use a harness Harness² adopted
```

Output: `runs/radar/digests/<date>.md`, with the estimated cost on the last lines. Each topic's
run directory (`runs/radar/runs/<date>/<topic>/`) holds `trajectory.jsonl` and `result.json`.

## Delivery

Delivery happens only with `--deliver`, only from host code (the agent has no way to send),
and every setting it needs is checked before the first model call. It prints a preview before
sending.

```bash
harness2-radar --deliver slack
harness2-radar --send runs/radar/digests/2026-10-18.md --deliver email
```

The sandbox has no SMTP egress, so send email from the host with `--send`. Slack works inside
the sandbox when the proxy's delivery list is switched on (see below).

## Improving the agent with Harness²

The benchmark grades a digest against a gold list of must-know items for that week and
topic. Gold lists stay outside the repository and never reach the agent or the improver.

1. Draft, prune, promote gold lists (free; fetches public feeds only):

   ```bash
   export HARNESS2_RADAR_GOLD_DIR=~/radar-gold
   python scripts/radar_gold.py --task 2026-10-12/frontier-applications
   # edit ~/radar-gold/2026-10-12/frontier-applications/gold.draft.json: delete noise, add
   # items from page-only and search-only sources the fetchers cannot see
   python scripts/radar_gold.py --task 2026-10-12/frontier-applications --promote
   ```

   Drafts come from the same fetchers the base agent uses, so an unpruned draft rewards an
   agent that just runs them. Pruning and adding the items the fetchers miss is what makes the
   list worth optimizing against.

2. Escalate spend deliberately. One task at budget `k` is 4k agent rollouts plus 3k improver
   sessions, plus one judge call per graded selection.

   ```bash
   harness2-check --bench radar --substrate ca
   harness2 --bench radar --substrate ca --k 1 --dry-run --max-tasks 1
   harness2 --bench radar --substrate ca --k 1 --max-tasks 1 --only-domains frontier-applications
   harness2-judge --bench radar --substrate ca --k 1
   harness2-report --bench radar --substrate ca --k 1
   ```

   Use the same flags for all three commands, and a fresh `HARNESS2_RUNS_DIR` when you change a
   model. A judge failure is ungraded and leaves the denominator; it is never a zero.

3. See what the loop improved, round by round. Run sequential mode at `k >= 2` (k=1 gives
   only base and one composed harness), then score every rollout — one more judge call each,
   after selection, so the scores never reach the selector or the improver — and render:

   ```bash
   harness2 --bench radar --substrate ca --k 2 --modes sequential --max-tasks 1 \
     --only-domains frontier-applications
   harness2-judge  --bench radar --substrate ca --k 2 --modes sequential --rollouts
   harness2-report --bench radar --substrate ca --k 2 --modes sequential \
     --trajectory runs/radar/trajectory.html
   ```

   The page charts base → c1 → c2, and for every rollout shows its score, the improver's
   notes, the harness diff each composed round introduced, and the digest it wrote.

## The sandbox

```bash
docker compose -f docker/compose.yaml build
docker compose -f docker/compose.yaml run --rm harness2                   # harness2-check
docker compose -f docker/compose.yaml run --rm harness2 harness2-radar
HARNESS2_RADAR_GOLD_HOST_DIR=~/radar-gold \
  docker compose -f docker/compose.yaml run --rm harness2 harness2 --bench radar --substrate ca --k 1 --max-tasks 1
RADAR_DELIVERY_ALLOWLIST=./proxy/delivery.txt \
  docker compose -f docker/compose.yaml run --rm harness2 harness2-radar --deliver slack
```

- The `harness2` container sits on an internal-only network: no default route, no external
  DNS. Its only egress is the squid proxy, which allows CONNECT to the hosts in
  `docker/proxy/allowlist.txt` and refuses everything else with 403.
- Anthropic's `web_search` and `web_fetch` run on Anthropic's side, so the agent can read the
  open web while the container cannot.
- Slack is reachable only when `RADAR_DELIVERY_ALLOWLIST=./proxy/delivery.txt` is set on the
  same command (compose recreates the proxy when the setting changes).
- Non-root user, read-only root filesystem, `/tmp` as tmpfs, all capabilities dropped. Output
  goes to `runs/docker/` on the host; gold mounts read-only at `/gold`.
- Credentials come from `../.env` at run time. The image contains no secrets and no `.env`;
  `.dockerignore` admits only the package, `pyproject.toml` and `scripts/radar_gold.py`.

## Costs

Rollout cost is estimated from token usage and `config.CLAUDE_PRICES` (Sonnet 5.5: $2 input,
$10 output, $0.10 cache read per million tokens; Opus 5.5, the improver, selector and judge
default, is twice that; web search $10 per 1,000) and written to each
run's `result.json` and the digest footer. Per-rollout caps: 60 turns, $3, 20 searches; the
improver session cap is $5. Core's ledger does not total rollout cost; read it from
`result.json`.
