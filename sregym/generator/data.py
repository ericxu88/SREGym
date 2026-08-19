"""Seeded business data (users, products, orders, payments) and SQLite provisioning."""
from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from faker import Faker

_PRODUCT_NOUNS = [
    "Desk Lamp", "Yoga Mat", "Water Bottle", "Notebook", "Backpack", "Headphones", "Coffee Grinder",
    "Running Shoes", "Rain Jacket", "Wool Socks", "Phone Stand", "Tote Bag", "Ceramic Mug", "Wall Clock",
    "Throw Blanket", "Cutting Board", "Bike Light", "Sunglasses", "Hand Cream", "Candle Set", "Puzzle",
    "Sketchbook", "Umbrella", "Travel Pillow", "Kettle", "Plant Pot", "Scarf", "Board Game", "Doormat",
    "Storage Bin", "Bath Towel", "Wireless Charger", "Keyboard", "Mouse Pad", "Cookbook", "Tea Sampler",
]
_TIERS = ["standard"] * 7 + ["plus"] * 2 + ["vip"]
_METHODS = ["card"] * 7 + ["paypal"] * 2 + ["apple_pay"]


@dataclass
class BusinessData:
    company: str
    domain: str
    team: list[dict[str, str]]  # developers: name, email
    users: list[dict] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    order_items: list[dict] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)
    carts: list[dict] = field(default_factory=list)

    @property
    def user_ids(self) -> list[int]:
        return [u["id"] for u in self.users]

    @property
    def active_skus(self) -> list[str]:
        return [p["sku"] for p in self.products if p["active"]]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "acme"


def generate_business_data(seed: int, now: datetime, history_end: datetime) -> BusinessData:
    """Generate the pre-existing state of the business as of ``history_end``.

    Orders are created between ~90 days ago and ``history_end`` (the log generator
    creates the orders that appear in the recent log window itself so the two agree).
    """
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    company = fake.company()
    domain = _slug(company.split(",")[0].split(" and ")[0]) + "." + rng.choice(["com", "io", "co", "shop"])
    team = []
    for _ in range(3):
        name = fake.name()
        team.append({"name": name, "email": f"{_slug(name).replace('-', '.')}@{domain}"})

    data = BusinessData(company=company, domain=domain, team=team)

    n_users = rng.randint(320, 720)
    n_products = rng.randint(28, 46)
    n_orders = rng.randint(1800, 4200)
    start = now - timedelta(days=90)

    seen_emails: set[str] = set()
    for uid in range(1, n_users + 1):
        while True:
            email = fake.unique.email().lower()
            if email not in seen_emails:
                seen_emails.add(email)
                break
        created = start - timedelta(days=rng.uniform(0, 400))
        data.users.append({
            "id": uid, "email": email, "full_name": fake.name(), "country": fake.country_code(),
            "tier": rng.choice(_TIERS), "created_at": _iso(created),
        })

    prefixes = [fake.lexify("???").upper() for _ in range(3)]
    used_skus: set[str] = set()
    nouns = list(_PRODUCT_NOUNS)
    rng.shuffle(nouns)
    for pid in range(1, n_products + 1):
        while True:
            sku = f"{rng.choice(prefixes)}-{rng.randint(1000, 9999)}"
            if sku not in used_skus:
                used_skus.add(sku)
                break
        noun = nouns[(pid - 1) % len(nouns)]
        name = f"{fake.color_name()} {noun}"
        data.products.append({
            "id": pid, "sku": sku, "name": name, "price_cents": rng.choice([499, 899, 1299, 1999, 2499, 3499, 4999, 7999, 12999]),
            "active": 0 if rng.random() < 0.08 else 1,
        })
    active_products = [p for p in data.products if p["active"]]

    span = (history_end - start).total_seconds()
    order_times = sorted(start + timedelta(seconds=rng.uniform(0, span)) for _ in range(n_orders))
    payment_id = 0
    for oid, created in enumerate(order_times, start=1):
        user = rng.choice(data.users)
        n_items = rng.choice([1, 1, 1, 2, 2, 3])
        items = rng.sample(active_products, k=min(n_items, len(active_products)))
        total = 0
        for p in items:
            qty = rng.choice([1, 1, 1, 2, 3])
            total += p["price_cents"] * qty
            data.order_items.append({"order_id": oid, "product_id": p["id"], "quantity": qty, "unit_price_cents": p["price_cents"]})
        age_days = (now - created).days
        r = rng.random()
        if r < 0.03:
            status = "refunded"
        elif r < 0.05:
            status = "cancelled"
        elif age_days > 7:
            status = "delivered"
        elif age_days > 2:
            status = "shipped"
        else:
            status = "confirmed"
        updated = created + timedelta(hours=rng.uniform(0, 72)) if status != "confirmed" else created
        data.orders.append({
            "id": oid, "user_id": user["id"], "status": status, "total_cents": total, "currency": "USD",
            "created_at": _iso(created), "updated_at": _iso(min(updated, history_end)),
        })
        if status != "cancelled":
            payment_id += 1
            data.payments.append({
                "id": payment_id, "order_id": oid, "user_id": user["id"], "amount_cents": total, "currency": "USD",
                "method": rng.choice(_METHODS), "status": "refunded" if status == "refunded" else "captured",
                "gateway_ref": "ch_" + fake.hexify("^" * 16), "created_at": _iso(created),
            })

    for cid in range(1, rng.randint(12, 40)):
        created = history_end - timedelta(minutes=rng.uniform(5, 600))
        expires = created + timedelta(minutes=45)
        if expires > history_end - timedelta(minutes=60):
            # still active at generation time; keep it alive well past any episode so the (now real) cron job
            # that expires carts does not modify generation-time rows during verification
            status = "active"
            expires = now + timedelta(hours=rng.uniform(6, 24))
        else:
            status = rng.choice(["expired", "expired", "converted"])
        data.carts.append({"id": cid, "user_id": rng.choice(data.users)["id"], "status": status,
                           "created_at": _iso(created), "expires_at": _iso(expires)})
    return data


def create_databases(data: BusinessData, core_path: Path, ledger_path: Path, migrations_dir: Path) -> None:
    core_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (core_path, ledger_path):
        if p.exists():
            p.unlink()
    core = sqlite3.connect(core_path)
    core.executescript((migrations_dir / "001_init.sql").read_text())
    core.executemany("INSERT INTO users (id, email, full_name, country, tier, created_at) VALUES (:id, :email, :full_name, :country, :tier, :created_at)", data.users)
    core.executemany("INSERT INTO products (id, sku, name, price_cents, active) VALUES (:id, :sku, :name, :price_cents, :active)", data.products)
    core.executemany("INSERT INTO orders (id, user_id, status, total_cents, currency, created_at, updated_at) VALUES (:id, :user_id, :status, :total_cents, :currency, :created_at, :updated_at)", data.orders)
    core.executemany("INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES (:order_id, :product_id, :quantity, :unit_price_cents)", data.order_items)
    core.executemany("INSERT INTO carts (id, user_id, status, created_at, expires_at) VALUES (:id, :user_id, :status, :created_at, :expires_at)", data.carts)
    core.commit()
    core.close()

    ledger = sqlite3.connect(ledger_path)
    ledger.executescript((migrations_dir / "002_ledger.sql").read_text())
    ledger.executemany("INSERT INTO payments (id, order_id, user_id, amount_cents, currency, method, status, gateway_ref, created_at) VALUES (:id, :order_id, :user_id, :amount_cents, :currency, :method, :status, :gateway_ref, :created_at)", data.payments)
    ledger.commit()
    ledger.close()


def db_table_snapshot(db_path: Path) -> dict[str, dict]:
    """Per-table columns, row count, max rowid and a hash of all current rows (over the generation-time
    columns, so an additive ``ALTER TABLE ADD COLUMN`` later does not read as "rows modified") -- plus the
    structured schema (tables/columns/indexes) -- for the manifest."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        indexes = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        out: dict[str, dict] = {"__schema__": {"tables": {}, "indexes": indexes}}
        for t in tables:
            columns = [(r[1], (r[2] or "").upper()) for r in conn.execute(f'PRAGMA table_info("{t}")')]
            out["__schema__"]["tables"][t] = {"columns": columns}
            count, max_rowid = conn.execute(f'SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM "{t}"').fetchone()
            names = [c[0] for c in columns]
            out[t] = {"count": count, "max_rowid": max_rowid, "columns": names, "hash": db_rows_hash(conn, t, max_rowid, names)}
        return out
    finally:
        conn.close()


def db_rows_hash(conn: sqlite3.Connection, table: str, max_rowid: int, columns: list[str] | None = None) -> str:
    import hashlib

    cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
    h = hashlib.sha256()
    for row in conn.execute(f'SELECT rowid, {cols} FROM "{table}" WHERE rowid <= ? ORDER BY rowid', (max_rowid,)):
        h.update(repr(tuple(row)).encode())
        h.update(b"\n")
    return h.hexdigest()
