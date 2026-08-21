# SREGym

A procedurally generated incident-response environment for LLM agents.

Each episode builds a small but complete production stack on local disk: a FastAPI
service, SQLite databases, a git repo with a plausible history, nginx/systemd/cron
config, and hours of coherent logs and metrics. A known fault gets injected, the agent
gets paged, and a deterministic verifier (no LLM judge) decides whether it fixed the
actual root cause without breaking anything else.

There are 10 fault templates plus two-fault compositions, three difficulty profiles,
and 8 stack identities so worlds don't repeat. Seeds are reproducible. Ships with
Anthropic and OpenAI-compatible adapters, a scripted reference solver, a sweep runner,
and a [verifiers](https://github.com/PrimeIntellect-ai/verifiers) taskset published on
the [Environments Hub](https://app.primeintellect.ai/dashboard/environments/ericxu88/sregym-env).

## Quick start

Needs Python ≥ 3.10, `git`, `sqlite3`. No Docker required.

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
source .venv/bin/activate

# offline demo, no API key (~4s)
sregym run --seed 42 --agent scripted

# real models (put ANTHROPIC_API_KEY in a git-ignored ./.env, it's auto-loaded)
sregym run --seed 42 --agent anthropic --model claude-opus-5 --max-steps 30
sregym run --seed 42 --agent openai --model gpt-5.2      # $OPENAI_API_KEY / $OPENAI_BASE_URL

# sweeps: many seeds, concurrent, resumable, with a report
sregym sweep --seeds 1-200 --model claude-sonnet-5 --concurrency 4 --out sweeps/run1
sregym report sweeps/run1

# poke at a world manually
sregym generate --seed 7 --serve --reveal
```

Here's the scripted solver on seed 42 (abridged):

```
$ sregym run --seed 42 --agent scripted
[sregym] world at /tmp/sregym-42-vb6dyrff (port 53264); fault=env_var_typo seed=42
[step 1] read_logs() -> 'Available log files ...'
[step 3] read_logs(path='checkout-service/logs/app.log', grep='checkout\\.access.* 5\\d\\d ', tail=True) -> '... 1688 matching lines ...'
[step 5] read_logs(path='checkout-service/logs/deploy.log', tail=True) -> '... deploy b25be1a requested by David Guzman ...'
[step 7] run_shell(command='git -C checkout-service show HEAD') -> '... -DATABASE_URL=... +DATABSAE_URL=... '
[step 10] edit_file(path='checkout-service/.env', old_string='DATABSAE_URL=...', new_string='DATABASE_URL=...')
[step 11] restart_service() -> '... Health: GET /health -> 200'
[step 16] resolve_incident(summary="Deploy of HEAD changed checkout-service/.env: ...")
symptom_resolved=PASS  root_cause_fixed=PASS  no_collateral_damage=PASS  reward=1.00
  [x] symptom    health_ok / checkout_ok / orders_ok / users_ok
  [x] root_cause env_value_correct / app_code_unchanged / db_file_in_place
  [x] collateral files, db rows, logs, git history, no forbidden actions
```

The scripted solver is a solvability oracle, not a benchmark. Its `mask`,
`workaround`, `noop`, and `sloppy` modes exercise the failure cases the verifier has
to tell apart. It scores 1.0 on every seed.

## Results

Everything below is from `sregym sweep` with claude-sonnet-5, lean prompt, seeds 1–20
(the env_var_typo baseline used seeds 1–200). Cost is Anthropic list price with prompt
caching. CIs are Wilson 95%.

| template | budget | success | 95% CI | $/ep | notes |
|---|---|---|---|---|---|
| env_var_typo | 10 | **58.0%** (116/200) | 51–65% | $0.09 | 47 fixed_not_restarted, 37 never_found |
| ledger_divergence | 22 | **65%** (13/20) | 43–82% | $0.19 | 85% at 30 steps |
| unapplied_migration | 12 | **55%** (11/20) | 34–74% | $0.11 | 100% at 30 steps |
| cron_write_lock | 30 | **95%** (19/20) | 76–99% | $0.33 | heaviest template, ~28 steps |
| db_file_permissions | 30 | **95%** (19/20) | 76–99% | $0.28 | data-dir variant is hardest (80%) |
| bad_dependency_pin | 30 | **100%** (20/20) | 84–100% | $0.15 | every run chose fix-forward |
| rate_limit_misconfig | 30 | **100%** (20/20) | 84–100% | $0.18 | |
| disk_full | 30 | **95%** (19/20) | 76–99% | $0.25 | 1 fixed on the last step, no restart left |
| stale_secret | 30 | **100%** (20/20) | 84–100% | $0.25 | |
| truncated_env | 30 | **100%** (20/20) | 84–100% | $0.20 | |
| composed (2 faults) | 20 | **30%** (6/20) | 15–52% | | see below |

Some things we learned measuring these:

- **Budgets are the main difficulty lever for frontier models.** The env_var_typo
  ladder: 100% at 30 steps, 100% at 30 lean, 95% at 12, 65% at 10, 15% at 8
  (CIs 84–100, 84–100, 76–99, 43–82, 5–36). Failures shift from fixed_not_restarted to
  never_found as the budget shrinks. Prompt verbosity made no difference. Opus 5 also
  went 20/20 at 30 steps ($0.26/ep, ~20 steps).
- Per env_var_typo variant: value typos 74%, key-name typos 45% (the app silently
  falls back to a dev default), ledger value/key 47%/45%.
- At 12 steps, unapplied_migration separates by fix difficulty: shipped-migration
  variants 67–100%, forgotten-migration 0–33% (authoring the SQL costs 5–8 extra
  calls; fixes landed at median call 11, range 8–19).
- ledger_divergence's deferred-restart variant didn't move the number (89% vs 82% at
  30 steps): models read `git log -p -- .env` instead of trusting `git show HEAD`.
- bad_dependency_pin's first measurement came back **0/20, all "workaround"** — every
  episode migrated the callers to the new API instead of rolling the pin back. The
  unanimity was the tell: the model was right and the rubric was too opinionated. The
  verifier now accepts either coherent end state (rollback or fix-forward), and the
  re-measure is the 100% row. Calibration also caught two verifier false positives
  (a regex matching commit-message text; a path matching a git-checkout heuristic);
  `sregym rescore` re-judged the saved results.

## What the agent sees

The page never names the cause:

```
[PagerDuty] INCIDENT #4710 — TRIGGERED — P1
Service:      checkout-service (production)   Escalation policy: payments-oncall → you
Title:        [P1] checkout-service: HTTP 5xx error rate > 10% (current 95.2%)
Triggered at: 2026-08-19 00:29:25 UTC   (condition held for 5m before paging)
Details:      Alert has been firing since 00:29 UTC; symptom start ~00:23 UTC.
Support note (00:33 UTC, Zendesk #77152): "Getting a spike of tickets: checkout spinner then an error toast."
```

The system prompt covers the host layout, the tools, and what counts as a good
resolution. Both prompts are stored in the trajectory.

### Tools

| tool | notes |
|---|---|
| `read_logs` | paginated, 50 lines max per call, cursors, regex grep, since/until, tail |
| `query_metrics` | per-minute series: requests by method/path/status, latency, db errors, up, derived error rate |
| `read_file` | numbered lines |
| `edit_file` | exact-string replace, returns a diff; log files are read-only |
| `run_shell` | allow-listed: inspection commands, git (no reset/clean/push), read-only sqlite3, curl to 127.0.0.1, hash-pinned repo scripts via `python`; pipes and `;`/`&&`/`\|\|` ok, no redirection or substitution |
| `restart_service` | systemctl-style; restart re-reads `.env` |
| `resolve_incident` | terminal; the agent submits a postmortem |

## What gets generated

```
<world>/
  .sregym/              # control plane: world.json, spec.json (the answer), manifest.json
                        # sibling of host/, unreachable by the tools (tested against grep -r, globs, .., absolute paths)
  host/                 # what the agent operates on
    checkout-service/   # git repo, ~9 commits over 90 days (name varies per seed, see stack variation)
      .env  checkout/*.py  migrations/  scripts/  data/*.db  logs/
    etc/nginx  etc/systemd  etc/cron.d  var/log/nginx/  metrics/series.jsonl
```

The world generator seeds business data (a few hundred users, thousands of orders and
payments) into SQLite, and renders the app source from templates with feature sections
so each git commit plausibly adds its feature. The history generator drives one
simulated request stream into the app log, nginx logs, metrics, and the database
together, so the evidence agrees with itself — including noise: 404s, a slow-query
burst, health probes, cron output, old deploys. During the episode the service actually
runs (uvicorn subprocess, clean env, so a restart is required to pick up config),
synthetic traffic keeps flowing at ~1.5 rps, a collector scrapes `/metrics` every 10s,
and a real cron daemon runs the deployed jobs.

## Fault templates

Templates implement `inject(world, seed) -> VerificationSpec` — three declarative
check lists (symptom, root cause, collateral) plus an incident timeline. The verifier
only interprets check types; templates never touch the verifier.

- **env_var_typo** — a config deploy typo'd a database env var (value or key name;
  key typos silently fall back to a dev default). Fix the line and restart.
- **ledger_divergence** — `LEDGER_DATABASE_URL` points at a stale audit snapshot.
  Nothing errors; payments silently land in the wrong file and a freshness metric
  pages. A full fix restores the URL, restarts, and backfills the diverted payments
  with the repo's reconcile script (config+restart alone scores 0.7).
- **unapplied_migration** — a release needs a schema migration nobody ran; in 30% of
  seeds the migration file was never committed and has to be written. Reverting the
  release is scored as a workaround.
- **cron_write_lock** — a backfill cron job at `* * * * *` holds the DB write lock
  20–40s per run, so checkouts fail in bursts aligned to the minute. No deploy to
  blame. Verification probes for 65s so a restart-only fix gets caught by the next burst.
- **db_file_permissions** — the config-management agent chmod'd the data path
  read-only. Reads fine, writes 500. The trail is fleetd.log and `ls -la`; fix is chmod.
- **bad_dependency_pin** — a bumped internal package removed an API; the service
  crash-loops and stays down (real captured tracebacks, 502s at the edge). Rollback
  and fix-forward are both accepted; incoherent mixtures are not.
- **rate_limit_misconfig** — the per-user checkout rate limit got set to 1–3/min
  ("clamp to 100" says the commit message). Zero 5xx; legitimate retry bursts 429.
  Verified with a 6-request burst probe.
- **disk_full** — a capacity-guardrail deploy set the DB page quota below the
  database's current size, so every write fails with a genuine "database or disk is
  full" while `df` shows plenty of space. (Real ENOSPC needs privileged mounts, and a
  faked error string would be reward-hackable; the quota produces the real error live.)
- **stale_secret** — a quarterly rotation also rotated the webhook secret shared with
  the payment gateway. Settlement webhooks 401 and settlements silently stop while
  checkouts keep working. Restore the old secret from git history; bypassing signature
  validation in code is caught.
- **truncated_env** — deploy-bot died mid-write and left `.env` truncated on disk
  (git has the good copy). Everything below the cut falls back to dev defaults, and
  because `LOG_PATH` is gone, app.log goes dark at exactly the restart — the missing
  traceback is the clue. `git checkout -- .env` and restart. Restoring only the DB
  lines leaves dev secrets in prod and counts as incomplete.

**Composition** (`--fault composed[:pair]`) puts two faults in one world with one
page. Vetted pairs: migration+perms (the migration can't apply until the write bit is
back), ratelimit+perms, migration+cron. Checks are member-prefixed so reports show
partial progress; reward is all-or-nothing. Measured at standard difficulty:
**30%** (6/20, CI 15–52%), all failures never_found at 20 steps — 8 of the 14 had
fully fixed exactly one of the two faults, 6 had fixed neither. Per pair:
ratelimit+perms 5/8, migration+perms 1/4, migration+cron 0/8. This is the first
configuration where difficulty comes from scope rather than time pressure.

## Verifier and reward

Deterministic, runs against the live world at episode end:

- **symptom_resolved** — live probes: health 200, a synthetic checkout 201, reads 200
- **root_cause_fixed** — the specific broken thing is correct again, app code
  unchanged (hardcoding around the problem is not a fix), nothing moved to match a typo
- **no_collateral_damage** — file hashes match the manifest (minus allowed files), DB
  rows/schema intact (additive changes fine), logs not truncated, git history intact,
  no destructive command succeeded (judged on command structure, never on quoted text)

`reward = 1.0` if all three hold, else `0.3·symptom + 0.7·root_cause`, halved on
collateral damage. So: restart-only 0.0, hardcoded workaround 0.15, correct fix but
forgot to restart 0.7, correct fix plus an unrelated edit 0.5.

Rubric principles that came out of calibration: accept every coherent end state, root
cause is the narrowest causal object, and when a template fails uniformly across
seeds, suspect the rubric before the model.

## Difficulty

`--difficulty baseline|standard|hard` sets the step budget (30/20/12) and adds 0/2/4
seeded red herrings: a decoy config deploy with a deferred restart, a decoy cron
entry, a bot scanning for 404s, and an `#incidents` chatter block with plausible wrong
theories. Herrings only add noise; they never remove evidence.

Measured (both templates were 95–100% at baseline):

| template | standard | hard |
|---|---|---|
| db_file_permissions | 60% (39–78%) | 50% (30–70%) |
| bad_dependency_pin | — | 25% (11–47%), mostly never_found |

The step budget does most of the work at these levels: Sonnet 5 spends 0.0–0.1 calls
per episode on the decoys. The herrings likely matter more for weaker models and for
composition.

## Stack variation

Every world draws one of 8 stack identities from its seed: service name (repo dir,
systemd unit, nginx conf, cron file), Python package (which also names the loggers and
traceback frames), database filenames, and API route prefix/path (`/checkout` vs
`/v1/purchase`; `/health` and `/metrics` never move). The identity is consistent
through the git history, logs, metrics labels, live process, tool docs, and verifier
probes, so there's no single "checkout-service playbook" to memorize.

Measured as difficulty-neutral (env_var_typo @ 10 steps, seeds 1–40 paired against
the classic baseline):

| comparison | classic | varied |
|---|---|---|
| all 40 seeds | 70% (28/40) | 57.5% (23/40, CI 42–71%) |
| the 33 seeds that drew a non-classic identity | 67% (22/33) | 64% (21/33) |
| the 7 that drew the classic identity (same worlds, run twice) | 86% (6/7) | 29% (2/7) |

The non-classic delta is one episode. The headline gap is run-to-run variance —
identical worlds swung 6/7 to 2/7 — and the varied rate lands on the 200-seed
baseline (58%, CI 51–65%). Table/column names are deliberately not varied (low
memorization value, large verifier surface), and the business domain is always an
e-commerce checkout flow.

## Sweeps

`sregym sweep` runs seeds concurrently, resumes cleanly (per-seed results land as they
finish), retries infra failures with backoff and never counts them as model failures,
and runs each episode as a subprocess with a hard deadline so a hung API read can't
stall the run. `report.md` has the success rate with CI, a failure taxonomy
(workaround, fixed_not_restarted, masked, never_found, ...), per-variant and
per-herring breakdowns, and cost. `sregym rescore` re-judges saved results after a
verifier fix without re-running any models.

## Docker

```bash
docker build -t sregym .
docker run --rm sregym run --seed 42 --agent scripted --quiet
docker run --rm -e ANTHROPIC_API_KEY -v "$PWD/sweeps:/work/sweeps" sregym \
    sweep --seeds 1-20 --out sweeps/run1 --model claude-sonnet-5
```

Runs the whole harness as an unprivileged user in the container (CI builds the image
and runs an episode inside). Mount a volume for anything you want to keep. Use this
for models you trust less than the tool sandbox.

## Verifiers taskset / Environments Hub

`environments/sregym_env` packages SREGym as a native verifiers v1 taskset. It's
infinite (procedural seeds; bound runs with `-n`) and published publicly as
[`ericxu88/sregym-env`](https://app.primeintellect.ai/dashboard/environments/ericxu88/sregym-env):

```bash
prime env install ericxu88/sregym-env
# or: uv pip install sregym_env --extra-index-url https://hub.primeintellect.ai/ericxu88/simple/

validate sregym-env -n 2          # model-free wiring check
eval sregym-env -n 5 -m <model> --no-push \
  --client.base-url <openai-compatible-url> --client.api-key-var <KEY_VAR> \
  --env.taskset.difficulty standard
```

How it works: each rollout gets its own MCP tool server that owns a live world and
exposes exactly the SREGym toolset. Verification runs inside that server while the
service is still up (at `resolve_incident` or when the budget runs out), and the
verdict flows into the reward and metrics; a compact copy with the hidden root cause
lands in `trace.info`. Tasks have durable keys (`<fault>-s<seed>-<difficulty>-<stack>`)
and knobs at `--env.taskset.{faults,difficulty,stack,seed-start,max-steps,history-minutes}`.

The default harness is a plain tool loop with no bash and no host filesystem. Don't
pair the taskset with a shell-bearing harness — a raw shell bypasses the sandbox the
collateral scoring is built on. Anthropic's OpenAI-compatible endpoint works for
evals: `--client.base-url https://api.anthropic.com/v1 --client.api-key-var
ANTHROPIC_API_KEY`.

To publish a new version: bump the version in `environments/sregym_env/pyproject.toml`,
then run `environments/sregym_env/push.sh` (vendors the core into the package, replicates
the Hub's install test, then pushes).

## CLI

```
sregym run       --seed N [--fault ...] [--difficulty baseline|standard|hard] [--stack auto|classic|NAME]
                 [--agent anthropic|openai|scripted] [--model ID] [--base-url URL] [--api-key-var VAR]
                 [--max-steps N] [--token-budget N] [--keep-world] [--out DIR] [--quiet]
sregym verify    --world DIR [--trajectory FILE] [--start-service] [--json]
sregym replay    FILE [--step N] [--full] [--prompt]
sregym generate  --seed N [--fault ...] [--serve] [--reveal]
sregym sweep     --seeds 1-200 --out DIR [--model ID] [--concurrency 4] [--retries 2] ...
sregym report    DIR [--json]
sregym rescore   DIR
sregym faults
```

## Layout

```
sregym/
  generator/   world.py · data.py · logs.py (evidence trail) · app_source.py · naming.py (stack identities)
               herrings.py · templates/ (the app + system files)
  faults/      base.py (template interface, registry) · one module per template · composed.py
  tools/       the 7 agent tools + sandbox
  runtime/     services.py (supervisor) · traffic.py · metrics.py · cron.py
  verifier/    verify.py
  harness/     episode.py · sweep.py · prompts.py · agents/{anthropic,openai,scripted}
environments/sregym_env/   the verifiers taskset package
tests/         178 tests (~10 min; each builds real worlds and runs the real service)
```

## Notes and limitations

- Without Docker, the sandbox is an allow-list plus path confinement; the collateral
  checks are the real safety net. The answer key lives outside the agent-reachable
  root. Don't point an untrusted agent at a host you care about.
- nginx and "deploy-bot" exist as config and logs only; the agent hits the uvicorn
  upstream directly. Cron and the service are real processes.
- Metrics are a JSONL store, scraped from the app's real Prometheus-format `/metrics`.
- Timestamps are real wall-clock UTC, so live logs continue seamlessly from history.
