"""
Log of connection problems the app ran into and what it did about them, e.g. a stuck dongle and
whether resetting it brought it back. Kept so `doctor` (and a bug report) can show whether the
automatic reset actually helps, since the stuck state only turns up every few days.

One JSON object per line in $XDG_STATE_HOME/audezeforlinux/events.log (~/.local/state/...).
"""

import json
import os
import time
from typing import Any, Dict, List

MAX_LOG_BYTES = 64 * 1024


def log_path() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "audezeforlinux", "events.log")


def record_event(event: str, **fields: Any) -> None:
    """Appends one event. Never raises: logging must not get in the way of talking to the device."""
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
    entry.update(fields)
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Keep only the newer half once the log grows past MAX_LOG_BYTES
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[len(lines) // 2:])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def recent_events(limit: int = 5) -> List[Dict[str, Any]]:
    """The last `limit` events, oldest first."""
    try:
        with open(log_path(), "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
