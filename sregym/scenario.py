"""Scenario preparation: healthy world -> red herrings -> injected fault -> evidence -> manifest.

Difficulty profiles bundle the realism-preserving knobs (step budget, red-herring count).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sregym.faults.base import VerificationSpec, get_fault
from sregym.generator.herrings import apply_red_herrings
from sregym.generator.logs import generate_history
from sregym.generator.naming import resolve as resolve_stack
from sregym.generator.world import World


@dataclass(frozen=True)
class DifficultyProfile:
    name: str
    max_steps: int
    red_herrings: int


PROFILES = {
    "baseline": DifficultyProfile("baseline", max_steps=30, red_herrings=0),
    "standard": DifficultyProfile("standard", max_steps=20, red_herrings=2),
    "hard": DifficultyProfile("hard", max_steps=12, red_herrings=4),
}


def prepare_world(seed: int, fault: str = "env_var_typo", root: Path | None = None, now: datetime | None = None,
                  history_minutes: int = 180, difficulty: str = "baseline",
                  stack: str = "auto") -> tuple[World, VerificationSpec]:
    """Build the stack, add the profile's red herrings, inject the fault, write the evidence, freeze the manifest."""
    profile = PROFILES[difficulty]
    world = World.build(seed, root=root, now=now, history_minutes=history_minutes, naming=resolve_stack(stack, seed))
    world.extra["difficulty"] = profile.name
    apply_red_herrings(world, profile.red_herrings, random.Random((seed * 2_654_435_761) ^ 0x4E44))
    template = get_fault(fault)
    spec = template.inject(world, seed)
    world.extra.setdefault("fault_params", {})["stack"] = world.naming.service
    generate_history(world, spec.incident)
    template.finalize(world, spec)
    spec.save(world)
    world.snapshot_manifest()
    world.save()
    return world, spec
