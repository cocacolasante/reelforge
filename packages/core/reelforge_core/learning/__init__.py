"""Learning loop (pro-editing CP12): what real audiences did with the reels.

Performance labels arrive from the growth agent (apps/queue_consumer →
`labels.sqlite3`, keyed by `reelforge_clip_id` = candidate_id). This package
joins them with what selection and compose knew about each reel, reports
which signals predict completion and shares, fits score weights OFFLINE
(never online), and lifts the best performers into the ranking prompt as
examples. Everything is inert until labels exist.
"""
