"""Sandboxed shell: allow-listed, read-mostly commands run inside the world root.

Safety model (MVP, no containers): the command is tokenized with shell semantics,
every command in a pipeline/sequence must start with an allow-listed program, shell
control operators other than ``|``, ``;``, ``&&``, ``||`` are rejected, path-like arguments must stay inside the
host root (the control plane lives outside it, so nothing name-based is needed), and a few programs get
extra rules (git subcommands, sqlite3 forced read-only, no ``sed -i``, no
``find -delete/-exec``, curl only to localhost). The *reconstructed* command is
executed with ``/bin/sh -c`` in a clean environment. Destructive actions that slip
through are still caught by the verifier's collateral-damage checks.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from sregym import util
from sregym.tools.base import Tool, ToolContext, ToolError, ToolResult

ALLOWED = {
    "cat", "head", "tail", "grep", "egrep", "fgrep", "zgrep", "ls", "wc", "cut", "sort", "uniq", "tr", "awk", "sed",
    "diff", "find", "stat", "file", "du", "df", "pwd", "echo", "printf", "date", "env", "ps", "git", "sqlite3", "curl",
    "which", "whoami", "hostname", "uptime", "uname", "jq", "tac", "rev", "nl", "column", "basename", "dirname",
    "realpath", "readlink", "shasum", "md5", "md5sum", "sha256sum", "true", "false", "test", "[", "strings", "comm",
    "paste", "expand", "fold", "od", "xxd", "hexdump", "seq", "tree", "less", "more", "id", "lsof", "netstat", "ss",
    "python", "python3",  # only `python <repo>/scripts/<name>.py ...` for unmodified, generation-time scripts (see below)
    "rm",  # only files the agent created itself (not in the generation-time manifest, not under data/logs/...)
    "chmod",  # host-root-confined; permissions are real operational state here (see db_file_permissions fault)
}
PROTECTED_DIRS = {".git", "data", "logs", "var", "run", "metrics", "__pycache__"}  # never rm-able
GIT_ALLOWED = {
    "log", "show", "diff", "status", "blame", "grep", "ls-files", "rev-parse", "cat-file", "branch", "tag", "checkout",
    "revert", "reflog", "describe", "shortlog", "stash", "add", "commit", "restore", "switch", "rev-list", "name-rev",
    "whatchanged", "ls-tree", "show-branch", "diff-tree", "var", "help", "version", "--version",
}
GIT_DENIED_ARGS = {"expire", "delete", "--force", "-f", "--hard", "--exec", "--edit"}
SQLITE_DOT_ALLOWED = {".tables", ".schema", ".indexes", ".indices", ".headers", ".header", ".mode", ".dbinfo", ".width",
                      ".nullvalue", ".timer", ".print", ".show", ".databases", ".fullschema", ".stats", ".help", ".quit", ".exit"}
FIND_DENIED = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
CURL_DENIED = {"-O", "--remote-name", "-T", "--upload-file", "-K", "--config", "--create-dirs", "-D", "--dump-header"}
SEQUENCE_OPERATORS = {";", "&&", "||"}  # allowed between commands: each command is validated on its own
CONTROL_OPERATORS = {"&", ">", ">>", "<", "<<", "<<<", ">|", "&>", "|&", "(", ")"}
NULL_SINK = "/dev/null"
TIMEOUT_S = 60


def tokenize_command(command: str) -> list[tuple[str, list[str]]]:
    """Split into (operator, argv) items where operator is the token joining this command to the
    previous one ('' for the first, then '|', ';', '&&' or '||'). Anything else is rejected."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError as e:
        raise ToolError(f"cannot parse command: {e}") from e
    if not tokens:
        raise ToolError("empty command")
    items: list[tuple[str, list[str]]] = [("", [])]
    for tok in tokens:
        if tok == "|" or tok in SEQUENCE_OPERATORS:
            if not items[-1][1]:
                raise ToolError(f"empty command before {tok!r}")
            items.append((tok, []))
            continue
        if tok in CONTROL_OPERATORS or (len(tok) > 1 and set(tok) <= set(";&|<>()")):
            raise ToolError(f"shell operator {tok!r} is not allowed (commands may be joined with '|', ';', '&&', '||'); "
                            "use edit_file to write files")
        if "`" in tok or "$(" in tok or "${" in tok:
            raise ToolError("command substitution is not allowed")
        items[-1][1].append(tok)
    if not items[-1][1]:
        raise ToolError("trailing operator")
    return items


def _check_paths(argv: list[str], ctx: ToolContext) -> None:
    root = ctx.world.root.resolve()
    for tok in argv:
        if tok.startswith("~"):
            raise ToolError("home-relative paths are not allowed")
        candidates = [tok]
        if "=" in tok and not tok.startswith("http"):
            candidates.append(tok.split("=", 1)[1])
        for cand in candidates:
            if cand == NULL_SINK:
                continue  # discarding output is always fine
            if cand.startswith(("http://", "https://")):
                if not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", cand):
                    raise ToolError("network access is limited to the local service (127.0.0.1)")
                continue
            if "/" not in cand and ".." not in cand:
                continue
            # strip glob tails / trailing punctuation for the containment check
            core = re.split(r"[*?\[]", cand, 1)[0].rstrip(":,")
            if not core:
                continue
            p = (root / core) if not core.startswith("/") else util.Path(core)
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if core.startswith("/") or ".." in core:
                if not util.is_within(resolved, root):
                    raise ToolError(f"path {cand!r} is outside the host filesystem you have access to")


def _validate_segment(argv: list[str], piped_in: bool, ctx: ToolContext) -> list[str]:
    prog = os.path.basename(argv[0])
    if prog not in ALLOWED:
        raise ToolError(f"command {argv[0]!r} is not allowed. Allowed: {', '.join(sorted(ALLOWED))}")
    argv = [prog] + argv[1:]
    _check_paths(argv[1:], ctx)
    if prog == "git":
        args = argv[1:]
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C" and i + 1 < len(args):
                i += 2  # -C <path>: path already containment-checked above
                continue
            if args[i] not in ("--no-pager", "-P", "--version", "--help"):
                raise ToolError(f"git option {args[i]} is not allowed")
            i += 1
        sub = args[i] if i < len(args) else "status"
        if sub not in GIT_ALLOWED:
            raise ToolError(f"git {sub} is not allowed (allowed: {', '.join(sorted(GIT_ALLOWED))})")
        for a in args[i + 1:]:
            if a in GIT_DENIED_ARGS or a.startswith("--force"):
                raise ToolError(f"git {sub} {a} is not allowed")
        if sub == "stash" and any(a in ("drop", "clear") for a in args[i + 1:]):
            raise ToolError("git stash drop/clear is not allowed")
        if sub in ("branch", "tag") and any(a in ("-d", "-D", "--delete") for a in args[i + 1:]):
            raise ToolError(f"git {sub} deletion is not allowed")
    elif prog == "sqlite3":
        if piped_in:
            raise ToolError("sqlite3 cannot read commands from a pipe (no stdin scripts); pass SQL as an argument")
        for a in argv[1:]:
            low = a.strip().lower()
            if low.startswith("."):
                if low.split()[0] not in SQLITE_DOT_ALLOWED:
                    raise ToolError(f"sqlite3 dot-command {a.split()[0]!r} is not allowed")
            if a in ("-init", "-cmd"):
                raise ToolError(f"sqlite3 option {a} is not allowed")
            if re.search(r"\b(insert|update|delete|drop|alter|create|replace|attach|vacuum|pragma\s+writable)\b", low):
                raise ToolError("sqlite3 access is read-only on this host")
        argv = ["sqlite3", "-readonly", *argv[1:]]
    elif prog == "find":
        for a in argv[1:]:
            if a in FIND_DENIED:
                raise ToolError(f"find {a} is not allowed")
    elif prog == "sed":
        for a in argv[1:]:
            if a == "--in-place" or a.startswith("--in-place=") or (a.startswith("-") and not a.startswith("--") and "i" in a[1:]):
                raise ToolError("sed -i (in-place editing) is not allowed; use edit_file")
            if re.search(r"(^|[;\n])\s*w\s", a) or re.search(r"/w\s+\S", a):
                raise ToolError("sed write commands are not allowed")
    elif prog == "awk":
        for a in argv[1:]:
            if re.search(r"system\s*\(|getline|>\s*\"|\|\s*\"", a):
                raise ToolError("awk programs may not run commands or write files")
    elif prog == "curl":
        for j, a in enumerate(argv[1:], start=1):
            if a in CURL_DENIED:
                raise ToolError(f"curl {a} is not allowed")
            if a in ("-o", "--output") and (j + 1 >= len(argv) or argv[j + 1] != NULL_SINK):
                raise ToolError("curl -o may only write to /dev/null (use -s -o /dev/null -w '%{http_code}' for status codes)")
            if a.startswith("--output=") and a != f"--output={NULL_SINK}":
                raise ToolError("curl --output may only write to /dev/null")
    elif prog == "sort":
        for a in argv[1:]:
            if a in ("-o", "--output") or a.startswith("--output="):
                raise ToolError("sort -o is not allowed")
    elif prog == "env" and len(argv) > 1:
        raise ToolError("env may only be used without arguments")
    elif prog in ("less", "more"):
        argv = ["cat", *argv[1:]]
    elif prog in ("python", "python3"):
        argv = _validate_python(argv, ctx)
    elif prog == "rm":
        _validate_rm(argv, ctx)
    return argv


def _validate_rm(argv: list[str], ctx: ToolContext) -> None:
    """`rm <file>...`: only plain files the agent created during the episode (clean up after yourself).
    Generation-time files, directories, and anything under data/logs/var/run/.git are refused."""
    world = ctx.world
    targets = [a for a in argv[1:] if not a.startswith("-")]
    for opt in (a for a in argv[1:] if a.startswith("-")):
        if opt in ("-r", "-R", "--recursive", "-rf", "-fr", "-Rf", "-fR") or ("r" in opt.lstrip("-").lower() and not opt.startswith("--")):
            raise ToolError("rm -r is not allowed")
    if not targets:
        raise ToolError("rm needs a file to remove")
    for t in targets:
        full = (world.root / t if not t.startswith("/") else Path(t)).resolve()
        if not util.is_within(full, world.root):
            raise ToolError(f"path {t!r} is outside the host filesystem you have access to")
        rel = util.relpath(full, world.root)
        if any(part in PROTECTED_DIRS for part in Path(rel).parts):
            raise ToolError(f"rm {t}: files under data/, logs/, var/, run/ or .git are protected")
        if full.is_dir():
            raise ToolError(f"rm {t}: directories cannot be removed")
        if rel in ctx.manifest_files:
            raise ToolError(f"rm {t}: this file is part of the deployed system; only files you created yourself can be removed")


def _validate_python(argv: list[str], ctx: ToolContext) -> list[str]:
    """`python scripts/<name>.py args...`: run an ops script that ships with the repo.

    Only scripts whose content is byte-identical to the generation-time manifest are allowed (so a
    script edited by the agent cannot be used to run arbitrary code), no interpreter options, and
    the script runs from the repo root (where the app's .env lives) via a subshell. Path-like
    arguments given relative to the host root are made absolute so they keep working."""
    world = ctx.world
    if len(argv) < 2 or argv[1].startswith("-"):
        raise ToolError("python may only run an ops script shipped with the repo: python checkout-service/scripts/<name>.py [args]")
    script = argv[1]
    candidates = [world.root / script, world.repo / script]
    full = next((c.resolve() for c in candidates if c.is_file()), None)
    if full is None or not util.is_within(full, world.repo / "scripts"):
        raise ToolError(f"{script!r} is not a runnable repo script (allowed: {', '.join(sorted(ctx.allowed_scripts)) or 'none'})")
    rel = util.relpath(full, world.root)
    expected = ctx.allowed_scripts.get(rel)
    if expected is None:
        raise ToolError(f"{rel} is not a runnable repo script (allowed: {', '.join(sorted(ctx.allowed_scripts)) or 'none'})")
    if util.sha256_file(full) != expected:
        raise ToolError(f"{rel} has been modified; only the shipped version of repo scripts may be executed")
    args: list[str] = []
    for a in argv[2:]:
        for base in (world.root, world.repo):
            cand = base / a
            if not a.startswith("-") and cand.exists() and util.is_within(cand, world.root):
                a = str(cand.resolve())
                break
        args.append(a)
    # executed as: (cd <repo> && <python> scripts/<name>.py args)
    return ["__SREGYM_REPO_SCRIPT__", world.python, str(full.relative_to(world.repo.resolve())), *args]


class RunShellTool(Tool):
    name = "run_shell"
    max_output_chars = 12000
    description = (
        "Run a read-mostly shell command in the host root (working directory). Allowed: common inspection commands "
        "(cat, grep, ls, find, head, tail, wc, diff, stat, ps, ...), git (log/show/diff/blame/checkout/revert/commit/... "
        "no reset/clean/push), sqlite3 (read-only), curl to 127.0.0.1 only, `python checkout-service/scripts/<name>.py ...` "
        "for the ops scripts that ship with the repo, and rm only for files you created yourself. Pipes and "
"joining commands with ';', '&&', '||' is fine; no redirection or command substitution (curl -o /dev/null is allowed). "
        "Use edit_file to change files and restart_service to restart."
    )
    input_schema = {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The command line to run, e.g. `git -C checkout-service log --oneline -5` or `cd`-free paths relative to the host root."}},
        "required": ["command"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("command is required")
        world = ctx.world
        items = tokenize_command(command)
        parts: list[str] = []
        for op, argv in items:
            argv = _validate_segment(argv, piped_in=(op == "|"), ctx=ctx)
            if argv and argv[0] == "__SREGYM_REPO_SCRIPT__":
                segment = f"(cd {shlex.quote(str(world.repo))} && {shlex.join(argv[1:])})"
            else:
                segment = shlex.join(argv)
            parts.append((f" {op} " if op else "") + segment)
        rebuilt = "".join(parts)
        home = world.root / "run" / ".home"
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home), "LANG": "C.UTF-8", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true", "GIT_AUTHOR_NAME": "oncall-sre", "GIT_AUTHOR_EMAIL": f"oncall@{world.domain}",
            "GIT_COMMITTER_NAME": "oncall-sre", "GIT_COMMITTER_EMAIL": f"oncall@{world.domain}",
            "PYTHONDONTWRITEBYTECODE": "1", "SREGYM_WORLD": str(world.root),
        }
        try:
            proc = subprocess.run(rebuilt, shell=True, cwd=world.root, env=env, capture_output=True, text=True,
                                  timeout=TIMEOUT_S, errors="replace")
        except subprocess.TimeoutExpired:
            return ToolResult(f"$ {command}\n[timed out after {TIMEOUT_S}s]", is_error=True)
        out = proc.stdout
        if proc.stderr:
            out += ("\n" if out and not out.endswith("\n") else "") + proc.stderr
        out = out.rstrip("\n")
        header = f"$ {command}"
        if rebuilt != command and "sqlite3 -readonly" in rebuilt:
            header += "   (sqlite3 opened read-only)"
        body = out if out else "(no output)"
        footer = f"\n[exit code {proc.returncode}]" if proc.returncode != 0 else ""
        return ToolResult(f"{header}\n{body}{footer}", is_error=proc.returncode != 0)
