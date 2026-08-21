# sregym-env

SREGym as a verifiers taskset: on-call incident response with procedural generation
and deterministic scoring. No LLM judge anywhere.

Each task is one incident. A seed builds a full production stack on disk: a FastAPI
service, SQLite databases, a git repo with months of history, nginx/systemd/cron
config, and hours of logs and metrics that agree with each other. The world picks one
of 8 stack identities (service name, package name, database files, route prefixes) so
nothing carries over between episodes, then injects a fault from a library of 10
templates: config typos, silent ledger divergence, unapplied migrations, cron
write-lock storms, permission clamps, crash-looping dependency pins, rate limit
misconfigurations, db quota exhaustion, rotated shared secrets, and torn config
writes. There are also two-fault compositions.

The agent works through a sandboxed on-call toolset over MCP: paginated log reading,
metrics queries, file read/edit, an allow-listed shell, and service control. Each
rollout gets its own live world. The broken service really runs, synthetic traffic
keeps flowing, and cron keeps firing.

Scoring is a deterministic three-part verifier that runs against the live world: did
the symptom actually resolve (HTTP probes), was the real root cause fixed (workarounds
and code patches around the problem are caught), and was anything else damaged (file
hashes, db rows, logs, git history, destructive commands). Reward is 1.0 only when all
three pass, otherwise 0.3*symptom + 0.7*root_cause, halved on collateral damage. The
rubrics were calibrated against measured runs. Claude Sonnet 5 baselines range from 55
to 100% depending on the template, and the standard/hard profiles plus composition
land in the 25 to 60% band.

## Usage

```bash
validate sregym-env -n 2          # model-free wiring check
eval sregym-env -n 10 -m <model>  # the taskset is infinite, so bound it with -n
```

Config knobs under `--env.taskset.*`: `faults` (comma list or "all"), `difficulty`
(baseline|standard|hard), `stack` (auto|classic|variant), `seed-start`, `max-steps`,
`history-minutes`.

The default harness is a plain tool loop. The model only gets the sandboxed MCP tools,
no bash and no host filesystem. Don't run this taskset with a shell-bearing harness,
since a raw shell bypasses the sandbox the scoring depends on.

Source, measured baselines, and methodology: https://github.com/ericxu88/SREGym
