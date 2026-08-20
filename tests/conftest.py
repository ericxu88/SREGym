from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sregym.faults.base import VerificationSpec
from sregym.generator.world import World
from sregym.scenario import prepare_world
from sregym.runtime.services import ServiceManager

HISTORY_MINUTES = 40  # keep generation fast in tests
FIXED_NOW = datetime(2026, 8, 18, 14, 40, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _classic_stack(monkeypatch):
    """Pin the seeded stack pick to the classic checkout-service identity: the suite's
    assertions encode classic names. Variation coverage lives in test_stack_variation.py,
    which requests variants explicitly (resolve() by name bypasses the pick)."""
    from sregym.generator import naming, world as world_mod

    monkeypatch.setattr(naming, "pick", lambda seed: naming.CLASSIC)
    monkeypatch.setattr(world_mod, "pick_naming", lambda seed: naming.CLASSIC)


@pytest.fixture
def faulted(tmp_path: Path) -> tuple[World, VerificationSpec]:
    world, spec = prepare_world(seed=7, root=tmp_path / "world", history_minutes=HISTORY_MINUTES)
    yield world, spec
    world.destroy()


@pytest.fixture
def running(faulted):
    """A faulted world with the (broken) service running; yields (world, spec, service_manager)."""
    world, spec = faulted
    sm = ServiceManager(world)
    msg = sm.start(announce=False)
    assert "listening" in msg, msg
    try:
        yield world, spec, sm
    finally:
        sm.close()
