"""Background synthetic traffic against the live service (keeps logs/metrics moving)."""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone

from sregym import util
from sregym.generator import traffic_profile as tp
from sregym.generator.world import World


class TrafficGenerator(threading.Thread):
    def __init__(self, world: World, rps: float = 1.5, seed: int | None = None):
        super().__init__(name="sregym-traffic", daemon=True)
        self.world = world
        self.rps = rps
        self.rng = random.Random((seed if seed is not None else world.seed) ^ 0x7A11)
        self._stop = threading.Event()
        self.sent = 0
        self.failed = 0
        self._nginx = world.root / "var" / "log" / "nginx" / "access.log"
        self._last_health = 0.0
        self._last_metrics = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - self._last_health >= tp.HEALTH_INTERVAL_S:
                    self._last_health = now
                    self._request("GET", "/health", None, "ELB-HealthChecker/2.0", log_nginx=False)
                if now - self._last_metrics >= tp.METRICS_INTERVAL_S:
                    self._last_metrics = now
                    self._request("GET", "/metrics", None, "Prometheus/2.51.0", ip="10.0.4.12")
                self._one()
            except Exception:  # noqa: BLE001 - never die
                pass
            self._stop.wait(max(0.05, self.rng.expovariate(self.rps)))

    def _one(self) -> None:
        rng = self.rng
        method, template = tp.pick_endpoint(rng)
        w = self.world
        body = None
        if template == "/checkout":
            path = "/checkout"
            body = {"user_id": rng.choice(w.sample_user_ids),
                    "items": [{"sku": s, "quantity": rng.choice([1, 1, 2])} for s in rng.sample(w.skus, k=min(len(w.skus), rng.choice([1, 1, 2])))],
                    "payment_method": rng.choice(["card"] * 7 + ["paypal"] * 2 + ["apple_pay"])}
            if rng.random() < tp.BURST_PROB:  # double-click / client retry: same user, right away
                self._webhook_after(self._request(method, path, body, rng.choice(tp.USER_AGENTS)))
                for _ in range(rng.randint(*tp.BURST_EXTRA)):
                    time.sleep(rng.uniform(0.3, 1.2))
                    self._webhook_after(self._request(method, path, body, rng.choice(tp.USER_AGENTS)))
                return
        elif template == "/orders/{order_id}":
            path = f"/orders/{rng.randint(max(1, w.max_order_id - 3000), w.max_order_id + 20)}"
        elif template == "/orders":
            path = f"/orders?user_id={rng.choice(w.sample_user_ids)}&limit=10"
        elif template == "/users/{user_id}":
            path = f"/users/{rng.choice(w.sample_user_ids)}"
        else:
            path = "/users?limit=20"
        result = self._request(method, path, body, rng.choice(tp.USER_AGENTS))
        if template == "/checkout":
            self._webhook_after(result)

    def _webhook_after(self, result: tuple[int, str]) -> None:
        """After a successful capture the gateway pushes a signed settlement confirmation.
        It signs with ITS copy of the shared secret (the world's canonical config), whatever
        the service's .env currently says."""
        import hashlib
        import hmac
        import json as _json

        status, text = result
        if status != 201:
            return
        try:
            resp = _json.loads(text)
            event = {"event": "capture.settled", "gateway_ref": resp["gateway_ref"],
                     "order_id": resp["order_id"], "amount_cents": resp["total_cents"]}
        except (ValueError, KeyError):
            return
        secret = self.world.base_env.get("WEBHOOK_SIGNING_SECRET", "")
        if not secret:
            return
        raw = _json.dumps(event).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        time.sleep(self.rng.uniform(0.2, 1.0))
        self._request("POST", "/webhooks/payments", event, "PaymentsGateway-Webhooks/2.4",
                      headers={"X-Signature": sig})

    def _request(self, method: str, path: str, body, ua: str, log_nginx: bool = True, ip: str | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, str]:
        path = self.world.naming.route(path)  # canonical -> this stack's concrete route
        hdrs = {"User-Agent": ua}
        if headers:
            hdrs.update(headers)
        status, text = util.http_request(method, self.world.base_url + path, body=body, timeout=8, headers=hdrs)
        self.sent += 1
        if status == 0:
            self.failed += 1
            status = 502
            text = ""
        if log_nginx:
            ts = datetime.now(timezone.utc)
            line = f'{ip or tp.fake_client_ip(self.rng)} - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "{method} {path} HTTP/1.1" {status} {len(text)} "-" "{ua}"\n'
            try:
                with open(self._nginx, "a") as f:
                    f.write(line)
            except OSError:
                pass
        return status, text
