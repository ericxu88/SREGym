# SREGym

**A procedurally-generated incident-response RL environment for LLM agents.**

Every episode generates a small but complete production stack on local disk (FastAPI
service + SQLite databases + git repo with a plausible history + nginx/systemd/cron
config + hours of realistic logs and metrics), injects a *known* fault from a template
library, pages the agent like a real on-call engineer would be paged, gives it on-call
tools, and then **deterministically verifies** — no LLM judge — whether the agent fixed
the true root cause without doing collateral damage.

```
$ sregym run --seed 42 --agent anthropic --model claude-opus-5
```

The MVP ships one fault template (`env_var_typo`: a config deploy typo'd a database
env var), one model adapter (Anthropic Messages API, tool use) plus a deterministic
reference solver, a paginated investigation toolset, a 3-part verifier, JSONL
trajectories, and a CLI to run / verify / replay episodes.

---

## Quick start

Requirements: Python ≥ 3.10, `git`, `sqlite3` (both usually preinstalled). No Docker.

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
source .venv/bin/activate

# offline demo: deterministic reference solver, no API key needed (~4 s)
sregym run --seed 42 --agent scripted

# a real model: put ANTHROPIC_API_KEY=... in a git-ignored ./.env (auto-loaded), export it, or use an `ant auth login` profile
sregym run --seed 42 --agent anthropic --model claude-opus-5 --max-steps 30

# replay any trajectory offline
sregym replay runs/<timestamp>-seed42-anthropic/trajectory.jsonl

# calibration sweep: many seeds, concurrent, resumable; writes report.md with success rate, failure taxonomy, cost
sregym sweep --seeds 1-200 --agent anthropic --model claude-sonnet-5 --concurrency 4 --out sweeps/sonnet5-v1
sregym report sweeps/sonnet5-v1

# generate a world and keep it running for manual poking (Ctrl-C to stop)
sregym generate --seed 7 --serve --reveal

pytest -q       # 65 tests, ~1.5 min (each test builds a real world and runs the real service)
```

### Demo run (scripted reference solver, seed 42)

```
$ sregym run --seed 42 --agent scripted
[sregym] world at /tmp/sregym-42-vb6dyrff (port 53264); fault=env_var_typo seed=42
[sregym] hidden root cause: Deploy b25be1a (ops: bump payment gateway timeout to 2500ms (PAY-231)) changed
         checkout-service/.env: key DATABASE_URL was mistyped as DATABSAE_URL. Fix: restore DATABASE_URL=... and restart.
[agent] Paged for 5xx on checkout-service. Starting with the log inventory and error rate by path.
[step 1] read_logs() -> 'Available log files (path, lines, size, time span):\ncheckout-service/logs/app.log   33654 lines ...'
[step 2] query_metrics(metric='http_error_rate', window_minutes=60, group_by='path', step_minutes=5) -> 'metric=http_error_rate ...'
[step 3] read_logs(path='checkout-service/logs/app.log', grep='checkout\\.access.* 5\\d\\d ', tail=True, limit=15) -> '... 1688 matching lines ...'
[agent] 500s carry 'unable to open database file'. Checking for restarts/config warnings around the onset.
[step 4] read_logs(path='checkout-service/logs/app.log', grep='checkout\\.serve|checkout\\.config|Shutting down', tail=True) -> ...
[step 5] read_logs(path='checkout-service/logs/deploy.log', tail=True, limit=8) -> '... deploy b25be1a requested by David Guzman ...'
[step 6] run_shell(command='curl -s http://127.0.0.1:53264/health') -> '{"status":"degraded",...,"checks":{"core_db":"error: unable to open database file","ledger_db":"ok"}}'
[agent] A config-only deploy restarted the service right when errors began. Reviewing that commit.
[step 7] run_shell(command='git -C checkout-service show HEAD') -> '... -DATABASE_URL=sqlite:///data/checkout.db\n+DATABSAE_URL=sqlite:///data/checkout.db ...'
[step 8] read_file(path='checkout-service/.env') -> ...
[step 9] run_shell(command='ls -la checkout-service/data') -> 'checkout.db  ledger.db'
[agent] The deploy changed the line 'DATABSAE_URL=...'; restoring 'DATABASE_URL=...' in .env.
[step 10] edit_file(path='checkout-service/.env', old_string='DATABSAE_URL=sqlite:///data/checkout.db', new_string='DATABASE_URL=sqlite:///data/checkout.db') -> 'edited ... (1 replacement)'
[step 11] restart_service() -> 'checkout-service stopped (pid 19134)\ncheckout-service started (pid 19168) ... Health: GET /health -> 200'
[step 12] run_shell(command='curl -s http://127.0.0.1:53264/health') -> '{"status":"ok", ...}'
[step 13] run_shell(command='sqlite3 checkout-service/data/checkout.db "select sku from products ..."') -> 'INB-9595'
[step 14] run_shell(command="curl -s -X POST http://127.0.0.1:53264/checkout ...") -> '{"order_id":6123,"payment_id":6066,"status":"confirmed",...}'
[step 15] read_logs(path='checkout-service/logs/app.log', grep='checkout\\.access.* (2|5)\\d\\d ', tail=True, limit=10) -> ...
[step 16] resolve_incident(summary="Deploy of HEAD changed checkout-service/.env: ...", root_cause='config typo in the database env var introduced by the last deploy')
symptom_resolved=PASS  root_cause_fixed=PASS  no_collateral_damage=PASS  reward=1.00  success=True
  [x] symptom    health_ok                  GET /health -> 200
  [x] symptom    checkout_ok                POST /checkout -> 201
  [x] symptom    orders_ok                  GET /orders?user_id=249&limit=5 -> 200
  [x] symptom    users_ok                   GET /users/249 -> 200
  [x] root_cause env_value_correct          DATABASE_URL=sqlite:///data/checkout.db
  [x] root_cause app_code_unchanged         5 files unchanged
  [x] root_cause db_file_in_place           checkout-service/data/checkout.db present
  [x] collateral unrelated_files_unchanged  16 tracked files intact (allowed to change: checkout-service/.env)
  [x] collateral db_rows_intact             row counts ok: carts=34, order_items=10183, orders=6124, ...
  [x] collateral logs_preserved             5 log files intact
  [x] collateral git_history_preserved      9 commits present; HEAD=b25be1a
  [x] collateral no_forbidden_actions       no forbidden actions in 16 steps

seed=42 fault=env_var_typo agent={'agent': 'scripted', 'mode': 'solve'} steps=16 stop=resolved duration=3.93s
reward=1.00 success=True  symptom=True root_cause=True collateral_ok=True
trajectory: runs/20260819-011059-seed42-scripted/trajectory.jsonl
```

The scripted solver is a solvability oracle, not a benchmark: `ScriptedAgent("mask")`,
`"workaround"`, `"noop"` and `"sloppy"` exercise the failure modes the verifier must
distinguish (see [Reward](#verifier--reward)). Across seeds 1–20 the solver scores 1.0
on every world; each episode takes ~4 s end to end.

---

## Baselines (measured)

All numbers are from `sregym sweep`, deterministic verifier, no LLM judge; seeds are shared across rows
(1–20 for pilots, 1–200 for the baseline). Cost is the Anthropic list price with prompt caching.

**`env_var_typo` — reference configuration: lean prompt, `max_steps=10`** (calibrated into the 30–60% band)

| model | seeds | success | 95% CI | mean reward | outcome taxonomy | steps | $/episode |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | 1–200 | **58.0%** (116/200) | 51–65% | 0.745 | success 116 · fixed_not_restarted 47 · never_found 37 | 10 (budget) | $0.087 |

Per variant (same run): `DATABASE_URL` value-typo 74%, key-name typo 45%, `LEDGER_DATABASE_URL` value 47% / key 45% —
the silent-fallback key-name variant is the harder one. Full report: `sweeps/baseline-sonnet5-lean-steps10/report.md` (local).

**`ledger_divergence` — reference configuration: lean prompt, `max_steps=22`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 17/20 = 85% | 64–95% | 0.91 | remediation_incomplete 1 · never_found 1 · collateral 1 (stray helper script*) | $0.26 |
| claude-sonnet-5 | **lean, 22 steps** | **13/20 = 65%** | 43–82% | 0.825 | remediation_incomplete 4 · fixed_not_restarted 1 · never_found 2 | $0.19 |

**`unapplied_migration` — reference configuration: lean prompt, `max_steps=12`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 20/20 = 100% | 84–100% | 1.00 | — (0 workarounds; forgotten migrations were authored, not patched around) | $0.24 |
| claude-sonnet-5 | **lean, 12 steps** | **11/20 = 55%** | 34–74% | 0.56 | never_found 8 · workaround 1 (rolled the release back with `git revert`) | $0.11 |

At 12 steps the budget separates by *fix difficulty* rather than pure truncation: shipped-migration variants
67–100%, forgotten-migration 0–33% (authoring the migration costs 5–8 extra calls; the fix landed at median call 11,
range 8–19). Rolling back the release restores service but is scored as a workaround — the root cause is the
schema, and the verifier checks that the application code is unchanged.

**`cron_write_lock` — reference configuration: lean prompt, `max_steps=30`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 19/20 = 95% | 76–99% | 0.958 | workaround 1 (rescheduled the job to *hourly* and edited the script — the rubric wants at most daily) | $0.33 |

The heaviest template so far (~28 steps, ~3.5 min/episode — models spend calls correlating the burst timing
before looking at cron, and verification itself takes a 65 s probe window). Calibrating it surfaced a scoring
principle now encoded in the template: root cause is the **schedule alone** — disabling the entry while *also*
editing the deployed script or app code scores 0.5 (collateral: right diagnosis, change-control violation),
not 0.15 (workaround). Script-only edits still fail: the host refuses to run modified scripts.

**`db_file_permissions` — reference configuration: lean prompt, `max_steps=30`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 19/20 = 95% | 76–99% | 0.95 | never_found 1 (the `data/` 0555 variant — hardest at 80%; both file variants 100%) | $0.28 |

**`bad_dependency_pin` — reference configuration: lean prompt, `max_steps=30`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 20/20 = 100% | 84–100% | 1.00 | — (every episode chose fix-forward; ~21 steps, ~$0.15) | $0.15 |

**`rate_limit_misconfig` — reference configuration: lean prompt, `max_steps=30`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 20/20 = 100% | 84–100% | 1.00 | — (~20 steps; clean diagnose → fix pin value → restart → burst-verify) | $0.18 |

**`disk_full` — reference configuration: lean prompt, `max_steps=30`, `--stack auto`** (mini-calibration, 20 seeds)

| model | config | success | 95% CI | mean reward | outcome taxonomy | $/episode |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | lean, 30 steps | 19/20 = 95% | 76–99% | 0.985 | 1 fixed_not_restarted (removed the quota on the last step) | $0.25 |

The first measurement of this template returned **0/20, all "workaround"** — every episode migrated the two
`kv()` call sites to the new API instead of rolling the pin back. That unanimity was the tell: the model was
right and the rubric was opinionated. The verifier now accepts either coherent end state (see the template
description above); the re-measure under the corrected rubric is the 100% row. Difficulty here will come from
red herrings and step budgets, not from disqualifying legitimate engineering.

The causal-depth (deferred-restart) variant did not reduce success (89% vs 82% at 30 steps): the model reads
`git log -p -- .env` rather than trusting `git show HEAD`. Episodes are ~2× longer than `env_var_typo` because a
complete fix also requires the data backfill; at 22 steps the typical failure is "config fixed and restarted,
backfill not reached". (*The stray-script case led to letting agents `rm` files they created themselves.)

Calibration ladder (claude-sonnet-5, seeds 1–20):

| configuration | success | 95% CI | failure mode |
|---|---|---|---|
| full prompt, 30 steps | 20/20 = 100% | 84–100% | — (claude-opus-5: also 20/20, 20 steps avg, $0.26/episode) |
| lean prompt, 30 steps | 20/20 = 100% | 84–100% | — (spelled-out norms made no difference; every run still verified after restart) |
| full prompt, 12 steps | 19/20 = 95% | 76–99% | 1 fixed_not_restarted |
| full prompt, 10 steps | 13/20 = 65% | 43–82% | 6 fixed_not_restarted, 1 never_found |
| full prompt, 8 steps | 3/20 = 15% | 5–36% | 15 never_found, 2 fixed_not_restarted |

What this says: for frontier models this template saturates at a generous budget, prompt verbosity is not a
lever, and the step budget moves the number by truncation (fixed-but-not-restarted / out of budget) rather than
by investigation failures. Harder content (subtler faults, red herrings, stack variation) is where real headroom
comes from — that is the rung-3 work. Two verifier false positives were found and fixed during calibration
(commit-message text matched a redirect regex; `checkout-service` path matched a git-checkout heuristic);
`sregym rescore` re-judged the affected saved results.

## What the agent sees

The page (task prompt) is symptom-level, timestamped and vague — it never names the cause:

```
[PagerDuty] INCIDENT #4710 — TRIGGERED — P1
Service:      checkout-service (production)   Escalation policy: payments-oncall → you
Title:        [P1] checkout-service: HTTP 5xx error rate > 10% (current 95.2%)
Triggered at: 2026-08-19 00:29:25 UTC   (condition held for 5m before paging)
Alert rule:   sum(rate(http_requests_total{service="checkout-service",status=~"5.."}[5m])) / sum(rate(...)) > 0.10
Details:      Alert has been firing since 00:29 UTC; symptom start ~00:23 UTC. No auto-remediation configured.
Support note (00:33 UTC, Zendesk #77152): "Getting a spike of tickets: checkout spinner then an error toast. ..."
Runbook:      (none linked)
Acknowledged: you, 00:33 UTC

Current time is 2026-08-19T00:46:16Z (all timestamps UTC). Investigate, mitigate, and fix the root cause.
```

The system prompt describes the host layout, the tools, and what a good resolution
looks like (restore service, fix the root cause not a workaround, no collateral damage,
verify, then call `resolve_incident`). Both prompts are stored in the trajectory.

### Tools

| tool | what it does |
|---|---|
| `read_logs` | **Paginated** log reader: max 50 lines per call, opaque cursors, `grep` (regex), `since`/`until`, `tail`. Lists log files when called without a path. |
| `query_metrics` | Per-minute time series from the metrics store: `http_requests_total{method,path,status}`, latency sum/count, `db_errors_total{db}`, `up`, derived `http_error_rate` and `..._avg`; `group_by`, `filters`, windows. |
| `read_file` | Read text files with line numbers (config, source, nginx/systemd/cron files). |
| `edit_file` | Exact-string replacement (or create); returns a unified diff. Log files are read-only. |
| `run_shell` | Sandboxed shell: allow-listed read-mostly commands, `git` (log/show/diff/blame/checkout/revert/commit…, no reset/clean/push), `sqlite3` (forced read-only), `curl` to 127.0.0.1 only, `python` only for unmodified repo ops scripts; pipes and `;`/`&&`/`||` sequences (each command validated), no redirection/substitution; paths confined to the host root. |
| `restart_service` | `systemctl`-like control of `checkout-service` (restart/status/start/stop) — restart re-reads `.env`. |
| `resolve_incident` | Terminal: agent submits a postmortem summary and ends the episode. |

Tool results are text, capped per tool (a full 50-line log page always fits).

---

## What gets generated

```
<world>/                              # temp dir, deleted after the episode unless --keep-world
  .sregym/                            # control plane: world.json, spec.json (incl. the answer), manifest.json.
                                      # A sibling of host/, so tools confined to host/ cannot reach it — not even
                                      # with `grep -r`, globs, `..` or absolute paths (tested).
  host/                               # the "host filesystem" the agent operates on; all tool paths are confined here
    checkout-service/                 # git repo (9 commits over ~90 days), the service's working directory
                                      # (names below show the classic identity; every world draws one of 8 -- see Stack variation)
      .env                            # production config, tracked in git, "shipped by deploy-bot"
      checkout/{config,db,main,serve,telemetry}.py   # FastAPI app: /health /users /orders POST /checkout /metrics
      migrations/*.sql  scripts/expire_carts.py  README.md  requirements.txt
      data/checkout.db  data/ledger.db   # SQLite (users/products/orders/carts; payments ledger)
      logs/app.log                    # 3h of access + application log, UTC, incl. tracebacks; the live app appends to it
      logs/deploy.log  logs/cron.log
    etc/nginx/sites-enabled/checkout-service.conf, etc/systemd/system/*.service, etc/cron.d/checkout-service
    var/log/nginx/access.log, error.log
    metrics/series.jsonl              # metrics store (historical + live scrapes)
```

* **World** (`sregym/generator/world.py`): Faker-seeded business data (company, ~300–700
  users, ~30–45 SKUs, thousands of orders/payments) → SQLite; app source rendered from
  templates with *feature sections* so the git history is believable (each feature
  commit adds its section: checkout → ledger → metrics → cron job, plus config-only
  commits); nginx/systemd/cron files pointing at the real port and interpreter.
* **History** (`sregym/generator/logs.py`): one simulated request stream drives the app
  log, nginx logs, per-minute metrics *and* the database (successful checkouts in the log
  window are inserted as real orders), so the artifacts agree with each other. It
  contains noise and red herrings (404s/400s/422s, a slow-query burst an hour before,
  LB health probes, Prometheus scrapes, cron output, old deploys) and, after injection,
  the incident: the deploy restart banner, then failing endpoints with tracebacks whose
  line numbers match the actual source. The live app logs in the exact same format.
* **Live runtime** (`sregym/runtime/`): the service runs as a real uvicorn subprocess
  with a clean environment (so `.env` is the only source of config and a restart is
  required to pick up changes); a synthetic traffic thread (~1.5 rps) keeps errors and
  logs flowing during the episode; a metrics collector scrapes `/metrics` every 10 s and
  appends deltas to the store, so `query_metrics` shows recovery after a fix.

### Fault template interface

```python
class FaultTemplate:
    name: str
    def inject(self, world: World, seed: int) -> VerificationSpec: ...
```

`VerificationSpec` is declarative: three lists of typed `Check`s (`symptom_checks`,
`root_cause_checks`, `collateral_checks` — the last includes forbidden-action patterns
matched against the trajectory), an `IncidentProfile` (timeline + symptom facts used to
render the logs and the page, plus a *hidden* root-cause summary for analysis), and
`allowed_changed_files`. The verifier only interprets check types; templates never
touch the verifier.

**`env_var_typo`** (seed-parameterized): which variable breaks (`DATABASE_URL` → all DB
endpoints 500; `LEDGER_DATABASE_URL` → only `POST /checkout` 500), how (value typo like
`checkuot.db` / `dat/`, or a *key-name* typo like `DATABSE_URL` after which the app
silently falls back to a non-existent dev default and logs one WARNING at startup),
which innocent change shares the same commit (payment timeout, cart TTL, sqlite busy
timeout, or a comment tidy-up), and the timeline (commit → deploy-bot → restart → first
errors → page 5 min later → support note). Fix = restore the value in `.env` (or `git
revert`/`checkout` it) **and** restart.

**`ledger_divergence`** (rung 3, template #1): a config change pointed `LEDGER_DATABASE_URL` at the
weekly audit snapshot (`data/ledger-snapshot-YYYYMMDD.db`). Nothing errors — `/checkout` keeps returning
201 and payments silently land in the stale file; the finance-side ledger exporter
(`ledger_last_payment_age_seconds`, `ledger_payments_total`, read from the canonical file) is what pages.
Seeded: snapshot age (and whether an older one also exists), which commit made the change, and a
**causal-depth** variant where the config shipped hours earlier with the restart *deferred* and an innocent
release commit later restarted the service (so `git show HEAD` misleads). A complete fix = restore the URL,
restart, **and** backfill the diverted payments with the repo's `scripts/reconcile_ledger.py --source … --apply`
(config + restart alone scores 0.7: bleeding stopped, ledger still incomplete). New verifier check types:
`http … then_sql` (the probe checkout's payment must appear in the real ledger) and `ledger_complete`
(every confirmed order since the incident has a ledger payment). The shell can run **generation-time,
hash-pinned repo scripts** (`python checkout-service/scripts/<name>.py …`, executed from the repo root);
any other use of `python`, or a script the agent has modified, is refused.

**`unapplied_migration`** (rung 3, template #2): the latest release ships a feature whose SQL needs a new
column (seeded: coupon codes on `orders` → `POST /checkout` + `GET /orders/{id}` fail; fulfillment status →
both orders GETs; marketing opt-in on `users` → both users GETs). deploy-bot never runs migrations (it says so
on every code deploy); the manual step was skipped — and in 30% of seeds the migration file was never even
committed. `/health` stays 200; the failing endpoints log `sqlite3.OperationalError: no such column: …`.
Fix: apply the migration with the repo's `scripts/migrate.py --apply` (no restart needed — connections are per
request); in the forgotten variant, write `migrations/003_<name>.sql` first (allowed by a glob in the
manifest check). Root cause = the schema has the columns (+ the shipped migration recorded) **and** app code
unchanged — patching or reverting the code is a workaround. Verifier generalizations that came with it: row
hashes over generation-time columns, an additive-only schema rule (new tables/columns/indexes fine; dropping or
retyping anything is damage), glob allow-lists, and a `db_query` check.

**`cron_write_lock`** (rung 3, template #3): a "temporary" orders-archive backfill was dropped into
`etc/cron.d/checkout-service` at `* * * * *`; each run opens `BEGIN IMMEDIATE`, does a slow verification
scan, and holds the core DB's write lock ~20–40 s — so `POST /checkout` fails with `database is locked`
after the 5 s busy timeout, **in bursts aligned to the minute**. Reads and `/health` are fine, and there is
**no deploy to blame**: the evidence is the periodicity, `archive_orders:` lines in cron.log (with held-time),
a crond RELOAD line, and the cron file's fresh mtime. The live world runs a real cron daemon (deployed,
hash-pinned repo scripts only — a script the agent edits is skipped, and cron.log says so), so the incident
keeps reproducing during the episode. Fix = remove/comment the entry (once-a-day off-hours also accepted);
editing the job script or the app is a workaround. Verification uses a **probe window**: after waiting out any
in-flight lock, `POST /checkout` must succeed every 5 s for 65 s with no new `database is locked` lines — a
restart-only "fix" gets caught by the next burst.

**`db_file_permissions`** (rung 3, template #4): the host's config-management agent ("fleetd") applied a
permissions baseline written for other hosts and stripped the write bit from the service's data path
(seeded target: `data/` 0555, `data/checkout.db` 0444, or `data/ledger.db` 0444). SQLite silently falls back
to read-only, so reads and `/health` stay green — but every write fails instantly with
`sqlite3.OperationalError: attempt to write a readonly database`, and only `POST /checkout` 500s. No deploy,
no restart, no git change; the trail is `var/log/fleetd.log` (rule name, old → new mode — every world carries
routine fleetd policy-sync entries so the log's existence is not a tell) and `ls -la`. Fix: `chmod` the write
bit back (the sandbox allows chmod, confined to the host root; no restart needed). Repointing `.env` at a
writable path or patching the app is a workaround. Reproduces only when the harness runs unprivileged (root
ignores file modes; CI runners are non-root).

**`bad_dependency_pin`** (rung 3, template #6): a release bumped the internal `reqlog` package pin to 3.0.0,
whose API removed `kv()`; deploy-bot installed it from the local wheelhouse into the (gitignored) `lib/` and
restarted — the service dies at import, crash-loops to its start limit, and stays **down**. The first
dead-service template: 100% 502s at nginx, `up == 0`, connection-refused floods in the edge log, five real
captured crash tracebacks in app.log (generated by actually running the broken service once at build), and a
`deploy FAILED … start-limit-hit` trail in deploy.log. `restart_service` genuinely crash-loops (the supervisor
retries 5× then gives up, like systemd). Fix: restore the pin (or `git revert`), reinstall with
`scripts/deploy_deps.py` (copies the pinned version from `vendor/wheels/` into `lib/`), restart. Root cause
accepts **either coherent end state** (a lesson the first calibration taught: all 20 Sonnet 5 episodes chose to
*fix forward* — migrate the two `kv()` call sites to the new API — which is a legitimate remediation for an
intentional bump, not a workaround): rollback (pin 2.1.0 + `lib/` byte-identical to the pristine wheel + app
untouched) or fix-forward (pin 3.0.0 + pristine 3.0.0 install + `main.py` still uses the library + the
structured access-log behavior verified live by a probe-then-grep check). Incoherent mixtures — hand-patched
`lib/`, edited wheelhouse, dropped log fields — match neither state and fail (new checks: `any_of`,
`file_matches`, `dirs_equal`, `http_then_log`).

**`rate_limit_misconfig`** (rung 3, template #7): a config deploy set `RATE_LIMIT_PER_MINUTE` to 1–3 —
in the flagship variant the commit message says "clamp to 100/min" while the diff sets **1** (dropped
zeroes). The first pure-4xx policy incident: zero 5xx, `/health` green, latency normal — but checkout
traffic contains legitimate bursts (double-clicks, client retries, split carts; all worlds simulate them,
live and historical), and every attempt past the limit 429s with a `checkout.ratelimit` WARNING and a
`rate_limited_requests_total` tick. The page fires on the counter, not an error rate. Fix: restore a sane
per-user value (≥ 60 accepted — the intended 100 and the old 600 both pass) and restart; patching the
limiter code is a workaround. Symptom verification is a **burst probe** (`http_burst`): 6 rapid checkouts
by one user must all return 201.

**`disk_full`** (rung 3, template #8): a capacity-guardrail deploy set `DATABASE_MAX_PAGES`
(the app's optional `PRAGMA max_page_count` cap on the core database, documented in the repo README)
*below the database's current size* — SQLite clamps the quota to the current size and every write that
needs a new page fails with the genuine `sqlite3.OperationalError: database or disk is full`. The error
actively misleads: `df` shows plenty of space, permissions are fine, reads and `/health` stay green; the
trail is the config-only deploy. Fix: remove the quota (or raise it ≥ 10,000 pages, ~13× the db) and
restart; patching the pragma out of `db.py` or pruning rows are workarounds (`files_unchanged` /
`db_rows_intact` catch both). Mechanism note: literal ENOSPC cannot be simulated honestly without
privileged mounts, and faking the error string without the underlying condition would be reward-hackable —
the quota produces the real error from the real engine, live. `finalize()` VACUUMs the core db so packed
pages make the very first write fail deterministically.

**Fault composition** (`--fault composed` or `composed:<pair>`): two independent faults in one world — a
deploy-borne one and an environmental one — with one page (the first alert that fired, plus the second
stacked onto it as "ALSO TRIGGERED"). Vetted pairs: `migration+perms` (with a real causal ordering — the
migration cannot be applied until the write bit is restored), `ratelimit+perms` (one endpoint interleaves
429 bursts and readonly 500s), `migration+cron` (steady 500s on one endpoint family, minute-aligned lock
bursts on another). Each member keeps its own incident clock (per-endpoint failure start times); profiles,
evidence trails and verification specs merge, with member-prefixed check names so reports show partial
progress. Merge rule learned in testing: one member's "file unchanged" workaround-detector is shrunk by the
sibling's allowed-changed files — otherwise fixing fault A correctly would veto fault B's required fix.
Reward stays all-or-nothing: both root causes coherently fixed, all probes green, no collateral.

Measured (claude-sonnet-5, lean, `--difficulty standard`, seeds 1–20, seed-picked pairs):
**6/20 = 30%** (95% CI 15–52%), all failures `never_found` at the 20-step budget — of which 8/14 had
fully fixed exactly one of the two faults (4× cron, 4× perms) before running out, and 6/14 neither.
Per-pair: ratelimit+perms 5/8, migration+perms 1/4, migration+cron 0/8. This is the first configuration
where difficulty comes from **scope** (two remediations + verification in one budget) rather than pure
time pressure on a known playbook — and where partial progress is measurable via member-prefixed checks.

### Verifier & reward

Deterministic, no LLM (`sregym/verifier/verify.py`), run against the *live* world at the
end of the episode:

| criterion | checks |
|---|---|
| `symptom_resolved` | `GET /health` 200; `POST /checkout` (synthetic order) 201; `/orders`, `/users/{id}` 200 |
| `root_cause_fixed` | the env var resolves to the right database path (accepts `./`, absolute forms); app code (`config.py`, `db.py`, …) unchanged — hardcoding is **not** a fix; DB file still at its original path (not moved to match the typo) |
| `no_collateral_damage` | every non-log file hash equals the generation-time manifest (only `.env` may change); all original DB rows/schema intact (new rows allowed); logs not deleted/truncated; original commits still exist; no destructive command succeeded in the trajectory (judged on command *structure* — program, git subcommand/flags, sqlite keywords, redirect operators — never on quoted text such as commit messages) |

`reward = 1.0` if all three hold; otherwise `0.3·symptom + 0.7·root_cause`, halved when
there is collateral damage. So: restart-only (still broken) 0.0; hardcode-in-code
workaround 0.15; correct `.env` fix but forgot to restart 0.7; correct fix + touched
an unrelated file 0.5; did nothing 0.0.

`sregym verify --world <dir> [--trajectory t.jsonl] [--start-service]` re-runs the
verifier on a kept world.

### Trajectory (JSONL)

```
{"type":"meta", "seed":42, "fault":"env_var_typo", "agent":{...}, "system_prompt":..., "task_prompt":..., "incident":{...}}
{"type":"step", "step":1, "observation":"<what the agent saw before this call>", "assistant_text":"...",
 "assistant_thinking":"<summary if available>", "tool_call":"read_logs", "tool_args":{...},
 "tool_result":"...", "tool_error":false, "state_hash":"sha256:...", "usage":{"input_tokens":..,"output_tokens":..}, "ts":"..."}
...
{"type":"end", "stop_reason":"resolved|max_steps|token_budget|agent_stopped|agent_error", "reward":1.0,
 "verification":{...per-criterion booleans + every check...}, "usage":{...}, "agent_summary":"...", "hidden_root_cause":"..."}
```

`state_hash` covers the agent-controllable state (tracked files, DB contents, service
identity) so read-only steps hash identically and edits/restarts are visible.
`sregym replay <file> [--step N] [--full] [--prompt]` renders it offline.

### Agent loop

`sregym/harness/episode.py` owns the loop (`scenario.prepare_world → LiveWorld → agent.next_turn()
→ execute tool calls → agent.observe() → … → verify`), enforcing `--max-steps` (one step
per tool call; a turn may contain several) and `--token-budget` (sum of all tokens
processed across turns). Adapters implement three methods
(`sregym/harness/agents/base.py`):

```python
class AgentAdapter:
    def start(self, system_prompt, task_prompt, tool_specs): ...
    def next_turn(self) -> AgentTurn:  # text, tool_calls, usage, stop, thinking
    def observe(self, results: list[tuple[ToolCall, ToolResult]]): ...
```

Tool specs are Anthropic-style `{name, description, input_schema}`; an OpenAI/local
adapter wraps them as `{"type":"function","function":{...,"parameters": input_schema}}`
and registers itself in `sregym/harness/agents/__init__.py`. The Anthropic adapter
(`anthropic_adapter.py`) uses the Messages API with tool use, adaptive thinking (echoed
back unchanged between turns), automatic prompt caching over the growing transcript,
`refusal` handling, and server-side refusal fallbacks for Fable 5 / Opus 5 (auto-disabled
if the account rejects the beta). `--model claude-opus-5` is the default; `--effort`,
`--thinking off`, `--max-tokens` are available.

---

## Difficulty profiles & red herrings

`--difficulty baseline|standard|hard` (on `run` and `sweep`) bundles the realism-preserving knobs:
the default step budget (30 / 20 / 12; explicit `--max-steps` still wins) and how many **red herrings**
the world gets (0 / 2 / 4). Herrings are seeded, template-agnostic, and strictly additive — they add
plausible noise, never remove real evidence:

- **decoy config deploy** — an innocent recent `.env` commit + deploy with the restart *deferred* (it cannot
  have caused anything); for no-deploy faults it sits at HEAD, exactly where a "blame the last commit"
  heuristic looks
- **decoy cron entry** — a harmless, recently-added maintenance line (fresh file mtime)
- **bot scan** — a scraper bursting hundreds of 404s from one IP through app and edge logs near the incident
- **incident-channel chatter** — a `#incidents` excerpt on the page with plausible wrong hypotheses
  (the promo email, the decoy deploy, credential rotation, the CDN)

`report.md` breaks results down by herring combination. Profiles are the intended calibration instrument:
budgets and noise live here, not in per-template tweaks.

Measured effect (claude-sonnet-5, lean prompt, seeds 1–20; both templates were 95–100% at baseline):

| template | standard (20 steps, 2 herrings) | hard (12 steps, 4 herrings) |
|---|---|---|
| db_file_permissions | **60%** (39–78%) | **50%** (30–70%) |
| bad_dependency_pin | — | **25%** (11–47%), 60% never_found |

Honest reading: at these budgets the step budget does most of the work — trajectory analysis shows Sonnet 5
essentially never spends calls on the decoys (0.0–0.1 decoy-touching calls per episode). The herrings raise
ambient log volume and add candidate hypotheses, but a frontier model filters them; they may matter more for
weaker models and for fault composition. That is a measured property of the difficulty layer, not an assumption.

## Stack variation (un-memorizability)

Every world draws one of **8 coherent stack identities** from the seed, so "the
checkout-service playbook" (`cat checkout/config.py`, `sqlite3 data/checkout.db`,
`cat etc/cron.d/checkout-service`) does not transfer across episodes:

- **service name** — repo dir, systemd unit, nginx conf + upstream, cron.d file, `APP_NAME`
  (`checkout-service`, `storefront-api`, `order-service`, `commerce-gateway`, ...)
- **python package** — import path, `python -m <pkg>.serve`, logger prefixes and traceback
  frames in `app.log` (`checkout.access` vs `storefront.access`)
- **database filenames** — `data/checkout.db`/`data/ledger.db` vs `data/storefront.db`/`data/payments.db`, ...
- **API routes** — an optional prefix (`/api`, `/v1`) on the business endpoints and the checkout
  path word (`/checkout` vs `/v1/purchase`); `/health` and `/metrics` never move

The identity is consistent end to end — git history, `.env`, system files, three hours of
historical logs and metric labels, the live process, tool documentation, the page, and the
verifier's probes — and fault templates stay naming-agnostic: specs carry canonical endpoint
templates ("POST /checkout") that the verifier and the log generator render through the
world's naming. Injection machinery operates on the varied names too (a value-typo world on
`storefront-api` ships `LEDGER_DATABASE_URL=sqlite:///data/payments.ddb`). `--stack auto`
(default) picks per seed; `--stack classic` pins the original identity (used by the test
suite); `--stack <service-name>` selects a variant for debugging. `report.md` adds a
"By stack" breakdown.

Deliberately **not** varied: table/column names (low memorization value, large verifier/SQL
surface — a future lever) and the business domain itself (always an e-commerce checkout flow).

**Measured: variation is difficulty-neutral** (claude-sonnet-5, lean, `env_var_typo` @ 10 steps,
seeds 1–40 paired against the 200-seed classic baseline):

| comparison | classic | varied |
|---|---|---|
| all 40 seeds | 70% (28/40) | 57.5% (23/40; CI 42–71%) |
| the 33 seeds that drew a non-classic identity | 67% (22/33) | 64% (21/33) |
| the 7 seeds that drew the classic identity (same worlds, two runs) | 86% (6/7) | 29% (2/7) |

The paired non-classic delta is one episode; the headline gap is run-to-run sampling variance
(identical worlds swung 6/7 → 2/7), and the varied rate lands exactly on the 200-seed baseline
(58%, CI 51–65%). Failure taxonomy is unchanged (fixed_not_restarted + never_found, the known
10-step budget modes); trajectory triage found no naming-related artifacts.
Full report: `sweeps/varied-sonnet5-lean-steps10/report.md` (local).

## Calibration sweeps

`sregym sweep` runs many seeds through the same agent config, `--concurrency N` at a time
(each episode is its own temp world/port/agent instance), and is **resumable**: per-seed
results land in `<out>/results/seed-N.json` as they finish, and re-running the same
command skips completed seeds. API/infra failures (service didn't start, rate limits or
outages after the SDK's own retries) are retried with backoff and recorded as
`infra_error` — never as model failures. `report.md` / `summary.json` contain:

- success rate with a Wilson 95% CI, mean reward, reward histogram
- **failure taxonomy** (deterministic, from the verifier + trajectory): `success`,
  `collateral_damage`, `workaround` (service restored without fixing the config),
  `fixed_not_restarted`, `remediation_incomplete` (config fixed and restarted, data repair missing),
  `wrong_fix` (edited the config, still broken), `masked`
  (declared resolved without fixing), `gave_up`, `never_found`, `infra_error`
- breakdown by fault variant (env var × typo kind, innocent co-change), steps/tokens/
  duration/cost per episode, and a triage table of failed seeds with the hidden root cause

## CLI

```
sregym run       --seed N [--difficulty baseline|standard|hard] [--fault env_var_typo] [--agent anthropic|scripted] [--model ID] [--effort ...] [--thinking adaptive|off]
                 [--max-steps 30] [--token-budget 400000] [--history-minutes 180] [--workdir DIR] [--keep-world]
                 [--out DIR] [--no-traffic] [--mode solve|mask|workaround|noop|sloppy] [--quiet]
sregym verify    --world DIR [--trajectory FILE] [--start-service] [--json]
sregym replay    FILE [--step N] [--full] [--prompt] [--width N]
sregym generate  --seed N [--fault ...] [--workdir DIR] [--serve] [--reveal] [--history-minutes N] [--no-traffic]
sregym sweep     --seeds 1-200 --out DIR [--agent ...] [--model ID] [--concurrency 4] [--retries 2] [--rerun] [--keep-worlds] ...
sregym report    DIR [--json]
sregym rescore   DIR            # re-judge trajectory-only checks of saved results after a verifier fix (never re-runs models)
sregym faults
```

Runs go to `runs/<timestamp>-seed<seed>-<agent>/{trajectory.jsonl,result.json,prompt.txt}`.

## Layout

```
sregym/
  generator/   world.py (layout, git history, DBs, manifest, state hash) · data.py (Faker data, DB provisioning) · herrings.py
               logs.py (historical evidence trail) · app_source.py (templates → revisions) · traffic_profile.py
               templates/checkout-service/** (the app) · templates/system/* (nginx, systemd, cron)
  faults/      base.py (FaultTemplate, VerificationSpec, Check, IncidentProfile, registry) · env_var_typo.py · ledger_divergence.py · unapplied_migration.py · cron_write_lock.py · db_file_permissions.py · bad_dependency_pin.py · rate_limit_misconfig.py · disk_full.py · composed.py
  tools/       base.py (Tool, registry, path sandbox) · read_logs.py · query_metrics.py · read_file.py · edit_file.py
               run_shell.py · restart_service.py · resolve_incident.py
  runtime/     services.py (process supervisor) · traffic.py · metrics.py (collector) · cron.py (cron daemon)
  verifier/    verify.py (check interpreters, reward)
  harness/     episode.py · trajectory.py · prompts.py · sweep.py (calibration runner + report) · agents/{base,anthropic_adapter,scripted}.py
  scenario.py  (world -> fault -> history -> manifest) · cli.py
tests/         world · fault · tools (pagination, sandbox) · verifier (unfixed / fixed / masked / collateral) · episode · adapter
```

## Design notes & limitations (MVP)

* No containers: the "sandbox" is an allow-list + path confinement to `host/` + read-only sqlite,
  and the verifier's collateral checks are the real safety net. The answer key lives outside `host/`. Do not point this at a
  host you care about with an untrusted agent.
* nginx and cron are configuration + logs only (they are not running); the agent hits
  the uvicorn upstream directly. `deploy-bot` is fictional; the harness's service manager
  writes its own lines to `deploy.log` only for agent-triggered restarts.
* Metrics are a JSONL store rather than a real Prometheus; the app does expose a real
  Prometheus-format `/metrics` that the collector scrapes.
* Timestamps are real wall-clock UTC (the incident happened 18–40 minutes before
  generation) so live logs continue seamlessly from the history.
* Deliberately **not** built yet (next after the vertical slice): more fault templates,
  difficulty knobs, Docker orchestration, Verifiers-framework packaging, web UI.
