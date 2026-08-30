from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_hash(value: Any) -> str:
    """A short, deterministic digest, used to fingerprint rows for upsert."""
    if value is None:
        return ""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
