"""Retrieval layer.

Sits between the store and the LLM. The model has a fixed context window, the
store does not — this decides which stored chunks are worth spending that window
on, so evidence is selected on relevance rather than dropped by a blind
per-item character cap.

Retrieval is semantic: the query is embedded with the same local model used to
embed the chunks, and sqlite-vec ranks them by vector distance. Severity still
weighs in, so a chunk carrying an actual error outranks a merely similar one.

If embeddings are unavailable — no model pulled, daemon down — this falls back
to lexical scoring over every stored chunk instead of failing. Degraded
retrieval beats no diagnosis.

Same contract as the rest of the agent: returns dicts with `success` and an
`error` that is None on success, never raises.
"""

import re

from embeddings import embed_one
from store import get_chunks, get_source_inventory, vector_search

# Prompt budget for an 8k-context model once the system prompt, the JSON schema
# instruction, and the response have taken their share. Sized for a 6GB GPU:
# 12k chars pushed llama3.1:8b past 180s on this hardware, 6k lands near 90s.
DEFAULT_BUDGET_CHARS = 6000

# Signals that a chunk carries the incident rather than background noise.
ERROR_TERMS = (
    "error", "fatal", "exception", "traceback", "failed", "failure",
    "refused", "denied", "timeout", "timed out", "unauthorized",
    "panic", "crash", "critical", "cannot", "unable",
)

SEVERITY_WEIGHT = {"ERROR": 6.0, "WARNING": 3.0, "INFO": 0.0}

TOKEN_RE = re.compile(r"[a-z0-9_]+")

# What we are asking the index for when the caller has no specific question.
DEFAULT_QUERY = (
    "error failure exception stack trace connection refused authentication "
    "denied timeout crash restart misconfiguration root cause of the incident"
)

# Distance is small when similar; this converts it to a positive contribution.
VECTOR_WEIGHT = 8.0


def _tokens(text):
    return set(TOKEN_RE.findall((text or "").lower()))


def score_chunk(chunk, query_tokens):
    """Rank one chunk. Higher is more worth sending to the model."""
    text = chunk.get("text", "")
    lowered = text.lower()

    score = SEVERITY_WEIGHT.get(chunk.get("severity", "INFO"), 0.0)

    # Each distinct error term present, counted once, so a log that repeats
    # "ERROR" 400 times does not crowd out every other source.
    score += 2.0 * sum(1 for term in ERROR_TERMS if term in lowered)

    if query_tokens:
        overlap = query_tokens & _tokens(text)
        score += 1.5 * len(overlap)

    # Mild preference for the head of a source, where startup and connection
    # failures usually appear.
    score += max(0.0, 1.0 - 0.1 * chunk.get("seq", 0))

    return score


def select_context(incident_row, query="", budget_chars=DEFAULT_BUDGET_CHARS, k=40):
    """Retrieve the chunks worth spending the model's context window on.

    Semantic first: embed the query, KNN the vector index, then rank by distance
    and severity together. Falls back to lexical scoring over all stored chunks
    when the embedding model is not available.

    Every source that produced chunks is guaranteed at least its best chunk, so
    a high-volume noisy source cannot starve a quiet one out of the context.
    """
    effective_query = query.strip() or DEFAULT_QUERY

    mode = "vector"
    embedded = embed_one(effective_query)
    chunks = []
    degraded = None

    if embedded["success"]:
        found = vector_search(incident_row, embedded["vector"], k=k)

        if found["success"] and found["matches"]:
            chunks = found["matches"]
        else:
            mode = "lexical"
            degraded = found.get("error") or "Vector index returned no matches"
    else:
        mode = "lexical"
        degraded = embedded["error"]

    if mode == "lexical":
        stored = get_chunks(incident_row)

        if not stored["success"]:
            return {
                "success": False, "mode": mode, "chunks": [], "used_chars": 0,
                "budget_chars": budget_chars, "sources": [],
                "error": stored["error"],
            }

        chunks = stored["chunks"]

    if not chunks:
        return {
            "success": False, "mode": mode, "chunks": [], "used_chars": 0,
            "budget_chars": budget_chars, "sources": [],
            "error": degraded or f"No chunks stored for incident row {incident_row}",
        }

    query_tokens = _tokens(effective_query)

    for chunk in chunks:
        score = score_chunk(chunk, query_tokens)

        # Vector hits carry a distance; closer is better.
        if chunk.get("distance") is not None:
            score += VECTOR_WEIGHT / (1.0 + float(chunk["distance"]))

        chunk["score"] = score

    selected = []
    used = 0
    taken = set()

    # Pass 1 - fair share: the best chunk from every source that matched.
    by_source = {}
    for chunk in chunks:
        source = chunk.get("source", "unknown")
        if source not in by_source or chunk["score"] > by_source[source]["score"]:
            by_source[source] = chunk

    for chunk in sorted(by_source.values(), key=lambda c: -c["score"]):
        if used + len(chunk["text"]) > budget_chars:
            continue
        selected.append(chunk)
        taken.add(chunk["id"])
        used += len(chunk["text"])

    # Pass 2 - spend what is left on the highest scorers overall.
    for chunk in sorted(chunks, key=lambda c: -c["score"]):
        if chunk["id"] in taken:
            continue
        if used + len(chunk["text"]) > budget_chars:
            continue
        selected.append(chunk)
        taken.add(chunk["id"])
        used += len(chunk["text"])

    # Restore source order so the model reads each source contiguously.
    selected.sort(key=lambda c: (c.get("source", ""), c.get("seq", 0)))

    return {
        "success": True,
        "mode": mode,
        "degraded_reason": degraded,
        "query": effective_query,
        "chunks": selected,
        "used_chars": used,
        "budget_chars": budget_chars,
        "selected": len(selected),
        "available": len(chunks),
        "sources": sorted({c.get("source", "unknown") for c in selected}),
        "error": None,
    }


def format_context(selection, incident_row=None):
    """Render the retrieved chunks into the prompt body the LLM receives.

    Leads with an inventory of every source collected, so a source that failed
    or returned nothing is still visible to the model instead of being silently
    absent from the prompt.
    """
    blocks = []

    if incident_row is not None:
        inventory = get_source_inventory(incident_row)

        if inventory["success"] and inventory["sources"]:
            lines = ["=== SOURCES CONSULTED ==="]

            for entry in inventory["sources"]:
                note = ""

                if entry["raw_chars"] == 0:
                    note = "  <-- returned NO DATA"
                elif entry["chunk_count"] == 0:
                    note = "  <-- stored but not indexed"

                lines.append(
                    f"{entry['source']:14s} {entry['severity']:8s} "
                    f"{entry['raw_chars']:7d} chars collected{note}"
                )
                lines.append(f"    finding: {entry['finding']}")

            lines.append(
                "\nExcerpts below are the passages most relevant to the query; "
                "a source listed above with no excerpt contributed nothing."
            )
            blocks.append("\n".join(lines))

    current = None

    for chunk in selection.get("chunks", []):
        source = chunk.get("source", "unknown")

        if source != current:
            current = source
            blocks.append(
                "\n".join(
                    [
                        f"=== SOURCE: {source} ===",
                        f"category: {chunk.get('category', 'unknown')}",
                        f"severity: {chunk.get('severity', 'unknown')}",
                        f"finding: {chunk.get('finding', '')}",
                        f"total_collected_chars: {chunk.get('raw_chars', 0)}",
                        "excerpts:",
                    ]
                )
            )

        blocks.append(chunk.get("text", ""))

    return "\n".join(blocks)
