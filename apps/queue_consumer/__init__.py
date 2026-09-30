"""Queue-driven service interface for growth-agent.

This package is an interface layer only. It consumes jobs from a shared BullMQ
queue, drives the EXISTING pipeline (analyze -> select -> compose -> export), and
emits a manifest carrying stable clip ids and per-stage selection scores.

Clip-selection behaviour is frozen: nothing here imports from
`reelforge_core.reels` except `select_reels` itself, and nothing under
`reelforge_core/reels/` is modified. The CLI, the FastAPI app and the arq worker
continue to work exactly as before.
"""
