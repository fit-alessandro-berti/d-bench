#!/usr/bin/env python3
"""Remove all generated artifacts for a model from the D-Bench workspace."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator


def sanitize_model_name(model_name: str) -> str:
    # Keep this in sync with common.sanitize_model_name without importing common.py,
    # which requires API keys at import time.
    sanitized = model_name.replace("/", "").replace(":", "")
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in sanitized)


def iter_target_files(project_root: Path, model_key: str) -> Iterator[Path]:
    answers_dir = project_root / "answers"
    if answers_dir.is_dir():
        yield from sorted(answers_dir.glob(f"{model_key}_*.txt"))

    for evaluation_dir in sorted(path for path in project_root.glob("evaluation_*") if path.is_dir()):
        yield from sorted(evaluation_dir.glob(f"{model_key}_*.txt.json"))

    explanations_root = project_root / "explanations"
    if explanations_root.is_dir():
        for explanation_dir in sorted(path for path in explanations_root.iterdir() if path.is_dir()):
            yield from sorted(explanation_dir.glob(f"{model_key}_*.txt"))


def remove_files(paths: Iterable[Path], project_root: Path, dry_run: bool) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in paths:
        relative_parent = str(path.parent.relative_to(project_root))
        counts[relative_parent] += 1
        if not dry_run:
            path.unlink()
    return counts


def prune_empty_explanation_dirs(project_root: Path) -> None:
    explanations_root = project_root / "explanations"
    if not explanations_root.is_dir():
        return

    for explanation_dir in sorted((path for path in explanations_root.iterdir() if path.is_dir()), reverse=True):
        try:
            explanation_dir.rmdir()
        except OSError:
            continue

    try:
        explanations_root.rmdir()
    except OSError:
        pass


def refresh_leaderboards(project_root: Path) -> None:
    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", "unused")
    env.setdefault("GROK_API_KEY", "unused")
    env.setdefault("OPENROUTER_API_KEY", "unused")
    subprocess.run(
        [sys.executable, str(project_root / "results.py")],
        cwd=project_root,
        check=True,
        env=env,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove all generated artifacts for a model from answers, evaluation_* folders, "
            "and explanations."
        )
    )
    parser.add_argument("model_name", help="Original model name or sanitized filename key.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which files would be removed without deleting anything.",
    )
    parser.add_argument(
        "--skip-results",
        action="store_true",
        help="Do not rerun results.py to refresh leaderboard markdown after deletion.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root containing answers/, evaluation_*/, and explanations/.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    model_key = sanitize_model_name(args.model_name)

    target_files = list(dict.fromkeys(iter_target_files(project_root, model_key)))

    print(f"Model name: {args.model_name}")
    print(f"Sanitized key: {model_key}")

    if not target_files:
        print("No matching files found.")
        return 0

    action = "Would remove" if args.dry_run else "Removing"
    print(f"{action} {len(target_files)} file(s):")
    for path in target_files:
        print(f"  {path.relative_to(project_root)}")

    counts = remove_files(target_files, project_root, dry_run=args.dry_run)
    for folder_name in sorted(counts):
        print(f"{folder_name}: {counts[folder_name]}")

    if args.dry_run:
        print("Dry run only; no files were deleted.")
        return 0

    prune_empty_explanation_dirs(project_root)

    if args.skip_results:
        print("Skipped leaderboard refresh.")
        return 0

    print("Refreshing leaderboard markdown via results.py ...")
    refresh_leaderboards(project_root)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
