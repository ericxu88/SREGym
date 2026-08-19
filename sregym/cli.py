"""Command line: ``sregym run|verify|replay|generate|faults``."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

from sregym import util


def _cmd_run(args: argparse.Namespace) -> int:
    from sregym.harness.agents import make_agent
    from sregym.harness.episode import EpisodeConfig, run_episode

    if args.agent == "anthropic":
        from sregym.harness.agents.anthropic_adapter import api_credentials_present

        if not api_credentials_present():
            print("warning: no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN and no `ant auth login` profile found; "
                  "the API call will likely fail (use --agent scripted for an offline demo)", file=sys.stderr)
    agent = make_agent(args.agent, **_agent_kwargs(args))
    config = EpisodeConfig(
        seed=args.seed, fault=args.fault, max_steps=args.max_steps, token_budget=args.token_budget,
        workdir=Path(args.workdir) if args.workdir else None, keep_world=args.keep_world,
        history_minutes=args.history_minutes, out_dir=Path(args.out) if args.out else None,
        live_traffic=not args.no_traffic, prompt_style=args.prompt_style,
    )
    result = run_episode(agent, config, verbose=not args.quiet)
    print()
    print(f"seed={result.seed} fault={result.fault} agent={result.agent} steps={result.steps} stop={result.stop_reason} "
          f"tokens={result.usage} duration={result.duration_s}s")
    print(f"reward={result.reward:.2f} success={result.success}  "
          f"symptom={result.verification['symptom_resolved']} root_cause={result.verification['root_cause_fixed']} "
          f"collateral_ok={result.verification['no_collateral_damage']}")
    print(f"trajectory: {result.trajectory_path}")
    if result.error:
        print(f"error: {result.error.splitlines()[0]}", file=sys.stderr)
    if config.keep_world:
        print(f"world kept at: {result.world_root}")
    return 0 if result.error is None else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    from sregym.faults.base import VerificationSpec
    from sregym.generator.world import World
    from sregym.harness.trajectory import read_trajectory
    from sregym.runtime.services import ServiceManager
    from sregym.verifier.verify import verify

    world = World.load(Path(args.world))
    spec = VerificationSpec.load(world)
    steps = []
    if args.trajectory:
        _, steps, _ = read_trajectory(Path(args.trajectory))
    sm = None
    if args.start_service:
        sm = ServiceManager(world)
        print(sm.start())
    elif not util.port_open(world.port):
        print(f"note: nothing is listening on 127.0.0.1:{world.port}; symptom checks will fail "
              f"(pass --start-service to start the service from the current on-disk state)", file=sys.stderr)
    try:
        result = verify(world, spec, world.load_manifest(), steps)
    finally:
        if sm:
            sm.close()
    print(result.summary())
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.success else 1


def _cmd_replay(args: argparse.Namespace) -> int:
    from sregym.harness.trajectory import read_trajectory

    meta, steps, end = read_trajectory(Path(args.trajectory))
    width = args.width
    if meta:
        print(f"== episode seed={meta.get('seed')} fault={meta.get('fault')} agent={meta.get('agent')} "
              f"max_steps={meta.get('max_steps')} started={meta.get('started_at')}")
        if args.full or args.prompt:
            print("\n-- system prompt --\n" + meta.get("system_prompt", ""))
        print("\n-- task prompt --\n" + meta.get("task_prompt", ""))
    for s in steps:
        if args.step and s["step"] != args.step:
            continue
        print(f"\n== step {s['step']}  {s.get('ts', '')}  state={s.get('state_hash', '')[:23]}")
        if s.get("assistant_text"):
            print(textwrap.indent(_clip(s["assistant_text"], None if args.full else width), "  agent> "))
        print(f"  call > {s['tool_call']}({json.dumps(s.get('tool_args', {}))[:2000]})")
        tag = "  error> " if s.get("tool_error") else "  result> "
        print(textwrap.indent(_clip(s.get("tool_result", ""), None if args.full else width), tag))
    if end:
        v = end.get("verification", {})
        print(f"\n== end: stop_reason={end.get('stop_reason')} reward={end.get('reward')} success={end.get('success')} "
              f"steps={end.get('steps')} usage={end.get('usage')}")
        if v:
            print(f"   symptom_resolved={v.get('symptom_resolved')} root_cause_fixed={v.get('root_cause_fixed')} "
                  f"no_collateral_damage={v.get('no_collateral_damage')}")
            for c in v.get("checks", []):
                print(f"   [{'x' if c['passed'] else ' '}] {c['criterion']:<10} {c['name']:<26} {c['detail'][:160]}")
        if end.get("agent_summary"):
            print(f"   agent postmortem: {end['agent_summary']}")
        if args.full and end.get("hidden_root_cause"):
            print(f"   hidden root cause: {end['hidden_root_cause']}")
    else:
        print("\n== (no end record: episode did not finish)")
    return 0


def _clip(text: str, width: int | None) -> str:
    if width is None or len(text) <= width:
        return text
    return text[:width] + f"\n... [{len(text) - width} more chars; use --full]"


def _cmd_generate(args: argparse.Namespace) -> int:
    from sregym.harness.episode import LiveWorld
    from sregym.scenario import prepare_world
    from sregym.harness.prompts import build_task_prompt

    root = Path(args.workdir) / f"world-seed{args.seed}-{time.strftime('%Y%m%d-%H%M%S')}" if args.workdir else None
    world, spec = prepare_world(args.seed, args.fault, root=root, history_minutes=args.history_minutes)
    print(f"world:     {world.base}   (control plane: {world.control_dir})")
    print(f"host root: {world.root}")
    print(f"repo:      {world.repo}")
    print(f"service:   {world.base_url}  (not started{' -- use --serve' if not args.serve else ''})")
    print(f"company:   {world.company} ({world.domain})")
    print(f"fault:     {spec.fault}  {spec.notes}")
    if args.reveal:
        print(f"root cause: {spec.incident.root_cause_summary}")
    print("\n" + build_task_prompt(world, spec.incident))
    if not args.serve:
        return 0
    live = LiveWorld(world, traffic_rps=args.traffic_rps, live_traffic=not args.no_traffic)
    print("\n" + live.start())
    print("service, synthetic traffic and metrics collector running; Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        live.stop()
        print("stopped.")
    return 0


def _agent_kwargs(args: argparse.Namespace) -> dict:
    if args.agent == "anthropic":
        return {"model": args.model, "max_tokens": args.max_tokens, "thinking": args.thinking, "effort": args.effort}
    return {"mode": args.mode}


def _cmd_sweep(args: argparse.Namespace) -> int:
    from sregym.harness.sweep import SweepConfig, parse_seeds, run_sweep

    if args.agent == "anthropic":
        from sregym.harness.agents.anthropic_adapter import api_credentials_present

        if not api_credentials_present():
            print("warning: no Anthropic credentials found (ANTHROPIC_API_KEY / ./.env / ant profile)", file=sys.stderr)
    cfg = SweepConfig(
        seeds=parse_seeds(args.seeds), out_dir=Path(args.out), agent=args.agent, agent_kwargs=_agent_kwargs(args),
        fault=args.fault, max_steps=args.max_steps, token_budget=args.token_budget, concurrency=args.concurrency,
        history_minutes=args.history_minutes, live_traffic=not args.no_traffic, retries=args.retries, rerun=args.rerun,
        keep_worlds=args.keep_worlds, prompt_style=args.prompt_style,
    )
    summary = run_sweep(cfg)
    if summary.get("n_model_results"):
        lo, hi = summary["success_ci95"]
        print(f"\nsuccess {summary['success']}/{summary['n_model_results']} = {100 * summary['success_rate']:.1f}% "
              f"(95% CI {100 * lo:.0f}-{100 * hi:.0f}%)  mean reward {summary['mean_reward']:.3f}  "
              f"cost {('$%.2f' % summary['cost_usd_total']) if summary['cost_usd_total'] is not None else 'n/a'}")
        print("outcomes: " + ", ".join(f"{k}={v}" for k, v in summary["outcomes"].items() if v))
    print(f"report: {cfg.out_dir / 'report.md'}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from sregym.harness.sweep import build_report

    summary, md = build_report(Path(args.sweep_dir))
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(md)
    return 0


def _cmd_rescore(args: argparse.Namespace) -> int:
    from sregym.harness.sweep import rescore_forbidden_actions

    out = rescore_forbidden_actions(Path(args.sweep_dir))
    print(f"examined {out['examined']} results; changed {len(out['changed'])}")
    for c in out["changed"]:
        print(f"  seed {c['seed']}: {c['outcome']} {c['reward']} -> {c['new_outcome']} {c['new_reward']}")
    return 0


def _cmd_faults(args: argparse.Namespace) -> int:
    from sregym.faults.base import list_faults

    for name, desc in list_faults().items():
        print(f"{name:<20} {desc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sregym", description="SREGym: procedurally generated incident-response environment")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="generate a world, run an agent, verify, write a trajectory")
    r.add_argument("--seed", type=int, required=True)
    r.add_argument("--fault", default="env_var_typo")
    r.add_argument("--agent", choices=["anthropic", "scripted"], default="anthropic")
    r.add_argument("--model", default="claude-opus-5", help="Anthropic model id (agent=anthropic), e.g. claude-opus-5, claude-sonnet-5")
    r.add_argument("--max-tokens", type=int, default=16000, help="max output tokens per model turn")
    r.add_argument("--thinking", choices=["adaptive", "off"], default="adaptive", help="adaptive thinking (default) or none")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None, help="output_config.effort (default: API default)")
    r.add_argument("--mode", default="solve", help="scripted agent mode: solve|mask|workaround|noop|sloppy")
    r.add_argument("--max-steps", type=int, default=30)
    r.add_argument("--prompt-style", choices=["full", "lean"], default="full", help="system prompt variant (lean = no spelled-out resolution norms)")
    r.add_argument("--token-budget", type=int, default=400_000)
    r.add_argument("--history-minutes", type=int, default=180)
    r.add_argument("--workdir", help="parent directory for the generated world (default: system temp)")
    r.add_argument("--keep-world", action="store_true", help="do not delete the world directory afterwards")
    r.add_argument("--out", help="output directory for trajectory.jsonl/result.json (default runs/<ts>-seed<seed>-<agent>)")
    r.add_argument("--no-traffic", action="store_true", help="disable synthetic background traffic")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(func=_cmd_run)

    v = sub.add_parser("verify", help="re-run the deterministic verifier against an existing world directory")
    v.add_argument("--world", required=True, help="world directory (contains .sregym/ and host/)")
    v.add_argument("--trajectory", help="trajectory.jsonl for forbidden-action checks")
    v.add_argument("--start-service", action="store_true", help="start the service from the current on-disk state first")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=_cmd_verify)

    rp = sub.add_parser("replay", help="pretty-print a trajectory offline")
    rp.add_argument("trajectory")
    rp.add_argument("--step", type=int, help="show only this step")
    rp.add_argument("--full", action="store_true", help="do not truncate observations/results")
    rp.add_argument("--prompt", action="store_true", help="also print the system prompt")
    rp.add_argument("--width", type=int, default=1200, help="max chars per observation when not --full")
    rp.set_defaults(func=_cmd_replay)

    g = sub.add_parser("generate", help="generate a world (optionally keep it running for manual exploration)")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--fault", default="env_var_typo")
    g.add_argument("--workdir", help="parent directory for the world (default: system temp)")
    g.add_argument("--history-minutes", type=int, default=180)
    g.add_argument("--serve", action="store_true", help="start the service + traffic and wait for Ctrl-C")
    g.add_argument("--traffic-rps", type=float, default=1.5)
    g.add_argument("--no-traffic", action="store_true")
    g.add_argument("--reveal", action="store_true", help="print the hidden root cause")
    g.set_defaults(func=_cmd_generate)

    sw = sub.add_parser("sweep", help="run many seeds concurrently (resumable) and write a calibration report")
    sw.add_argument("--seeds", required=True, help="e.g. 1-200 or 1-50,60,70-80")
    sw.add_argument("--out", required=True, help="sweep directory (results, per-episode trajectories, report.md)")
    sw.add_argument("--fault", default="env_var_typo")
    sw.add_argument("--agent", choices=["anthropic", "scripted"], default="anthropic")
    sw.add_argument("--model", default="claude-opus-5")
    sw.add_argument("--max-tokens", type=int, default=16000)
    sw.add_argument("--thinking", choices=["adaptive", "off"], default="adaptive")
    sw.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    sw.add_argument("--mode", default="solve", help="scripted agent mode")
    sw.add_argument("--max-steps", type=int, default=30)
    sw.add_argument("--prompt-style", choices=["full", "lean"], default="full")
    sw.add_argument("--token-budget", type=int, default=400_000)
    sw.add_argument("--concurrency", type=int, default=4, help="episodes in flight at once (each is its own world/port)")
    sw.add_argument("--retries", type=int, default=2, help="retries per seed for infra/API errors")
    sw.add_argument("--history-minutes", type=int, default=180)
    sw.add_argument("--no-traffic", action="store_true")
    sw.add_argument("--rerun", action="store_true", help="ignore existing results for these seeds")
    sw.add_argument("--keep-worlds", action="store_true")
    sw.set_defaults(func=_cmd_sweep)

    rep = sub.add_parser("report", help="(re)generate the report for a sweep directory")
    rep.add_argument("sweep_dir")
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=_cmd_report)

    rs = sub.add_parser("rescore", help="re-judge trajectory-only checks of saved sweep results with the current verifier")
    rs.add_argument("sweep_dir")
    rs.set_defaults(func=_cmd_rescore)

    f = sub.add_parser("faults", help="list fault templates")
    f.set_defaults(func=_cmd_faults)
    return p


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from ./.env into the environment (existing variables win).

    Lets you keep ANTHROPIC_API_KEY in a git-ignored .env next to the project instead of
    exporting it in every shell. Nothing is ever printed.
    """
    path = Path(".env")
    if not path.is_file():
        return
    try:
        for key, value in util.parse_env_file(path.read_text()).items():
            os.environ.setdefault(key, value)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # progress is useful even when redirected to a file
    except (AttributeError, ValueError):
        pass
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
