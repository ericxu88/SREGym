# sregym-env

**Infinite, verifiable, un-memorizable on-call incidents** — SREGym packaged as a native
[verifiers](https://github.com/PrimeIntellect-ai/verifiers) v1 taskset.

Every task is one procedurally generated production incident. A seed deterministically builds a
complete stack on local disk — a FastAPI service, SQLite databases, a git repo with a plausible
multi-month history, nginx/systemd/cron config, and hours of coherent logs and metrics — draws one of
several **stack identities** (service/package/db/route names) so no playbook transfers between
episodes, and injects a known fault from a template library of 10 measured incident types (config
typos, silent ledger divergence, unapplied migrations, cron write-lock storms, permission clamps,
crash-looping dependency pins, rate-limit misconfigurations, db quota exhaustion, rotated shared
secrets, torn config writes) plus vetted two-fault compositions.

The agent investigates through SREGym's sandboxed on-call toolset — paginated log reading, a metrics
store, file read/edit, an allow-listed read-mostly shell, service control — served by an MCP tool
server that owns the **live** world for that rollout: the faulty service actually runs, synthetic
traffic keeps flowing, metrics keep scraping, cron keeps firing.

Scoring is **deterministic — no LLM judge**. A 3-part verifier runs against the live world at the
terminal moment: symptom resolved (live HTTP/behavior probes), true root cause fixed (the narrowest
causal object; workarounds and code patches around the problem are caught), no collateral damage
(file manifest, DB row hashes, log preservation, git history, structural forbidden-action judging).
Reward 1.0 requires all three; partial credit is 0.3·symptom + 0.7·root-cause, halved on collateral
damage. Every template's rubric is calibrated against measured frontier-model runs.

## Usage

```bash
validate sregym-env -n 2          # model-free wiring check
eval sregym-env -n 10 -m <model>  # bound the infinite taskset with -n
```

Config knobs (`--env.taskset.*`): `faults` (comma list or "all"), `difficulty`
(baseline|standard|hard — step budget + seeded red herrings), `stack` (auto|classic|variant),
`seed-start`, `max-steps`, `history-minutes`.

The default harness is the taskset's own null-based tool loop (`SREGymHarness`): the model gets
exactly the sandboxed MCP toolset. Don't pair this taskset with a shell-bearing harness — a raw
shell bypasses the sandbox that the collateral/forbidden-action scoring is built on.

Difficulty is calibrated (Claude Sonnet 5 baselines range 55–100% by template at profile budgets;
`standard`/`hard` profiles and fault composition push into the 25–60% band). Source, measured
baselines, and the calibration methodology: https://github.com/ericxu88/SREGym
