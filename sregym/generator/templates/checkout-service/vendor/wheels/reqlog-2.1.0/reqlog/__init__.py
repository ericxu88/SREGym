"""reqlog -- tiny structured request-log formatting helpers (internal package).

    >>> kv({"user": 5, "order": 12})
    'user=5 order=12'
"""

__version__ = "2.1.0"


def kv(mapping) -> str:
    """Render a mapping as space-separated key=value pairs (insertion order preserved)."""
    return " ".join(f"{k}={v}" for k, v in dict(mapping).items())


def scrub(mapping, keys=("password", "token", "secret", "authorization")):
    """Return a copy of mapping with sensitive keys masked."""
    lowered = {k.lower() for k in keys}
    return {k: ("***" if k.lower() in lowered else v) for k, v in dict(mapping).items()}
