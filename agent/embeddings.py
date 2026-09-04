"""Embedding client.

Turns evidence chunks and queries into vectors using a local Ollama embedding
model. No API keys, no outbound calls — same daemon that serves the reasoning
model.

Same contract as the rest of the agent: returns a dict with `success` and an
`error` that is None on success, and never raises. When embeddings are
unavailable the caller falls back to lexical retrieval rather than failing the
investigation.
"""

import json
import os
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("SENTINEL_EMBED_MODEL", "nomic-embed-text")

# nomic-embed-text emits 768 dimensions. Declared here because the vec0 virtual
# table needs a fixed width at CREATE time.
EMBED_DIM = int(os.environ.get("SENTINEL_EMBED_DIM", "768"))

TIMEOUT = 120

# Ollama caps batch work by memory, not count; this keeps one request modest.
BATCH_SIZE = 32


def _post(path, payload):
    request = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _embed_batch(texts, model):
    """Call Ollama's embedding API. Raises; callers wrap."""
    try:
        response = _post(
            "/api/embed",
            # keep_alive 0 unloads the embedder immediately: on a 6GB GPU it would
            # otherwise sit resident and force the reasoning model onto the CPU.
            {"model": model, "input": texts, "keep_alive": "0s"},
        )
        vectors = response.get("embeddings")

        if vectors:
            return vectors
    except urllib.error.HTTPError as error:
        # Older daemons only expose the single-input /api/embeddings route.
        if error.code != 404:
            raise

    vectors = []

    for text in texts:
        response = _post(
            "/api/embeddings", {"model": model, "prompt": text, "keep_alive": "0s"}
        )
        vectors.append(response.get("embedding", []))

    return vectors


def embed_texts(texts, model=None):
    """Embed a list of strings. Returns one vector per input, in order."""
    model = model or EMBED_MODEL

    if not texts:
        return {"success": False, "model": model, "vectors": [], "dim": 0, "error": "No text to embed"}

    vectors = []

    try:
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(_embed_batch(texts[start:start + BATCH_SIZE], model))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]

        if error.code == 404:
            return {
                "success": False,
                "model": model,
                "vectors": [],
                "dim": 0,
                "error": f"Embedding model '{model}' is not available. Run: ollama pull {model}",
            }

        return {
            "success": False,
            "model": model,
            "vectors": [],
            "dim": 0,
            "error": f"Ollama returned HTTP {error.code}: {detail}",
        }
    except urllib.error.URLError as error:
        return {
            "success": False,
            "model": model,
            "vectors": [],
            "dim": 0,
            "error": (
                f"Ollama is not reachable at {OLLAMA_HOST} ({error.reason}). "
                "Start it with: ollama serve"
            ),
        }
    except (TimeoutError, OSError, json.JSONDecodeError) as error:
        return {
            "success": False,
            "model": model,
            "vectors": [],
            "dim": 0,
            "error": f"Embedding request failed: {error}",
        }

    if len(vectors) != len(texts) or not vectors[0]:
        return {
            "success": False,
            "model": model,
            "vectors": [],
            "dim": 0,
            "error": f"Expected {len(texts)} vectors, got {len(vectors)}",
        }

    return {
        "success": True,
        "model": model,
        "vectors": vectors,
        "dim": len(vectors[0]),
        "error": None,
    }


def embed_one(text, model=None):
    """Embed a single string — used for the query side of retrieval."""
    result = embed_texts([text], model=model)

    if not result["success"]:
        return {"success": False, "model": result["model"], "vector": None, "dim": 0, "error": result["error"]}

    return {
        "success": True,
        "model": result["model"],
        "vector": result["vectors"][0],
        "dim": result["dim"],
        "error": None,
    }
