"""Evidence store.

The durable stage between collection and reasoning. Every evidence item is
persisted at full fidelity — nothing is truncated on the way in — and split into
chunks that the retrieval layer can rank and feed to the LLM within its context
budget.

Deliberately SQLite, not the lab's Postgres. The agent must not depend on the
system it is diagnosing: when Postgres is the incident, a Postgres-backed store
would fail exactly when it is needed most.

Same contract as tools/ and reasoning.py: every function returns a dict with a
`success` boolean and an `error` that is None on success, and never raises.
"""

import os
import sqlite3
import struct
from datetime import datetime, timezone

import sqlite_vec

from embeddings import EMBED_DIM, embed_texts

DB_PATH = os.environ.get(
    "SENTINEL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.db"),
)

# Chunk size in characters. Small enough that a ranked subset fits an 8k context,
# large enough that a stack trace usually survives inside one chunk.
CHUNK_CHARS = 800
CHUNK_OVERLAP = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_row INTEGER NOT NULL REFERENCES incidents(id),
    source       TEXT NOT NULL,
    category     TEXT NOT NULL,
    finding      TEXT NOT NULL,
    severity     TEXT NOT NULL,
    raw_data     TEXT NOT NULL,
    raw_chars    INTEGER NOT NULL,
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id  INTEGER NOT NULL REFERENCES evidence(id),
    incident_row INTEGER NOT NULL REFERENCES incidents(id),
    seq          INTEGER NOT NULL,
    text         TEXT NOT NULL,
    embedded     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence(incident_row);
CREATE INDEX IF NOT EXISTS idx_chunks_incident ON chunks(incident_row);
"""


VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding float[{EMBED_DIM}]
);
"""


def serialize(vector):
    """Pack a float list into the little-endian float32 blob sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def _connect():
    """Open the store with the sqlite-vec extension loaded."""
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row

    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)

    return connection


def _ensure_schema(connection):
    connection.executescript(SCHEMA)
    connection.executescript(VEC_SCHEMA)


def init_store():
    """Create the schema if it does not exist. Safe to call repeatedly."""
    try:
        with _connect() as connection:
            _ensure_schema(connection)
    except sqlite3.Error as error:
        return {"success": False, "path": DB_PATH, "error": f"Could not initialize store: {error}"}

    return {"success": True, "path": DB_PATH, "error": None}


def chunk_text(text, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Split text into overlapping windows so a match near a boundary survives."""
    text = text or ""

    if not text.strip():
        return []

    if len(text) <= size:
        return [text]

    step = max(size - overlap, 1)
    return [text[start:start + size] for start in range(0, len(text), step) if text[start:start + size].strip()]


def _store_result(success, incident_row, stored=0, chunks=0, embedded=0, embed_error=None, error=None):
    """One return shape for every store_evidence() exit, so a caller can index
    `embed_error` (or any other key) unconditionally regardless of which path
    was taken - the same reasoning as evidence.py's `success`/`error` contract."""
    return {
        "success": success,
        "incident_row": incident_row,
        "stored": stored,
        "chunks": chunks,
        "embedded": embedded,
        "embed_error": embed_error,
        "error": error,
    }


def store_evidence(evidence, incident_id="INC-001", status="investigating", incident_row=None):
    """Persist a collected evidence list at full fidelity, plus its chunks.

    Pass `incident_row` to append to an existing incident instead of creating a
    new one - the live collector reuses a single row across polls so a
    continuous feed doesn't fragment into one incident per poll.
    """
    if not evidence:
        return _store_result(False, incident_row, error="No evidence to store")

    now = datetime.now(timezone.utc).isoformat()
    embed_error = None
    # `row` is the resolved id this call writes to; `incident_row` stays the
    # caller's original argument (None means "new incident"). Set up front so
    # it is always defined, including on a failure before the INSERT resolves it.
    row = incident_row

    try:
        with _connect() as connection:
            _ensure_schema(connection)

            if row is None:
                cursor = connection.execute(
                    "INSERT INTO incidents (incident_id, created_at, status) VALUES (?, ?, ?)",
                    (incident_id, now, status),
                )
                row = cursor.lastrowid
            else:
                connection.execute(
                    "UPDATE incidents SET status = ? WHERE id = ?", (status, row)
                )

            chunk_count = 0
            embed_count = 0
            pending = []

            for item in evidence:
                raw = str(item.get("raw_data", ""))

                cursor = connection.execute(
                    """
                    INSERT INTO evidence
                        (incident_row, source, category, finding, severity,
                         raw_data, raw_chars, collected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row,
                        item.get("source", "unknown"),
                        item.get("category", "unknown"),
                        item.get("finding", ""),
                        item.get("severity", "INFO"),
                        raw,
                        len(raw),
                        item.get("timestamp", now),
                    ),
                )
                evidence_id = cursor.lastrowid

                for seq, piece in enumerate(chunk_text(raw)):
                    cursor = connection.execute(
                        "INSERT INTO chunks (evidence_id, incident_row, seq, text) VALUES (?, ?, ?, ?)",
                        (evidence_id, row, seq, piece),
                    )
                    pending.append((cursor.lastrowid, piece))
                    chunk_count += 1

            # Embed every chunk in one pass, then index the vectors under the
            # chunk's own rowid so retrieval can join straight back to metadata.
            if pending:
                embedded = embed_texts([text for _, text in pending])

                if embedded["success"]:
                    if embedded["dim"] != EMBED_DIM:
                        embed_error = (
                            f"Embedding model returned {embedded['dim']} dimensions, "
                            f"but the index is built for {EMBED_DIM}. "
                            f"Set SENTINEL_EMBED_DIM={embedded['dim']} and rebuild the store."
                        )
                    else:
                        for (chunk_id, _), vector in zip(pending, embedded["vectors"]):
                            connection.execute(
                                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                                (chunk_id, serialize(vector)),
                            )
                            connection.execute(
                                "UPDATE chunks SET embedded = 1 WHERE id = ?", (chunk_id,)
                            )
                            embed_count += 1
                else:
                    # Chunks are still stored and still searchable lexically.
                    embed_error = embedded["error"]
    except sqlite3.Error as error:
        return _store_result(False, row, error=f"Could not store evidence: {error}")

    return _store_result(
        True, row, stored=len(evidence), chunks=chunk_count, embedded=embed_count, embed_error=embed_error
    )


def get_chunks(incident_row):
    """Return every stored chunk for an incident, with its evidence metadata."""
    try:
        with _connect() as connection:
            _ensure_schema(connection)

            rows = connection.execute(
                """
                SELECT c.id, c.seq, c.text, c.embedded,
                       e.source, e.category, e.finding, e.severity, e.raw_chars
                FROM chunks c
                JOIN evidence e ON e.id = c.evidence_id
                WHERE c.incident_row = ?
                ORDER BY e.id, c.seq
                """,
                (incident_row,),
            ).fetchall()
    except sqlite3.Error as error:
        return {"success": False, "chunks": [], "error": f"Could not read chunks: {error}"}

    return {"success": True, "chunks": [dict(row) for row in rows], "error": None}


def store_stats():
    """Report what the store currently holds.

    `latest_incident_row` is the incident that most recently received
    evidence, not simply the incident with the highest id: the live collector
    appends to one row across many polls, so its id stays fixed while a later
    manual /incidents/investigate call would otherwise look "newer" purely by
    id despite the live incident having fresher evidence. Falls back to the
    most recently created incident when nothing has any evidence yet.
    """
    try:
        with _connect() as connection:
            _ensure_schema(connection)

            incidents = connection.execute("SELECT count(*) FROM incidents").fetchone()[0]
            items = connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
            chunks = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            embedded = connection.execute("SELECT count(*) FROM chunks WHERE embedded = 1").fetchone()[0]
            chars = connection.execute("SELECT coalesce(sum(raw_chars), 0) FROM evidence").fetchone()[0]

            latest_active = connection.execute(
                "SELECT incident_row FROM evidence ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if latest_active:
                latest_incident_row = latest_active["incident_row"]
            else:
                latest_created = connection.execute(
                    "SELECT id FROM incidents ORDER BY id DESC LIMIT 1"
                ).fetchone()
                latest_incident_row = latest_created["id"] if latest_created else None
    except sqlite3.Error as error:
        return {"success": False, "error": f"Could not read store: {error}"}

    return {
        "success": True,
        "path": DB_PATH,
        "incidents": incidents,
        "evidence_items": items,
        "chunks": chunks,
        "embedded_chunks": embedded,
        "stored_chars": chars,
        "latest_incident_row": latest_incident_row,
        "error": None,
    }


def vector_search(incident_row, query_vector, k=40, overfetch=6):
    """KNN over the vector index, narrowed to one incident.

    sqlite-vec ranks globally, so it over-fetches and then filters by incident
    rather than returning fewer rows than asked for once older incidents are
    dropped.
    """
    if not query_vector:
        return {"success": False, "matches": [], "error": "No query vector"}

    try:
        with _connect() as connection:
            _ensure_schema(connection)

            rows = connection.execute(
                """
                SELECT c.id, c.seq, c.text, v.distance,
                       e.source, e.category, e.finding, e.severity, e.raw_chars
                FROM (
                    SELECT rowid, distance
                    FROM vec_chunks
                    WHERE embedding MATCH ? AND k = ?
                ) AS v
                JOIN chunks c ON c.id = v.rowid
                JOIN evidence e ON e.id = c.evidence_id
                WHERE c.incident_row = ?
                ORDER BY v.distance
                LIMIT ?
                """,
                (serialize(query_vector), k * overfetch, incident_row, k),
            ).fetchall()
    except sqlite3.Error as error:
        return {"success": False, "matches": [], "error": f"Vector search failed: {error}"}

    return {"success": True, "matches": [dict(row) for row in rows], "error": None}


def get_source_inventory(incident_row):
    """Every source collected for an incident, whether or not it produced chunks.

    Retrieval only ever returns chunks, so a source that failed or came back
    empty would vanish from the prompt entirely. The model needs to know a
    source was consulted and yielded nothing — that absence is itself evidence.
    """
    try:
        with _connect() as connection:
            _ensure_schema(connection)

            rows = connection.execute(
                """
                SELECT e.source, e.category, e.severity, e.finding, e.raw_chars,
                       count(c.id) AS chunk_count
                FROM evidence e
                LEFT JOIN chunks c ON c.evidence_id = e.id
                WHERE e.incident_row = ?
                GROUP BY e.id
                ORDER BY e.id
                """,
                (incident_row,),
            ).fetchall()
    except sqlite3.Error as error:
        return {"success": False, "sources": [], "error": f"Could not read inventory: {error}"}

    return {"success": True, "sources": [dict(row) for row in rows], "error": None}
