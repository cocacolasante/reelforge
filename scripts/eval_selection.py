"""Selection-quality eval — thin wrapper over reelforge_core.reels.evaluate.

Run inside any ReelForge container (logic ships in the image):

    docker compose run --rm -T --entrypoint python cli - < scripts/eval_selection.py

or use the CLI command, which is the same thing with argument parsing:

    ./reelforge eval-select [--labels DIR]

Labels default to /app/tests/reels/eval/labels (mounted into the cli service),
falling back to ./tests/reels/eval/labels for host-side runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from reelforge_core.reels.evaluate import evaluate_all, format_report

DEFAULT_LABEL_DIRS = (
    Path("/app/tests/reels/eval/labels"),
    Path("tests/reels/eval/labels"),
)


def main(labels_dir: Path | None = None) -> int:
    if labels_dir is None:
        labels_dir = next((d for d in DEFAULT_LABEL_DIRS if d.is_dir()), None)
        if labels_dir is None:
            print("no labels directory found; pass one explicitly", file=sys.stderr)
            return 2
    data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    print(f"labels: {labels_dir}   data: {data_dir}\n")
    print(format_report(evaluate_all(labels_dir, data_dir)))
    return 0


if __name__ == "__main__":
    arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(main(arg))
