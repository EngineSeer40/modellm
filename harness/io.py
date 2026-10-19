"""Typed JSON, content identities and exclusive/atomic artifact writes."""

import base64
import hashlib
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel


def encode(value):
    if isinstance(value, BaseModel):
        return encode(value.model_dump(mode="python"))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, (datetime, date, Decimal, bytes)):
        kind = type(value).__name__
        content = base64.b64encode(value).decode() if isinstance(value, bytes) else str(value)
        return {"$harness_type": kind, "value": content}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$harness_type": "float", "value": repr(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # numpy scalars occur in frame diagnostics, but arbitrary objects are errors.
    if hasattr(value, "item"):
        return encode(value.item())
    raise TypeError(f"Cannot preserve JSON type: {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"$harness_type", "value"}:
            parsers = {"date": date.fromisoformat, "datetime": datetime.fromisoformat,
                       "Decimal": Decimal, "bytes": base64.b64decode, "float": float}
            return parsers[value["$harness_type"]](value["value"])
        return {k: decode(v) for k, v in value.items()}
    return value


def canonical(value):
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return decode(json.loads(Path(path).read_text()))


def write(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(encode(value), indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x") as stream:
            stream.write(content)
        return
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(content)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return f"{prefix}_{uuid4().hex[:12]}"


def safe_id(value):
    import re
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", value):
        raise ValueError("ID must contain letters, numbers, dots, underscores or dashes")
    return value


def seal(path, value, *, exclusive=True):
    data = encode(value)
    write(path, {"sha256": digest(data), "data": data}, exclusive=exclusive)
    return digest(data)


def unseal(path):
    envelope = read(path)
    if digest(envelope["data"]) != envelope["sha256"]:
        raise ValueError(f"Manifest hash mismatch: {path}")
    return envelope["data"], envelope["sha256"]
