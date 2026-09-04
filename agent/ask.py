#!/usr/bin/env python3
"""Terminal client for asking questions about stored evidence.

    python ask.py "why can't the api reach the database?"
    python ask.py                 # interactive loop

Talks to the store and the local model directly, so it works whether or not the
FastAPI service is running.
"""

import sys

from reasoning import answer_question
from retrieval import format_context, select_context
from store import store_stats


def ask(question, incident_row=None):
    if incident_row is None:
        stats = store_stats()

        if not stats["success"] or not stats.get("latest_incident_row"):
            return "No stored incidents yet. Run an investigation first."

        incident_row = stats["latest_incident_row"]

    retrieval = select_context(incident_row, query=question)

    if not retrieval["success"]:
        return f"Retrieval failed: {retrieval['error']}"

    print(
        f"[incident {incident_row} | {retrieval['mode']} | "
        f"{retrieval['selected']}/{retrieval['available']} chunks | "
        f"sources: {', '.join(retrieval['sources'])}]",
        file=sys.stderr,
    )

    result = answer_question(question, format_context(retrieval, incident_row=incident_row))

    return result["answer"] if result["success"] else f"Error: {result['error']}"


def main():
    if len(sys.argv) > 1:
        print(ask(" ".join(sys.argv[1:])))
        return

    print("Ask about the stored evidence. Ctrl-D or 'exit' to quit.\n")

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if question in ("exit", "quit"):
            return

        if question:
            print(f"\n{ask(question)}\n")


if __name__ == "__main__":
    main()
