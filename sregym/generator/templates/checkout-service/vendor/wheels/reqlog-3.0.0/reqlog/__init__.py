"""reqlog -- structured request-log formatting helpers (internal package).

3.0 BREAKING CHANGES (see CHANGELOG):
  * kv() removed; use fields(), which returns a Fields object (str() gives the old output)
  * scrub() moved to reqlog.redact.scrub
"""

__version__ = "3.0.0"


class Fields:
    def __init__(self, mapping):
        self._m = dict(mapping)

    def __str__(self) -> str:
        return " ".join(f"{k}={v}" for k, v in self._m.items())

    def merged(self, **extra) -> "Fields":
        return Fields({**self._m, **extra})


def fields(mapping) -> Fields:
    """Render a mapping as structured fields. Replaces 2.x's kv()."""
    return Fields(mapping)
