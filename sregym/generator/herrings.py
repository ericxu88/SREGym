"""Red herrings: seeded, realism-preserving distractors added to a world before history generation.

Principles: herrings only ADD plausible noise (never remove real evidence); each is harmless to the
service; each is template-agnostic; which ones apply is deterministic in the seed. Applied by
``scenario.prepare_world`` between ``World.build`` and ``template.inject`` (so git/deploy ordering
stays chronological), except page chatter which is rendered into the task prompt.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sregym import util
from sregym.generator.world import World

# (key, old-must-startwith, new-value, commit message, deploy restart mode)
_DECOY_ENV_CHANGES = [
    ("PAYMENT_GATEWAY_TIMEOUT_MS", "PAYMENT_GATEWAY_TIMEOUT_MS=", "PAYMENT_GATEWAY_TIMEOUT_MS=1800",
     "ops: nudge payment gateway timeout to 1800ms while the gateway team investigates latency (PAY-260)", "deferred"),
    ("LOG_LEVEL", "LOG_LEVEL=", "LOG_LEVEL=INFO",
     "chore(config): normalize LOG_LEVEL casing and comments in production .env", "deferred"),
    ("SESSION_SECRET", None, None,  # rotate: new random value
     "chore: rotate SESSION_SECRET (quarterly, SECOPS calendar)", "deferred"),
]

_DECOY_CRON_LINES = [
    "50    *  *   *   *    app   find {repo}/logs -name '*.log' -size +512M -print >> logs/cron.log 2>&1  # OPS-471 disk watch",
    "5     *  *   *   *    app   cd {repo} && sqlite3 {core_db} 'PRAGMA quick_check;' >> logs/cron.log 2>&1  # integrity spot-check (OPS-468)",
]

_CHATTER = [
    ("marketing", "fyi marketing's promo email went out around {t1} — could this just be load from that?"),
    ("decoy_deploy", "we did ship a small config tweak earlier ({decoy_msg_short}) but it shouldn't be related... right?"),
    ("secrets", "didn't secops rotate credentials recently? maybe something is still using the old ones"),
    ("gateway", "the payment gateway status page showed elevated latency earlier today, might be them again"),
    ("cache", "could be the CDN/cache layer — we saw weird cache behavior last month with the same symptoms"),
]


def apply_red_herrings(world: World, count: int, rng: random.Random) -> list[str]:
    """Mutate the (healthy, pre-inject) world with ``count`` distractors; returns their names."""
    if count <= 0:
        return []
    applied: list[str] = []
    pool = ["decoy_deploy", "decoy_cron", "bot_scan", "chatter"]
    rng.shuffle(pool)
    for name in pool[: max(0, min(count, len(pool)))]:
        {"decoy_deploy": _decoy_deploy, "decoy_cron": _decoy_cron, "bot_scan": _bot_scan, "chatter": _chatter}[name](world, rng)
        applied.append(name)
    world.extra["herrings"] = applied
    world.save()
    return applied


def _window_time(world: World, frac_lo: float, frac_hi: float, rng: random.Random):
    span = (world.now - world.history_start).total_seconds()
    return world.history_start + timedelta(seconds=span * rng.uniform(frac_lo, frac_hi))


def _decoy_deploy(world: World, rng: random.Random) -> None:
    """An innocent, recent config commit + deploy (restart deferred, so it cannot cause anything)."""
    key, prefix, new_line, message, restart = rng.choice(_DECOY_ENV_CHANGES)
    text = world.env_file.read_text()
    lines = text.splitlines()
    if key == "SESSION_SECRET":
        new_line = f"SESSION_SECRET={'%032x' % rng.getrandbits(128)}"
        prefix = "SESSION_SECRET="
    lines = [new_line if l.startswith(prefix) else l for l in lines]
    new_text = "\n".join(lines) + "\n"
    if new_text == text:  # value identical (e.g. LOG_LEVEL already INFO): make the diff a comment tidy
        new_text = text.replace("# --- limits", "# --- limits (per-user; see checkout/main.py)")
    when = _window_time(world, 0.35, 0.6, rng)
    author = rng.choice(world.team)
    sha = world.commit_files({".env": new_text}, message, author, when)
    world.commits.append({"sha": sha, "message": message, "when": util.fmt_iso(when), "author": author["name"]})
    deploys = world.extra.setdefault("herring_deploys", [])
    deploys.append({"when": util.fmt_iso(when + timedelta(minutes=rng.uniform(2, 6))), "sha": sha[:7],
                    "author": author["name"], "message": message, "config_only": True, "restart": restart})
    world.extra["herring_decoy_msg"] = message


def _decoy_cron(world: World, rng: random.Random) -> None:
    """A recently added (harmless) cron entry: fresh file mtime, suspicious-looking line."""
    cron = world.root / "etc" / "cron.d" / world.naming.service
    line = rng.choice(_DECOY_CRON_LINES).format(repo=world.repo, core_db=world.naming.core_db_rel)
    cron.write_text(cron.read_text().rstrip("\n") + "\n" + line + "\n")
    when = _window_time(world, 0.3, 0.7, rng)
    import os

    os.utime(cron, (when.timestamp(), when.timestamp()))


def _bot_scan(world: World, rng: random.Random) -> None:
    """A scraper hammering order ids: a burst of 404s (and nginx noise) from one IP near the incident window."""
    start = _window_time(world, 0.55, 0.85, rng)
    world.extra["extra_traffic"] = [{
        "kind": "bot_scan", "start": util.fmt_iso(start), "duration_s": int(rng.uniform(150, 420)),
        "rps": round(rng.uniform(1.5, 3.5), 2), "ip": f"45.155.{rng.randint(1, 254)}.{rng.randint(1, 254)}",
        "ua": rng.choice(["python-requests/2.32.3", "Go-http-client/2.0", "curl/8.6.0"]),
    }]


def _chatter(world: World, rng: random.Random) -> None:
    """Speculative teammate chatter rendered into the page: plausible wrong hypotheses."""
    picks = rng.sample(_CHATTER, k=2)
    t1 = (world.now - timedelta(minutes=rng.uniform(30, 90))).strftime("%H:%M")
    decoy = world.extra.get("herring_decoy_msg", "a config tweak")
    world.extra["herring_chatter"] = [
        text.format(t1=t1, decoy_msg_short=decoy[:48]) for _name, text in picks
    ]
