"""Step 3: tools -- pagination, filters, and the sandbox."""
from __future__ import annotations

import re

import pytest

from sregym.tools.base import ToolContext, default_registry


@pytest.fixture
def ctx(running):
    world, spec, sm = running
    return ToolContext(world, sm)


def _call(ctx, name, **args):
    return default_registry().call(name, args, ctx)


def test_read_logs_paginates_with_cursors_without_gaps(ctx):
    log = "checkout-service/logs/app.log"
    r = _call(ctx, "read_logs", path=log, limit=500)  # limit is clamped
    assert not r.is_error
    lines = [l for l in r.content.splitlines() if re.match(r"L\d+ ", l)]
    assert len(lines) == 50
    # walk a grep forwards through the whole file and check coverage against grep -c
    seen: list[int] = []
    r = _call(ctx, "read_logs", path=log, grep=r"checkout\.access.* 500 ")
    total = int(re.search(r"-> (\d+) matching lines", r.content).group(1))
    while True:
        seen += [int(m) for m in re.findall(r"^L(\d+)", r.content, re.M)]
        m = re.search(r"next_cursor: (\S+)", r.content)
        if not m or m.group(1).startswith("("):
            break
        r = _call(ctx, "read_logs", cursor=m.group(1))
        assert not r.is_error
    assert len(seen) == total == len(set(seen)) and seen == sorted(seen)
    assert total == sum(1 for l in ctx.world.app_log.read_text().splitlines() if re.search(r"checkout\.access.* 500 ", l))


def test_read_logs_tail_and_time_filters(ctx):
    log = "checkout-service/logs/app.log"
    r = _call(ctx, "read_logs", path=log, tail=True, limit=5)
    nums = [int(m) for m in re.findall(r"^L(\d+)", r.content, re.M)]
    total_lines = int(re.search(r"\((\d+) lines", r.content).group(1))
    assert nums[-1] == total_lines and len(nums) == 5 and "live_cursor" in r.content
    inc = ctx.world.extra  # noqa: F841
    onset = ctx.world.now  # window ends at 'now'
    since = (onset.replace(second=0)).strftime("%H:%M")
    r = _call(ctx, "read_logs", path=log, since=since, limit=10)
    assert not r.is_error and "since=" in r.content
    r = _call(ctx, "read_logs", path=log, grep="[unbalanced")
    assert r.is_error and "regex" in r.content
    r = _call(ctx, "read_logs")
    assert "app.log" in r.content and "deploy.log" in r.content and "nginx" in r.content


def test_path_sandbox(ctx):
    ctrl = ctx.world.control_dir
    for name, args in [("read_logs", {"path": "../.sregym/spec.json"}), ("read_file", {"path": str(ctrl / "manifest.json")}),
                       ("read_file", {"path": "/etc/passwd"}), ("read_file", {"path": "../../.."}),
                       ("edit_file", {"path": "../.sregym/x", "old_string": "", "new_string": "y"}),
                       ("edit_file", {"path": "checkout-service/logs/app.log", "old_string": "INFO", "new_string": "x"})]:
        r = _call(ctx, name, **args)
        assert r.is_error, (name, args, r.content)
    assert not (ctrl / "x").exists()


def test_control_plane_is_unreachable_from_the_host_root(ctx):
    """The answer key (.sregym/spec.json, world.json) must not be discoverable or readable via any tool --
    including recursive greps/finds from the host root, globs, .. traversal or absolute paths."""
    from sregym import util

    world = ctx.world
    ctrl = world.control_dir
    assert (ctrl / "spec.json").exists() and not util.is_within(ctrl, world.root)
    for cmd in ["ls -a", "find . -name '*.json'", "grep -rl root_cause_summary .", "grep -rl base_env .",
                "grep -r DATABASE_URL . | grep -v checkout-service", "cat .sregy*/spec.json", "cat .*/spec.json"]:
        r = _call(ctx, "run_shell", command=cmd)
        output = "\n".join(r.content.splitlines()[1:])  # drop the echoed `$ command` line
        for needle in (".sregym", "root_cause", "base_env", "hidden"):
            assert needle not in output, (cmd, r.content)
    for cmd in ["cat ../.sregym/spec.json", f"cat {ctrl}/spec.json", "grep -r root_cause_summary ..", f"ls {world.base}",
                f"grep -r root_cause_summary {world.base}"]:
        r = _call(ctx, "run_shell", command=cmd)
        assert r.is_error and "outside the host filesystem" in r.content, (cmd, r.content)
    for name, args in [("read_file", {"path": "../.sregym/spec.json"}), ("read_file", {"path": str(ctrl / "world.json")}),
                       ("read_logs", {"path": str(ctrl / "spec.json")}), ("read_file", {"path": str(world.base)})]:
        r = _call(ctx, name, **args)
        assert r.is_error and "outside the host filesystem" in r.content, (name, args, r.content)


@pytest.mark.parametrize("command", [
    "rm -rf checkout-service/logs", "rm checkout-service/.env", "rm checkout-service/README.md", "rm checkout-service/logs/app.log",
    "rm -r checkout-service/scripts", "cat checkout-service/.env > out.txt", "ls; rm -rf checkout-service", "ls && python3 -c 1",
    "true || cat /etc/passwd", "echo `id`", "echo $(id)",
    "cat /etc/hosts", "cat ../../../../etc/hosts", "ls ~", "git -C checkout-service reset --hard HEAD~1",
    "git -C checkout-service push origin main", "git -C checkout-service -c core.hooksPath=run log",
    "sqlite3 checkout-service/data/checkout.db 'DROP TABLE orders'", "sqlite3 checkout-service/data/checkout.db '.shell id'",
    "echo .quit | sqlite3 checkout-service/data/checkout.db", "sed -i 's/a/b/' checkout-service/.env",
    "find checkout-service -name '*.log' -delete", r"find checkout-service -exec rm {} \;", "curl https://example.com",
    "curl -o /dev/null http://127.0.0.1:1/", "python3 -c 'print(1)'", "cat ../.sregym/world.json", "awk 'BEGIN{system(\"id\")}'",
    "kill -9 1", "tee checkout-service/.env",
])
def test_run_shell_blocks_dangerous_commands(ctx, command):
    r = _call(ctx, "run_shell", command=command)
    assert r.is_error, (command, r.content)
    assert not (ctx.world.root / "out.txt").exists()


def test_run_shell_allows_investigation_commands(ctx):
    world = ctx.world
    r = _call(ctx, "run_shell", command="git -C checkout-service log --oneline -3")
    assert not r.is_error and len(r.content.splitlines()) >= 4
    r = _call(ctx, "run_shell", command="git -C checkout-service show HEAD -- .env")
    assert not r.is_error and "-DATABASE_URL" in r.content or "-LEDGER_DATABASE_URL" in r.content
    r = _call(ctx, "run_shell", command="sqlite3 checkout-service/data/checkout.db 'select count(*) from users'")
    assert not r.is_error and "read-only" in r.content
    r = _call(ctx, "run_shell", command="grep -c 'unable to open database file' checkout-service/logs/app.log")
    assert not r.is_error and int(r.content.splitlines()[1]) > 0
    r = _call(ctx, "run_shell", command=f"curl -s http://127.0.0.1:{world.port}/health")
    assert not r.is_error and "degraded" in r.content
    r = _call(ctx, "run_shell", command="cat checkout-service/.env | grep -c URL")
    assert not r.is_error
    r = _call(ctx, "run_shell", command="ls nonexistent-dir")
    assert r.is_error and "exit code" in r.content
    r = _call(ctx, "run_shell", command=f"cat {world.repo}/README.md | head -2")  # absolute path inside the world is fine
    assert not r.is_error and "checkout-service" in r.content
    # command sequences: each command validated on its own (what a real operator types)
    r = _call(ctx, "run_shell", command="cat checkout-service/.env; ls -la checkout-service/data && git -C checkout-service log --oneline -3")
    assert not r.is_error and "DATABASE_TIMEOUT_SECONDS" in r.content and "checkout.db" in r.content and "ops:" in r.content
    r = _call(ctx, "run_shell", command=f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{world.port}/health")
    assert not r.is_error and r.content.strip().endswith("503")
    r = _call(ctx, "run_shell", command=f"curl -s -o out.txt http://127.0.0.1:{world.port}/health")
    assert r.is_error and not (world.root / "out.txt").exists()


def test_agent_can_remove_only_its_own_files(ctx):
    r = _call(ctx, "edit_file", path="checkout-service/scripts/scratch.py", old_string="", new_string="print(1)\n")
    assert not r.is_error and (ctx.world.repo / "scripts" / "scratch.py").exists()
    r = _call(ctx, "run_shell", command="rm checkout-service/scripts/scratch.py")
    assert not r.is_error and not (ctx.world.repo / "scripts" / "scratch.py").exists()
    for bad in ["rm checkout-service/scripts/expire_carts.py", "rm checkout-service/.env", "rm -r checkout-service/scripts", "rm checkout-service/data/ledger.db"]:
        r = _call(ctx, "run_shell", command=bad)
        assert r.is_error, (bad, r.content)
    assert (ctx.world.repo / "scripts" / "expire_carts.py").exists() and ctx.world.env_file.exists()


def test_edit_file_requires_unique_match_and_reports_diff(ctx):
    r = _call(ctx, "edit_file", path="checkout-service/.env", old_string="URL", new_string="X")
    assert r.is_error and "times" in r.content
    r = _call(ctx, "edit_file", path="checkout-service/.env", old_string="LOG_LEVEL=INFO", new_string="LOG_LEVEL=DEBUG")
    assert not r.is_error and "-LOG_LEVEL=INFO" in r.content and "+LOG_LEVEL=DEBUG" in r.content
    assert "LOG_LEVEL=DEBUG" in ctx.world.env_file.read_text()
    r = _call(ctx, "edit_file", path="checkout-service/NOTES.md", old_string="", new_string="hello\n")
    assert not r.is_error and (ctx.world.repo / "NOTES.md").read_text() == "hello\n"


def test_query_metrics_lists_and_groups(ctx):
    r = _call(ctx, "query_metrics")
    assert "http_requests_total" in r.content and "http_error_rate" in r.content
    r = _call(ctx, "query_metrics", metric="http_error_rate", window_minutes=60, group_by="path", step_minutes=5)
    assert not r.is_error and "/checkout" in r.content and "100.0" in r.content
    r = _call(ctx, "query_metrics", metric="nope")
    assert r.is_error


def test_restart_service_reports_status(ctx):
    r = _call(ctx, "restart_service", action="status")
    assert "active (running)" in r.content and "503" in r.content
    r = _call(ctx, "restart_service", service="nginx")
    assert r.is_error
    r = _call(ctx, "restart_service")
    assert not r.is_error and "started" in r.content and "Health: GET /health -> 503" in r.content
