# sregym-env

On-call incident response as a verifiers taskset. Infinite, procedurally generated,
deterministically scored — no LLM judge.

Each task is one incident. A seed builds a complete production stack on disk (a
FastAPI service, SQLite databases, a git repo with months of history, nginx/systemd/
cron config, hours of coherent logs and metrics), draws one of 8 stack identities so
no playbook carries over between episodes, and injects a fault from a library of 10
templates: config typos, silent ledger divergence, unapplied migrations, cron
write-lock storms, permission clamps, crash-looping dependency pins, rate-limit
misconfigurations, DB quota exhaustion, rotated shared secrets, torn config writes.
Two-fault compositions too.

The agent works through a sandboxed on-call toolset (paginated logs, metrics, file
edit, an allow-listed shell, service control) served by an MCP server that owns a
live world per rollout — the faulty service actually runs, traffic keeps flowing,
cron keeps firing.

Scoring is a 3-part deterministic verifier run against the live world: symptom
resolved (HTTP and behavior probes), true root cause fixed (workarounds and
patch-arounds are caught), no collateral damage (file hashes, DB rows, logs, git
history, forbidden actions). Reward 1.0 needs all three; otherwise
0.3·symptom + 0.7·root-cause, halved on collateral damage. Rubrics are calibrated
against measured frontier-model runs — Claude Sonnet 5 baselines run 55–100% by
template at profile budgets, and standard/hard profiles plus composition reach the
25–60% band.

## Usage

```bash
validate sregym-env -n 2          # model-free wiring check
eval sregym-env -n 10 -m <model>  # infinite taskset, so bound with -n
```

Knobs (`--env.taskset.*`): `faults` (comma list or "all"), `difficulty`
(baseline|standard|hard), `stack` (auto|classic|variant), `seed-start`, `max-steps`,
`history-minutes`.

The default harness is the taskset's own plain tool loop: the model gets exactly the
sandboxed MCP tools, no bash, no host filesystem. Don't pair it with a shell-bearing
harness — that bypasses the sandbox the scoring depends on.

Source, measured baselines, and methodology: https://github.com/ericxu88/SREGym
