"""Scenario preparation: healthy world -> injected fault -> historical evidence -> frozen manifest.

Shared by the episode harness, the CLI (``generate``/``run``) and the test-suite fixtures.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sregym.faults.base import VerificationSpec, get_fault
from sregym.generator.logs import generate_history
from sregym.generator.world import World


def prepare_world(seed: int, fault: str = "env_var_typo", root: Path | None = None, now: datetime | None = None,
                  history_minutes: int = 180) -> tuple[World, VerificationSpec]:
    """Build the stack, inject the fault, write the evidence trail, freeze the manifest."""
    world = World.build(seed, root=root, now=now, history_minutes=history_minutes)
    template = get_fault(fault)
    spec = template.inject(world, seed)
    generate_history(world, spec.incident)
    template.finalize(world, spec)
    spec.save(world)
    world.snapshot_manifest()
    world.save()
    return world, spec
