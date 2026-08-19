"""Step 1: the generated stack runs healthy with no fault; generation is deterministic."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sregym import util
from sregym.generator.logs import generate_history
from sregym.generator.world import World
from sregym.runtime.services import ServiceManager
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


def test_healthy_stack_serves_all_endpoints(tmp_path: Path):
    world = World.build(seed=3, root=tmp_path / "w", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    generate_history(world, incident=None)
    assert len(world.commits) == 8
    assert world.git("status", "--short").strip() == ""  # data/, logs/, run/ are gitignored
    env = util.parse_env_file(world.env_file.read_text())
    assert env["DATABASE_URL"] == "sqlite:///data/checkout.db"
    assert env["LEDGER_DATABASE_URL"] == "sqlite:///data/ledger.db"
    sm = ServiceManager(world)
    try:
        assert "listening" in sm.start()
        status, body = util.http_request("GET", world.base_url + "/health")
        assert status == 200 and '"status":"ok"' in body.replace(" ", "")
        for path in ("/users?limit=3", f"/users/{world.sample_user_ids[0]}", "/orders?limit=3", "/orders/1", "/metrics", "/"):
            status, _ = util.http_request("GET", world.base_url + path)
            assert status == 200, path
        status, body = util.http_request("POST", world.base_url + "/checkout",
                                         {"user_id": world.sample_user_ids[0], "items": [{"sku": world.skus[0], "quantity": 2}]})
        assert status == 201 and '"order_id"' in body
        status, _ = util.http_request("GET", world.base_url + "/orders/999999999")
        assert status == 404
        # the live app logs in the same format as the generated history
        tail = world.app_log.read_text().splitlines()[-1]
        assert " checkout.access req=" in tail and " POST /checkout 201 " in world.app_log.read_text()
    finally:
        sm.close()
    world.destroy()


def test_history_is_consistent_with_database(tmp_path: Path):
    world = World.build(seed=11, root=tmp_path / "w", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    stats = generate_history(world, incident=None)
    assert stats["errors"] == 0 and stats["requests"] > 500
    log = world.app_log.read_text()
    assert " 500 " not in log and "Traceback" not in log
    # every successful checkout in the log window exists as an order in the db
    order_ids = [int(m) for m in __import__("re").findall(r"POST /checkout 201 \d+ms user=\d+ order=(\d+)", log)]
    assert order_ids
    conn = sqlite3.connect(world.core_db)
    max_id = conn.execute("SELECT MAX(id) FROM orders").fetchone()[0]
    found = conn.execute(f"SELECT COUNT(*) FROM orders WHERE id IN ({','.join(map(str, order_ids))})").fetchone()[0]
    conn.close()
    assert found == len(order_ids) and max_id == max(order_ids) == world.max_order_id
    assert world.metrics_file.exists() and (world.root / "var/log/nginx/access.log").stat().st_size > 0
    world.destroy()


def test_generation_is_deterministic(tmp_path: Path):
    a = World.build(seed=99, root=tmp_path / "a", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    b = World.build(seed=99, root=tmp_path / "b", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    assert a.company == b.company and a.team == b.team
    assert [c["message"] for c in a.commits] == [c["message"] for c in b.commits]
    assert a.env_file.read_text().replace(str(a.port), "PORT") == b.env_file.read_text().replace(str(b.port), "PORT")
    ha = sqlite3.connect(a.core_db).execute("SELECT COUNT(*), SUM(total_cents) FROM orders").fetchone()
    hb = sqlite3.connect(b.core_db).execute("SELECT COUNT(*), SUM(total_cents) FROM orders").fetchone()
    assert ha == hb
    c = World.build(seed=100, root=tmp_path / "c", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    assert c.company != a.company
    for w in (a, b, c):
        w.destroy()
