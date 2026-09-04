---
description: Run a task through the Explore → Build → Review pipeline, then validate it yourself
argument-hint: <the task to run through the pipeline>
---

Run this task through the full pipeline: **$ARGUMENTS**

You are the orchestrator. You own the decisions and the final validation; the subagents do the legwork. Each subagent starts with a cold context, so every prompt you write must be self-contained — restate the task, do not refer to "the above" or to earlier stages the agent cannot see.

## 1. Explore

Spawn the `explore` agent. Give it the task verbatim plus what you already know, and tell it what specifically you need located. If the task has genuinely independent areas (say, the agent side and the lab side), spawn one `explore` per area **in a single message** so they run concurrently. Otherwise, one is enough.

Read the brief. If it comes back with open questions that change what gets built, resolve them — from the repo yourself, or by asking the user — before moving on.

## 2. Build

Spawn the `build` agent with: the task, the explore brief pasted in full (it cannot see the subagent's output otherwise), and the acceptance criteria you expect. One build agent — parallel agents editing the same tree collide.

If the task decomposes into files that genuinely do not touch each other, you may run two, but state the file boundary explicitly in each prompt.

## 3. Review

Spawn the `review` agent against the resulting diff. Give it the task and what was changed, but not the build agent's self-assessment — you want an independent read, not a confirmation.

If it returns BLOCKER or MAJOR findings, send them back to the same build agent with `SendMessage` (its context is intact — cheaper and better than a cold respawn). Re-review after the fix. Two rounds; if findings persist past that, stop and bring it to the user.

## 4. Tests + validation — you do this, not a subagent

This step is yours. Do not delegate it, and do not report success on a subagent's say-so.

- Read the actual diff yourself: `git status` and `git diff`.
- There is no test suite, linter, or formatter in this repo — verification is manual and you must actually run it:
  - `cd demo_app/lab && docker compose up -d --build`, then `docker compose ps` to confirm the stack is healthy.
  - Run the agent from `agent/` on a port other than 8000 (the lab API binds 8000, and both apps are `main:app`): `source .venv/bin/activate && uvicorn main:app --reload --port 8001`.
  - Hit the endpoints: `curl -s localhost:8001/incidents/investigate | jq`, plus the specific `/tools/*` endpoint the change touches.
  - Confirm each tool result still carries `success`, a payload, and an `error` key that is `None` on success.
- Never `docker compose down -v` (it drops the Postgres volume) unless the user asks.

## 5. Report

Tell the user: what changed (`file:line`), what you ran and what it actually returned, what the review found and whether it was fixed, and anything left undone. If a check failed or you skipped one, say so with the output — do not round up to "working".
