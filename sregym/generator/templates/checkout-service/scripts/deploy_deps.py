#!/usr/bin/env python
"""Install the internal packages pinned in requirements.txt from the local wheelhouse into lib/.

deploy-bot runs this on every code deploy; run it manually after changing a pin:

    python scripts/deploy_deps.py            # install/refresh lib/ to match requirements.txt
    python scripts/deploy_deps.py --check    # report pinned vs installed without changing anything

Only packages present in vendor/wheels/<name>-<version>/ are managed here; public packages
(fastapi, uvicorn, ...) come from the platform runtime image. Run from the repo root.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WHEELHOUSE = REPO / "vendor" / "wheels"
LIB = REPO / "lib"


def pinned() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (REPO / "requirements.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.]+)\s*$", line.strip())
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def installed_version(name: str) -> str | None:
    init = LIB / name / "__init__.py"
    if not init.exists():
        return None
    m = re.search(r'__version__ = "([^"]+)"', init.read_text())
    return m.group(1) if m else "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only; do not change lib/")
    args = ap.parse_args()
    managed = {p.name.rsplit("-", 1)[0] for p in WHEELHOUSE.iterdir() if p.is_dir()}
    changed = 0
    rc = 0
    for name, version in sorted(pinned().items()):
        if name not in managed:
            print(f"deploy_deps: {name}=={version:<12} provided by platform runtime (not managed)")
            continue
        wheel = WHEELHOUSE / f"{name}-{version}" / name
        current = installed_version(name)
        state = "installed" if current == version else (f"INSTALLED {current}, pinned {version}" if current else "NOT INSTALLED")
        if not wheel.exists():
            print(f"deploy_deps: {name}=={version:<12} ERROR: not in wheelhouse "
                  f"(available: {', '.join(sorted(p.name for p in WHEELHOUSE.iterdir() if p.name.startswith(name + '-')))})")
            rc = 1
            continue
        print(f"deploy_deps: {name}=={version:<12} {state}")
        if args.check or current == version:
            continue
        dest = LIB / name
        if dest.exists():
            shutil.rmtree(dest)
        LIB.mkdir(exist_ok=True)
        shutil.copytree(wheel, dest)
        print(f"deploy_deps: installed {name}-{version} into lib/{name}")
        changed += 1
    if args.check and any(installed_version(n) != v for n, v in pinned().items() if n in managed):
        rc = rc or 2
    print(f"deploy_deps: done ({changed} package(s) changed)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
