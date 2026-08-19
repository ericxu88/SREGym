"""Traffic shape shared by the historical log generator and the live traffic generator."""
from __future__ import annotations

import math
import random

# weight, method, path template
ENDPOINT_MIX: list[tuple[int, str, str]] = [
    (30, "POST", "/checkout"),
    (24, "GET", "/orders/{order_id}"),
    (14, "GET", "/orders"),
    (24, "GET", "/users/{user_id}"),
    (8, "GET", "/users"),
]
HEALTH_INTERVAL_S = 10  # load balancer probe
METRICS_INTERVAL_S = 15  # prometheus scrape

USER_AGENTS = [
    "CheckoutApp/3.8.1 (iOS 17.5; iPhone15,3)",
    "CheckoutApp/3.7.0 (Android 14; Pixel 8)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "okhttp/4.12.0",
    "python-requests/2.32.3",
]

# typical latency (ms) per template: (median, sigma of lognormal)
LATENCY = {
    "/checkout": (9.0, 0.45),
    "/orders/{order_id}": (2.5, 0.5),
    "/orders": (3.0, 0.5),
    "/users/{user_id}": (2.0, 0.5),
    "/users": (2.5, 0.5),
    "/health": (1.5, 0.4),
    "/metrics": (0.6, 0.4),
}


def pick_endpoint(rng: random.Random) -> tuple[str, str]:
    total = sum(w for w, _, _ in ENDPOINT_MIX)
    r = rng.uniform(0, total)
    acc = 0.0
    for w, method, tmpl in ENDPOINT_MIX:
        acc += w
        if r <= acc:
            return method, tmpl
    return ENDPOINT_MIX[-1][1], ENDPOINT_MIX[-1][2]


def latency_ms(rng: random.Random, template: str, slow: bool = False) -> float:
    median, sigma = LATENCY.get(template, (3.0, 0.5))
    value = median * math.exp(rng.gauss(0, sigma))
    if slow:
        value = rng.uniform(700, 2400)
    return max(0.3, value)


def diurnal_factor(hour_utc: float) -> float:
    """Mild traffic curve: peak ~18:00 UTC, trough ~06:00 UTC (+/-25%)."""
    return 1.0 + 0.25 * math.sin((hour_utc - 12.0) / 24.0 * 2 * math.pi)


def fake_client_ip(rng: random.Random) -> str:
    return f"{rng.choice([31, 45, 66, 72, 89, 94, 103, 128, 152, 173, 185, 201, 212])}.{rng.randint(1, 254)}.{rng.randint(0, 254)}.{rng.randint(1, 254)}"
