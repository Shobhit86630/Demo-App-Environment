"""Continuous, incremental evidence collection.

Runs as a background loop instead of waiting for a manual
`/incidents/investigate` call, so the store keeps filling with fresh evidence
in near-real-time and `/chat` always has something current to reason over.

Two things keep this cheap enough to poll every few seconds instead of every
few minutes:

1. **Delta fetch.** Log sources (`docker logs`, `journalctl`) are asked for
   only what was emitted since the last poll (`since=`), not the whole tail
   again - the store's chunk/embedding cost scales with new content, not with
   how often the loop runs.
2. **Change-only snapshots.** Sources with no native "since" (db connection
   count, network listeners, git log) are polled in full each cycle but only
   stored when their content actually changed since the last poll, so an idle
   system does not re-embed identical snapshots forever.

All evidence lands in one rolling "live" incident row, appended to rather than
recreated each poll - a continuous feed, not one incident per tick.

Same contract as the rest of the agent: nothing here raises past its own
boundary. A single source failing mid-poll degrades that source's evidence to
`success: False`, same as `evidence_collector.py`; `LiveCollector.run_forever()`
catches anything that still escapes so one bad poll can't kill the loop.
"""

import asyncio
import os
from datetime import datetime, timezone

from evidence_collector import build_evidence
from store import store_evidence
from tools.database import inspect_database
from tools.docker import get_container_logs
from tools.git import get_git_history
from tools.system import get_network_connections
from tools.systemd import get_systemd_logs

DEFAULT_POLL_INTERVAL_SECONDS = 20
LIVE_INCIDENT_ID = "INC-LIVE"


def _read_poll_interval():
    """Parse SENTINEL_POLL_INTERVAL, falling back to the default on anything
    that isn't a usable positive integer - a bad env var should degrade to a
    working interval, not crash the import chain three modules deep."""
    raw = os.environ.get("SENTINEL_POLL_INTERVAL")

    if not raw:
        return DEFAULT_POLL_INTERVAL_SECONDS

    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_POLL_INTERVAL_SECONDS

    return value if value >= 1 else DEFAULT_POLL_INTERVAL_SECONDS


POLL_INTERVAL_SECONDS = _read_poll_interval()

# Snapshot sources have no native "since" query, so they are polled in full
# every cycle and deduped by content instead. (source, category, subject,
# payload_key, fetch) - fetch takes no arguments so every source is called the
# same way in the loop below.
SNAPSHOT_SPECS = (
    ("postgresql", "database", "database state", "connections", inspect_database),
    ("git", "version_control", "Git history", "commits", lambda: get_git_history(10)),
    ("network", "network", "network listeners", "output", get_network_connections),
)


class LiveCollector:
    """Polls evidence sources on an interval and appends to one rolling incident.

    All per-poll state - watermarks for the delta sources, fingerprints for the
    snapshot sources, the incident row itself - lives on the instance rather
    than at module scope, so a second collector (a test, a restart-in-process)
    starts clean instead of inheriting another instance's history.
    """

    def __init__(self):
        self.incident_row = None
        self.polls = 0
        self.last_poll_at = None
        self.last_error = None
        self._logs_since = None
        self._last_snapshot = {}

    def _take_log_deltas(self):
        """Log sources: only lines emitted since the previous poll.

        The watermark is taken before the fetch and only committed after both
        calls return, so a line emitted while the fetch is in flight lands in
        exactly one poll's window instead of being double-counted across two.
        """
        since = self._logs_since
        fetch_started_at = datetime.now(timezone.utc).isoformat()

        logs = get_container_logs("sentinel-api", 1000, since=since)
        systemd = get_systemd_logs("docker", 1000, since=since)

        self._logs_since = fetch_started_at

        evidence = []

        if logs.get("logs", "").strip() or not logs.get("success"):
            evidence.append(
                build_evidence("docker_logs", "application", "new API container log lines", logs, "logs")
            )

        if systemd.get("logs", "").strip() or not systemd.get("success"):
            evidence.append(
                build_evidence("systemd", "host", "new docker daemon journal entries", systemd, "logs")
            )

        return evidence

    def _take_changed_snapshots(self):
        """State sources: polled in full, stored only when they've changed."""
        evidence = []

        for source, category, subject, payload_key, fetch in SNAPSHOT_SPECS:
            result = fetch()
            payload = result.get(payload_key)
            fingerprint = payload if isinstance(payload, str) else str(result)

            if self._last_snapshot.get(source) == fingerprint:
                continue

            self._last_snapshot[source] = fingerprint
            evidence.append(build_evidence(source, category, subject, result, payload_key))

        return evidence

    def collect(self):
        """One poll's worth of evidence - empty once the system goes quiet."""
        return self._take_log_deltas() + self._take_changed_snapshots()

    def poll_once(self):
        self.polls += 1
        self.last_poll_at = datetime.now(timezone.utc).isoformat()

        evidence = self.collect()

        if not evidence:
            return {"success": True, "stored": 0, "incident_row": self.incident_row}

        result = store_evidence(
            [item.to_dict() for item in evidence],
            incident_id=LIVE_INCIDENT_ID,
            status="live",
            incident_row=self.incident_row,
        )

        if result["success"]:
            self.incident_row = result["incident_row"]

        self.last_error = result.get("embed_error") or result.get("error")

        return result

    def stats(self):
        return {
            "incident_row": self.incident_row,
            "polls": self.polls,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        }

    async def run_forever(self):
        """The background loop `main.py` starts on FastAPI `startup`.

        `poll_once` blocks on subprocess and psycopg2 calls, so it runs in a
        thread rather than inline on the event loop. Anything it raises is
        caught and recorded on `last_error` instead of propagating - an
        uncaught exception here would end the loop silently, since nothing
        awaits this task's result.
        """
        while True:
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception as error:
                self.last_error = f"Poll failed: {error}"

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
