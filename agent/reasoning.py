"""LLM reasoning layer.

Runs against a local Ollama daemon — no API keys, no outbound network calls.
Takes the evidence the collector gathered and asks a Llama model to read a root
cause out of it.

Holds the same contract as everything in tools/: returns a dict with a `success`
boolean, its payload, and an `error` key that is None on success, and never
raises. A stopped Ollama or an unpulled model degrades the analysis; it does not
fail the investigation.
"""

import json
import os
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("SENTINEL_MODEL", "llama3.1:8b")

# Local inference is slow, so this is deliberately not the 10s bound the
# subprocess tools use.
TIMEOUT = 300

# Per-item truncation, so one noisy log dump cannot push the prompt past the
# model's context window and silently evict the other evidence.
MAX_RAW_CHARS = 3000

SYSTEM_PROMPT = """You are a site reliability engineer triaging a production incident.

You are given evidence collected from a running system: container logs, database
state, recent commits, and network listeners.

Reason only from the evidence given. Do not invent log lines, error messages, or
commits that are not present. If the evidence is not enough to name a cause, say
so and set confidence to "low".

Respond with a single JSON object and nothing else, using exactly these keys:
  root_cause           string           the most likely cause, one or two sentences
  confidence           string           "high", "medium", or "low"
  affected_components  array of strings which parts of the system are implicated
  supporting_evidence  array of strings which evidence sources led you there
  recommended_actions  array of strings concrete next steps, most important first
"""

ANALYSIS_KEYS = (
    "root_cause",
    "confidence",
    "affected_components",
    "supporting_evidence",
    "recommended_actions",
)


def format_evidence(evidence):
    """Render collected evidence into a bounded prompt body."""
    blocks = []

    for index, item in enumerate(evidence, start=1):
        raw = str(item.get("raw_data", ""))

        if len(raw) > MAX_RAW_CHARS:
            raw = raw[:MAX_RAW_CHARS] + "\n... [truncated]"

        blocks.append(
            "\n".join(
                [
                    f"--- EVIDENCE {index} ---",
                    f"source: {item.get('source', 'unknown')}",
                    f"category: {item.get('category', 'unknown')}",
                    f"severity: {item.get('severity', 'unknown')}",
                    f"finding: {item.get('finding', '')}",
                    "raw_data:",
                    raw,
                ]
            )
        )

    return "\n\n".join(blocks)


def _post_json(path, payload):
    """POST to Ollama and return the decoded response. Raises; callers wrap."""
    request = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def analyze_incident(evidence, model=None, context=None):
    """Ask the local model for a root-cause reading of the collected evidence.

    `context` is the retrieval layer's budgeted selection. When given it replaces
    the blind per-item truncation in format_evidence(), so what reaches the model
    is chosen by relevance rather than by character position.
    """
    model = model or MODEL

    if not evidence and not context:
        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": None,
            "error": "No evidence to analyze",
        }

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
            "num_ctx": 8192,
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Here is the evidence collected from the incident:\n\n"
                    f"{context if context else format_evidence(evidence)}\n\n"
                    "Analyze it and respond with the JSON object described above."
                ),
            },
        ],
    }

    try:
        response = _post_json("/api/chat", payload)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]

        if error.code == 404:
            return {
                "success": False,
                "model": model,
                "analysis": None,
                "raw": None,
                "error": f"Model '{model}' is not available. Run: ollama pull {model}",
            }

        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": None,
            "error": f"Ollama returned HTTP {error.code}: {detail}",
        }
    except urllib.error.URLError as error:
        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": None,
            "error": (
                f"Ollama is not reachable at {OLLAMA_HOST} ({error.reason}). "
                "Start it with: ollama serve"
            ),
        }
    except (TimeoutError, OSError) as error:
        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": None,
            "error": f"Ollama request failed after {TIMEOUT}s: {error}",
        }
    except json.JSONDecodeError as error:
        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": None,
            "error": f"Ollama returned a non-JSON response: {error}",
        }

    content = response.get("message", {}).get("content", "")

    try:
        analysis = json.loads(content)
    except json.JSONDecodeError:
        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": content,
            "error": "Model did not return parseable JSON",
        }

    if not isinstance(analysis, dict):
        return {
            "success": False,
            "model": model,
            "analysis": None,
            "raw": content,
            "error": "Model returned JSON that is not an object",
        }

    # Normalize the shape so callers can index the keys unconditionally, the way
    # evidence_collector.py indexes ["success"].
    normalized = {key: analysis.get(key) for key in ANALYSIS_KEYS}
    missing = [key for key in ANALYSIS_KEYS if analysis.get(key) is None]

    return {
        "success": True,
        "model": model,
        "analysis": normalized,
        "raw": content,
        "missing_keys": missing,
        "error": None,
    }


def check_llm(model=None):
    """Report whether the local model is reachable and pulled."""
    model = model or MODEL

    try:
        request = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")

        with urllib.request.urlopen(request, timeout=10) as response:
            tags = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        return {
            "success": False,
            "host": OLLAMA_HOST,
            "model": model,
            "model_available": False,
            "models": [],
            "error": (
                f"Ollama is not reachable at {OLLAMA_HOST} ({error.reason}). "
                "Start it with: ollama serve"
            ),
        }
    except (TimeoutError, OSError, json.JSONDecodeError) as error:
        return {
            "success": False,
            "host": OLLAMA_HOST,
            "model": model,
            "model_available": False,
            "models": [],
            "error": f"Could not read the model list: {error}",
        }

    names = [entry.get("name", "") for entry in tags.get("models", [])]

    return {
        "success": True,
        "host": OLLAMA_HOST,
        "model": model,
        "model_available": model in names,
        "models": names,
        "error": None,
    }


CHAT_SYSTEM_PROMPT = """You are a site reliability engineer answering questions about a
live incident. You are given evidence collected from the system: container logs,
database state, recent commits, network listeners, and host journals.

Answer only from the evidence given. Do not invent log lines, error messages, or
commits that are not present. If the evidence does not answer the question, say
exactly what is missing and which source would have it.

Be direct and concrete. Cite the source name when you use it. Prose, not JSON.
"""


def answer_question(question, context, model=None):
    """Answer a free-form question against retrieved evidence.

    The investigation path forces JSON for a fixed schema; this one is
    conversational, so the response is plain prose.
    """
    model = model or MODEL

    if not question or not question.strip():
        return {"success": False, "model": model, "answer": None, "error": "No question given"}

    if not context or not context.strip():
        return {
            "success": False,
            "model": model,
            "answer": None,
            "error": "No evidence context retrieved — run an investigation first",
        }

    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Evidence from the incident:\n\n{context}\n\n"
                    f"Question: {question.strip()}"
                ),
            },
        ],
    }

    try:
        response = _post_json("/api/chat", payload)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]

        if error.code == 404:
            return {
                "success": False,
                "model": model,
                "answer": None,
                "error": f"Model '{model}' is not available. Run: ollama pull {model}",
            }

        return {"success": False, "model": model, "answer": None, "error": f"Ollama returned HTTP {error.code}: {detail}"}
    except urllib.error.URLError as error:
        return {
            "success": False,
            "model": model,
            "answer": None,
            "error": (
                f"Ollama is not reachable at {OLLAMA_HOST} ({error.reason}). "
                "Start it with: ollama serve"
            ),
        }
    except (TimeoutError, OSError, json.JSONDecodeError) as error:
        return {"success": False, "model": model, "answer": None, "error": f"Ollama request failed: {error}"}

    answer = response.get("message", {}).get("content", "").strip()

    if not answer:
        return {"success": False, "model": model, "answer": None, "error": "Model returned an empty answer"}

    return {"success": True, "model": model, "answer": answer, "error": None}
