import json
import os
from threading import Lock

LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "audit_log.json")

_lock = Lock()


def _read_all():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def append_log_entry(entry):
    with _lock:
        entries = _read_all()
        entries.append(entry)
        with open(LOG_PATH, "w") as f:
            json.dump(entries, f, indent=2)


def get_log(limit=None):
    entries = list(reversed(_read_all()))
    if limit is not None:
        entries = entries[:limit]
    return entries


def update_log_entry(content_id, updates):
    """Merge `updates` into the existing entry for content_id, in place --
    e.g. an appeal augments the original classification entry rather than
    creating a disconnected second one. Returns True if an entry was found."""
    with _lock:
        entries = _read_all()
        found = False
        for entry in entries:
            if entry.get("content_id") == content_id:
                entry.update(updates)
                found = True
        if found:
            with open(LOG_PATH, "w") as f:
                json.dump(entries, f, indent=2)
        return found
