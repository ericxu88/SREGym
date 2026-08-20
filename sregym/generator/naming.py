"""Per-seed stack identity: what the service, package, databases and routes are called.

Un-memorizability lever: every world draws one of several coherent stack identities, so
"the checkout-service playbook" (grep logs/app.log, cat checkout/config.py, sqlite3
data/checkout.db, cat etc/cron.d/checkout-service) changes from episode to episode while
the business domain (an e-commerce order/checkout flow) and difficulty stay fixed.

Each variant is internally consistent: repo directory, systemd unit, nginx conf and
cron.d file share the service name; the python package names the loggers and traceback
frames; DATABASE_URL points at the variant's db file; API routes carry the variant's
prefix. Table/column names are deliberately NOT varied (little memorization value,
large verifier surface).
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StackNaming:
    service: str          # repo dir, systemd unit, nginx conf, cron.d file, APP_NAME
    package: str          # python package -> import name, logger prefix, traceback frames
    core_db: str          # filename under data/
    ledger_db: str        # filename under data/
    route_prefix: str     # "" | "/api" | "/v1" -- applied to business routes, not /health //metrics
    checkout_segment: str  # path word for the checkout POST ("checkout", "purchase")

    # ------------------------------------------------------------------ derived
    @property
    def core_db_rel(self) -> str:
        return f"data/{self.core_db}"

    @property
    def ledger_db_rel(self) -> str:
        return f"data/{self.ledger_db}"

    @property
    def upstream(self) -> str:
        return self.service.replace("-", "_")

    @property
    def checkout_route(self) -> str:
        return f"{self.route_prefix}/{self.checkout_segment}"

    def route(self, template: str) -> str:
        """Map a canonical endpoint template (or a concrete path with ids/query string)
        to this stack's concrete route.

        Canonical templates: /checkout, /orders, /orders/{order_id}, /users,
        /users/{user_id}; infra endpoints (/health, /metrics, /) never vary.
        """
        if template == "/" or template.startswith(("/health", "/metrics")):
            return template
        if template == "/checkout" or template.startswith(("/checkout/", "/checkout?")):
            return self.checkout_route + template[len("/checkout"):]
        return self.route_prefix + template

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "StackNaming":
        return cls(**d)


# The classic identity first: it is byte-for-byte the pre-variation stack.
VARIANTS: list[StackNaming] = [
    StackNaming("checkout-service", "checkout", "checkout.db", "ledger.db", "", "checkout"),
    StackNaming("checkout-api", "checkout", "checkout.db", "ledger.db", "/api", "checkout"),
    StackNaming("storefront-api", "storefront", "storefront.db", "payments.db", "", "checkout"),
    StackNaming("order-service", "orders", "orders.db", "ledger.db", "", "checkout"),
    StackNaming("shop-backend", "shop", "shop.db", "payments.db", "/api", "checkout"),
    StackNaming("commerce-gateway", "commerce", "commerce.db", "ledger.db", "/v1", "purchase"),
    StackNaming("webstore-api", "webstore", "store.db", "payments.db", "", "purchase"),
    StackNaming("purchase-service", "purchases", "purchases.db", "ledger.db", "", "purchase"),
]

CLASSIC = VARIANTS[0]
_BY_SERVICE = {v.service: v for v in VARIANTS}


def pick(seed: int) -> StackNaming:
    """Seeded, uniform choice of stack identity (independent of other world randomness)."""
    return random.Random(seed ^ 0x57AC4).choice(VARIANTS)


def resolve(stack: str | None, seed: int) -> StackNaming:
    """CLI-facing: 'auto'/None -> seeded pick, 'classic' -> the original identity,
    otherwise a variant's service name."""
    if stack in (None, "auto"):
        return pick(seed)
    if stack == "classic":
        return CLASSIC
    if stack in _BY_SERVICE:
        return _BY_SERVICE[stack]
    raise ValueError(f"unknown stack {stack!r}; use auto, classic or one of: {', '.join(_BY_SERVICE)}")
