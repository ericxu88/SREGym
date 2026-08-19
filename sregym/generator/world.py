"""World generation: seed -> a small, complete production stack on local disk.

Layout of a world root (acts as the host's filesystem):

    <root>/
      checkout-service/           git repo of the app; the service's working directory
        .env                      production config (tracked in git, shipped by "deploy-bot")
        checkout/*.py             FastAPI app
        migrations/*.sql, scripts/expire_carts.py, README.md, requirements.txt
        data/checkout.db, data/ledger.db      (gitignored)
        logs/app.log, logs/deploy.log, logs/cron.log
        run/checkout-service.pid
      etc/nginx/sites-enabled/checkout-service.conf
      etc/systemd/system/checkout-service.service
      etc/cron.d/checkout-service
      var/log/nginx/access.log, error.log
      metrics/series.jsonl        metrics store read by the query_metrics tool
      .sregym/                    control plane (world.json, manifest.json, spec.json) - hidden from the agent
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sregym import util
from sregym.generator import app_source
from sregym.generator.data import BusinessData, create_databases, db_table_snapshot, generate_business_data

SERVICE_NAME = "checkout-service"
CONTROL_DIR = ".sregym"
CORE_DB = "data/checkout.db"
LEDGER_DB = "data/ledger.db"

# directories never hashed into the file manifest / state hash
MANIFEST_EXCLUDE_DIRS = {CONTROL_DIR, ".git", "__pycache__", "logs", "var", "metrics", "run", "data"}

_GIT_ENV_BASE = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


@dataclass
class World:
    seed: int
    root: Path
    now: datetime  # generation time (UTC); "the present" of the incident
    history_start: datetime
    port: int
    company: str
    domain: str
    team: list[dict[str, str]]
    base_env: dict[str, str]  # the CORRECT production configuration
    python: str
    commits: list[dict[str, Any]] = field(default_factory=list)  # oldest first: sha, message, when, author
    sample_user_ids: list[int] = field(default_factory=list)
    skus: list[str] = field(default_factory=list)
    max_order_id: int = 0
    fault: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # free-form, e.g. incident summary for display
    data: BusinessData | None = None  # only populated at build time (not persisted)

    # ------------------------------------------------------------------ paths
    @property
    def repo(self) -> Path:
        return self.root / SERVICE_NAME

    @property
    def control_dir(self) -> Path:
        return self.root / CONTROL_DIR

    @property
    def env_file(self) -> Path:
        return self.repo / ".env"

    @property
    def core_db(self) -> Path:
        return self.repo / CORE_DB

    @property
    def ledger_db(self) -> Path:
        return self.repo / LEDGER_DB

    @property
    def log_dir(self) -> Path:
        return self.repo / "logs"

    @property
    def app_log(self) -> Path:
        return self.log_dir / "app.log"

    @property
    def metrics_file(self) -> Path:
        return self.root / "metrics" / "series.jsonl"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid_file(self) -> Path:
        return self.repo / "run" / f"{SERVICE_NAME}.pid"

    def db_paths(self) -> dict[str, Path]:
        return {"core": self.core_db, "ledger": self.ledger_db}

    # ------------------------------------------------------------------ build
    @classmethod
    def build(cls, seed: int, root: Path | None = None, now: datetime | None = None,
              history_minutes: int = 180) -> "World":
        """Create the healthy stack (no fault, no historical logs yet)."""
        now = (now or util.utcnow()).astimezone(timezone.utc).replace(microsecond=0)
        if root is None:
            root = Path(tempfile.mkdtemp(prefix=f"sregym-{seed}-"))
        root = Path(root).resolve()
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"world root {root} is not empty; pick a fresh directory")
        root.mkdir(parents=True, exist_ok=True)
        rng = random.Random(seed ^ 0xA5A5)
        history_start = now - timedelta(minutes=history_minutes)
        data = generate_business_data(seed, now=now, history_end=history_start)
        port = util.free_port()

        base_env = {
            "APP_NAME": SERVICE_NAME,
            "APP_ENV": "production",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "DATABASE_URL": f"sqlite:///{CORE_DB}",
            "LEDGER_DATABASE_URL": f"sqlite:///{LEDGER_DB}",
            "DATABASE_TIMEOUT_SECONDS": "5",
            "PAYMENT_GATEWAY_URL": f"https://payments-gw.internal.{data.domain}/v2",
            "PAYMENT_GATEWAY_TIMEOUT_MS": "1500",
            "PAYMENT_GATEWAY_MODE": "stub",
            "CART_TTL_MINUTES": "45",
            "LOG_PATH": "logs/app.log",
            "LOG_LEVEL": "INFO",
            "RATE_LIMIT_PER_MINUTE": "600",
            "SESSION_SECRET": "%032x" % rng.getrandbits(128),
        }
        world = cls(
            seed=seed, root=root, now=now, history_start=history_start, port=port,
            company=data.company, domain=data.domain, team=data.team, base_env=base_env,
            python=sys.executable, data=data,
            sample_user_ids=sorted(rng.sample(data.user_ids, k=min(60, len(data.user_ids)))),
            skus=data.active_skus, max_order_id=len(data.orders),
        )
        world._build_repo(rng, old_secret="%032x" % rng.getrandbits(128))
        create_databases(data, world.core_db, world.ledger_db, world.repo / "migrations")
        world._write_system_files()
        (world.repo / "logs").mkdir(exist_ok=True)
        (world.repo / "run").mkdir(exist_ok=True)
        (world.root / "var" / "log" / "nginx").mkdir(parents=True, exist_ok=True)
        (world.root / "metrics").mkdir(exist_ok=True)
        world.control_dir.mkdir(exist_ok=True)
        world.save()
        return world

    def template_values(self) -> dict[str, str]:
        return {
            "COMPANY": self.company, "DOMAIN": self.domain, "PORT": str(self.port),
            "REPO": str(self.repo), "PYTHON": self.python,
        }

    def _build_repo(self, rng: random.Random, old_secret: str) -> None:
        repo = self.repo
        repo.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        revisions = app_source.plan_revisions(now=self.history_start, base_env=self.base_env, old_secret=old_secret, rng=rng)
        for rev in revisions:
            values = dict(self.template_values(), VERSION=rev.version)
            files = app_source.render_app_files(rev.sections, values)
            files[".env"] = app_source.render_env(rev.env)
            files.update(rev.extra_files)
            author = self.team[rev.author_index % len(self.team)]
            sha = self.commit_files(files, rev.message, author, rev.when)
            self.commits.append({"sha": sha, "message": rev.message.splitlines()[0], "when": util.fmt_iso(rev.when),
                                 "author": author["name"]})

    def commit_files(self, files: dict[str, str], message: str, author: dict[str, str], when: datetime) -> str:
        for rel, content in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        self.git("add", "-A")
        stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
               "GIT_AUTHOR_NAME": author["name"], "GIT_AUTHOR_EMAIL": author["email"],
               "GIT_COMMITTER_NAME": author["name"], "GIT_COMMITTER_EMAIL": author["email"]}
        self.git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message, env_extra=env)
        return self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str, env_extra: dict[str, str] | None = None, check: bool = True) -> str:
        env = dict(os.environ)
        env.update(_GIT_ENV_BASE)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(["git", *args], cwd=self.repo, env=env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _write_system_files(self) -> None:
        values = self.template_values()
        targets = {
            "nginx.conf": self.root / "etc" / "nginx" / "sites-enabled" / f"{SERVICE_NAME}.conf",
            "checkout-service.service": self.root / "etc" / "systemd" / "system" / f"{SERVICE_NAME}.service",
            "cron.d": self.root / "etc" / "cron.d" / SERVICE_NAME,
        }
        for name, dest in targets.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(app_source.render_system_file(name, values))

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed, "root": str(self.root), "now": util.fmt_iso(self.now),
            "history_start": util.fmt_iso(self.history_start), "port": self.port,
            "company": self.company, "domain": self.domain, "team": self.team, "base_env": self.base_env,
            "python": self.python, "commits": self.commits, "sample_user_ids": self.sample_user_ids,
            "skus": self.skus, "max_order_id": self.max_order_id, "fault": self.fault, "extra": self.extra,
        }

    def save(self) -> None:
        util.write_json(self.control_dir / "world.json", self.to_dict())

    @classmethod
    def load(cls, root: Path) -> "World":
        root = Path(root).resolve()
        d = util.read_json(root / CONTROL_DIR / "world.json")
        return cls(
            seed=d["seed"], root=root, now=util.parse_iso(d["now"]), history_start=util.parse_iso(d["history_start"]),
            port=d["port"], company=d["company"], domain=d["domain"], team=d["team"], base_env=d["base_env"],
            python=d["python"], commits=d["commits"], sample_user_ids=d["sample_user_ids"], skus=d["skus"],
            max_order_id=d["max_order_id"], fault=d.get("fault"), extra=d.get("extra", {}),
        )

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ manifest / state
    def file_hashes(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in util.iter_files(self.root, exclude_dirs=MANIFEST_EXCLUDE_DIRS):
            rel = util.relpath(p, self.root)
            out[rel] = util.sha256_file(p)
        return out

    def log_files(self) -> list[Path]:
        files: list[Path] = []
        for d in (self.log_dir, self.root / "var" / "log"):
            if d.exists():
                files.extend(p for p in util.iter_files(d) if p.suffix == ".log")
        return sorted(files)

    def snapshot_manifest(self) -> dict[str, Any]:
        """Generation-time record of everything that must survive the episode."""
        manifest = {
            "files": self.file_hashes(),
            "dbs": {util.relpath(p, self.root): db_table_snapshot(p) for p in self.db_paths().values()},
            "logs": {},
            "git": {"head": self.git("rev-parse", "HEAD").strip(), "commits": [c["sha"] for c in self.commits]},
        }
        for p in self.log_files():
            manifest["logs"][util.relpath(p, self.root)] = {
                "lines": util.count_lines(p), "size": p.stat().st_size, "head_hash": util.head_hash(p),
            }
        util.write_json(self.control_dir / "manifest.json", manifest)
        return manifest

    def load_manifest(self) -> dict[str, Any]:
        return util.read_json(self.control_dir / "manifest.json")

    def state_hash(self, service_pid: int | None = None) -> str:
        """Hash of the agent-controllable state: tracked files, DB contents, service identity."""
        parts: dict[str, Any] = {"files": self.file_hashes(), "pid": service_pid}
        for name, p in self.db_paths().items():
            parts[f"db:{name}"] = db_table_snapshot(p) if p.exists() else None
        return "sha256:" + util.sha256_json(parts)
