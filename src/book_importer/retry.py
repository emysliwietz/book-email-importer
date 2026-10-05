"""Mails whose import failed wait here (RETRY_DIR) for the next attempt - on disk, so a restart doesn't lose them.

Every mail has two files: <id>.eml (the raw mail) and <id>.json (attempt count, next due time, library files the
earlier attempts already wrote, whether the sender was told about the retries). Only *.json files are read, and they
are written last and atomically, so a half-written entry is never picked up.
"""
import json
import os
import time

from . import config


def _path(entry_id: str, ext: str) -> str:
    return os.path.join(config.RETRY_DIR, entry_id + ext)


def save(entry: dict) -> None:
    os.makedirs(config.RETRY_DIR, exist_ok=True)
    tmp = _path(entry["id"], ".json.tmp")
    with open(tmp, "w") as f:
        json.dump(entry, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(entry["id"], ".json"))


def add(raw: bytes, uid: int | None, attempt: int, next_at: float, placed: list[str], notified: bool,
        error: str | None) -> dict:
    os.makedirs(config.RETRY_DIR, exist_ok=True)
    entry_id = f"{int(time.time())}-{uid if uid is not None else 'x'}"
    with open(_path(entry_id, ".eml"), "wb") as f:
        f.write(raw)
    entry = {"id": entry_id, "uid": uid, "attempt": attempt, "next_at": next_at, "placed": placed,
             "notified": notified, "first_error": error, "last_error": error, "created": time.time()}
    save(entry)
    return entry


def raw(entry: dict) -> bytes:
    with open(_path(entry["id"], ".eml"), "rb") as f:
        return f.read()


def remove(entry: dict) -> None:
    for ext in (".json", ".eml"):
        try:
            os.remove(_path(entry["id"], ext))
        except FileNotFoundError:
            pass


def entries() -> list[dict]:
    try:
        names = sorted(n for n in os.listdir(config.RETRY_DIR) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(config.RETRY_DIR, n)) as f:
                out.append(json.load(f))
        except (OSError, ValueError) as e:
            print(f"⚠️ unreadable retry entry {n}: {e}")
    return sorted(out, key=lambda e: e["next_at"])


def due(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [e for e in entries() if e["next_at"] <= now]


def next_due() -> float | None:
    es = entries()
    return es[0]["next_at"] if es else None


def delay_after(failed_attempts: int, started: float, now: float | None = None) -> float | None:
    """Minutes to wait after the n-th failed attempt (1 = the original import), None = give up.
    started: time of the first failure. The last gap repeats; the final attempt lands at RETRY_MAX_DAYS."""
    sched = config.RETRY_SCHEDULE_MIN
    if not sched or failed_attempts < 1:
        return None
    left = (started + config.RETRY_MAX_DAYS * 86400 - (time.time() if now is None else now)) / 60
    if left <= 0:
        return None
    return min(sched[min(failed_attempts, len(sched)) - 1], left)


def human(minutes: float) -> str:
    if minutes < 60:
        m = round(minutes)
        return f"{m} Minute" + ("" if m == 1 else "n")
    if minutes < 48 * 60:
        h = round(minutes / 60, 1)
        return f"{h:g}".replace(".", ",") + " Stunde" + ("" if h == 1 else "n")
    d = round(minutes / 1440, 1)
    return f"{d:g}".replace(".", ",") + " Tag" + ("" if d == 1 else "e")
