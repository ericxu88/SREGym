"""Stack variation: per-seed service/package/db/route identities (un-memorizability)."""
from __future__ import annotations

from pathlib import Path

from sregym.generator.naming import CLASSIC, VARIANTS, pick, resolve
from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.scenario import prepare_world

from tests.conftest import FIXED_NOW, HISTORY_MINUTES


def test_variants_coherent():
    services = [v.service for v in VARIANTS]
    assert len(set(services)) == len(services)
    for v in VARIANTS:
        assert v.package.isidentifier()
        assert v.core_db.endswith(".db") and v.ledger_db.endswith(".db")
        assert v.route_prefix in ("", "/api", "/v1")
        # infra endpoints never move
        assert v.route("/health") == "/health" and v.route("/metrics") == "/metrics"
        assert v.route("/checkout") == v.checkout_route
        assert v.route("/orders/{order_id}") == f"{v.route_prefix}/orders/{{order_id}}"
        assert v.route("/users?limit=20") == f"{v.route_prefix}/users?limit=20"
        assert v.route("/checkout?x=1") == f"{v.checkout_route}?x=1"
    # the classic identity is the original stack, byte for byte
    assert CLASSIC.service == "checkout-service" and CLASSIC.package == "checkout"
    assert CLASSIC.route("/checkout") == "/checkout" and CLASSIC.core_db_rel == "data/checkout.db"


def test_seeded_pick_deterministic_and_diverse():
    # `pick` here is the real function (bound at module import, before the autouse classic pin)
    assert pick(123) == pick(123)
    seen = {pick(s).service for s in range(1, 41)}
    assert len(seen) >= 4, seen
    assert resolve("classic", 1) is CLASSIC
    assert resolve("storefront-api", 1).package == "storefront"
    assert resolve("auto", 7) is CLASSIC  # the autouse fixture pins the module-level pick


def test_varied_world_builds_consistently(tmp_path: Path):
    world, spec = prepare_world(seed=11, fault="env_var_typo", root=tmp_path / "w", now=FIXED_NOW,
                                history_minutes=HISTORY_MINUTES, stack="storefront-api")
    nm = world.naming
    assert nm.service == "storefront-api" and nm.package == "storefront"
    assert (world.repo / "storefront" / "main.py").exists()
    assert world.core_db == world.repo / "data" / "storefront.db" and world.core_db.exists()
    assert (world.root / "etc" / "cron.d" / "storefront-api").exists()
    assert (world.root / "etc" / "systemd" / "system" / "storefront-api.service").exists()
    env = world.env_file.read_text()
    assert "APP_NAME=storefront-api" in env and "sqlite:///data/storefront.db" in env
    log = world.app_log.read_text()
    assert "storefront.access" in log and "checkout.access" not in log
    assert "storefront-api" in world.git("log", "--oneline")
    # nothing anywhere in the host still carries the classic identity or a template placeholder
    for f in world.root.rglob("*"):
        if not f.is_file():
            continue
        text = f.read_text(errors="ignore")
        assert "__SREGYM_" not in text, f
        assert "checkout-service" not in text, f
    world.destroy()


def test_scripted_episode_on_varied_stack(tmp_path: Path):
    """Live end-to-end on a prefixed + renamed stack: service boots under the new package,
    traffic and verifier probes hit /v1 routes, the scripted solver fixes the fault."""
    res = run_episode(ScriptedAgent("solve"),
                      EpisodeConfig(seed=13, fault="env_var_typo", out_dir=tmp_path / "run", now=FIXED_NOW,
                                    history_minutes=HISTORY_MINUTES, stack="commerce-gateway", max_steps=30))
    assert res.reward == 1.0 and res.success, res.verification
    assert res.fault_params.get("stack") == "commerce-gateway"


def test_classic_stack_unchanged(tmp_path: Path):
    """Guard: template parameterization must render the classic identity exactly as before."""
    world, spec = prepare_world(seed=7, fault="env_var_typo", root=tmp_path / "w", now=FIXED_NOW,
                                history_minutes=HISTORY_MINUTES, stack="classic")
    main = (world.repo / "checkout" / "main.py").read_text()
    assert '@app.post("/checkout", status_code=201)' in main
    assert '@app.get("/orders")' in main and '@app.get("/users/{user_id}")' in main
    assert 'logging.getLogger("checkout.access")' in main
    env = world.env_file.read_text()
    assert env.startswith("# checkout-service -- production configuration")
    nginx = (world.root / "etc/nginx/sites-enabled/checkout-service.conf").read_text()
    assert "upstream checkout_service {" in nginx
    world.destroy()
