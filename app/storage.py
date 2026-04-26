"""
Submission persistence.

One JSON file per submission, stored in SUBMISSIONS_DIR (a Docker volume
mount). IDs are lowercase ULIDs (sortable by time). This scales fine to
thousands of submissions; when it stops being fine, swap for SQLite.

File layout:
    /data/submissions/01HXYZ....json

Schema:
    {
      "id": "01HXYZ...",
      "status": "pending_review" | "changes_requested" | "approved"
                | "signed" | "declined",
      "created_at": ISO8601,
      "updated_at": ISO8601,
      "form_data": { ...full form dict... },
      "history": [ {"at": ISO8601, "event": str, "note": str?} ],
      "signwell_document_id": str | null,
      "landlord_notes": str | null,   # internal notes, never shown to tenant
      "tenant_fix_note": str | null,  # last "please fix X" note shown to tenant
    }
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUBMISSIONS_DIR = Path(os.environ.get("SUBMISSIONS_DIR", "/data/submissions"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    """Crockford-style ULID-ish: 10 chars time + 16 chars random, lowercase.

    We don't need strict ULID compliance — we need "sortable by time" and
    "collision-resistant." A 48-bit timestamp + 80 bits of randomness hits
    both with no dependencies.
    """
    ts = int(time.time() * 1000)  # ms
    # 10-char base32 of 48-bit timestamp
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford-ish
    ts_part = ""
    for _ in range(10):
        ts_part = alphabet[ts & 0x1F] + ts_part
        ts >>= 5
    rand_part = secrets.token_hex(8)  # 16 chars
    return ts_part + rand_part


def _path(submission_id: str) -> Path:
    # Defensive: ensure the id is a bare filename, not a traversal attempt.
    if "/" in submission_id or ".." in submission_id or not submission_id:
        raise ValueError("invalid submission id")
    return SUBMISSIONS_DIR / f"{submission_id}.json"


def ensure_dir() -> None:
    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)


def create(form_data: dict[str, Any]) -> dict[str, Any]:
    """Create a new submission in pending_review state. Returns the record."""
    ensure_dir()
    sid = _new_id()
    now = _now()
    record = {
        "id": sid,
        "status": "pending_review",
        "created_at": now,
        "updated_at": now,
        "form_data": form_data,
        "history": [{"at": now, "event": "submitted"}],
        "signwell_document_id": None,
        "landlord_notes": None,
        "tenant_fix_note": None,
    }
    _write(record)
    return record


def load(submission_id: str) -> dict[str, Any] | None:
    p = _path(submission_id)
    if not p.exists():
        return None
    with p.open("r") as f:
        return json.load(f)


def _write(record: dict[str, Any]) -> None:
    p = _path(record["id"])
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
    tmp.replace(p)  # atomic swap


def update_status(
    submission_id: str,
    status: str,
    *,
    event: str,
    note: str | None = None,
    form_data: dict[str, Any] | None = None,
    signwell_document_id: str | None = None,
    tenant_fix_note: str | None = None,
) -> dict[str, Any]:
    """Update a submission's status and append a history entry."""
    record = load(submission_id)
    if record is None:
        raise KeyError(submission_id)
    record["status"] = status
    record["updated_at"] = _now()
    entry: dict[str, Any] = {"at": record["updated_at"], "event": event}
    if note:
        entry["note"] = note
    record["history"].append(entry)
    if form_data is not None:
        record["form_data"] = form_data
    if signwell_document_id is not None:
        record["signwell_document_id"] = signwell_document_id
    if tenant_fix_note is not None:
        record["tenant_fix_note"] = tenant_fix_note
    _write(record)
    return record


def delete(submission_id: str) -> bool:
    """Permanently delete a submission JSON file. Returns True if deleted,
    False if it didn't exist."""
    p = _path(submission_id)
    if not p.exists():
        return False
    p.unlink()
    return True


def list_all(status_filter: str | None = None) -> list[dict[str, Any]]:
    """List submissions, newest first. Optionally filter by status."""
    ensure_dir()
    records = []
    for p in sorted(SUBMISSIONS_DIR.glob("*.json"), reverse=True):
        try:
            with p.open("r") as f:
                r = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if status_filter and r.get("status") != status_filter:
            continue
        records.append(r)
    return records
