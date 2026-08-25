#!/usr/bin/env python3
"""Rename all generated artifacts for a model in the D-Bench workspace."""

from __future__ import annotations

import argparse
import json
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

    embeddings_dir = project_root / "embeddings" / "output"
    if embeddings_dir.is_dir():
        yield from sorted(embeddings_dir.glob(f"{model_key}_*.json"))


def renamed_path(path: Path, old_key: str, new_key: str) -> Path:
    prefix = f"{old_key}_"
    if not path.name.startswith(prefix):
        raise ValueError(f"{path.name} does not start with {prefix!r}")
    return path.with_name(f"{new_key}_{path.name[len(prefix):]}")


def update_embedding_source(
    path: Path, project_root: Path, old_key: str, new_key: str
) -> None:
    if path.parent != project_root / "embeddings" / "output" or path.suffix != ".json":
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return

    source = payload.get("source")
    prefix = f"{old_key}_"
    if not isinstance(source, str) or not source.startswith(prefix):
        return

    payload["source"] = f"{new_key}_{source[len(prefix):]}"
    path.write_text(json.dumps(payload), encoding="utf-8")


def rename_files(
    pairs: Iterable[tuple[Path, Path]],
    project_root: Path,
    old_key: str,
    new_key: str,
    dry_run: bool,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for source_path, destination_path in pairs:
        relative_parent = str(source_path.parent.relative_to(project_root))
        counts[relative_parent] += 1
        if dry_run:
            continue
        source_path.rename(destination_path)
        update_embedding_source(destination_path, project_root, old_key, new_key)
    return counts


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
            "Rename all generated artifacts for a model in answers, evaluation_* folders, "
            "explanations, and embeddings/output."
        )
    )
    parser.add_argument("old_model_name", help="Current model name or sanitized filename key.")
    parser.add_argument("new_model_name", help="New model name or sanitized filename key.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which files would be renamed without changing anything.",
    )
    parser.add_argument(
        "--skip-results",
        action="store_true",
        help="Do not rerun results.py to refresh leaderboard markdown after renaming.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root containing answers/, evaluation_*/, explanations/, and embeddings/output/.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    old_key = sanitize_model_name(args.old_model_name)
    new_key = sanitize_model_name(args.new_model_name)

    print(f"Old model name: {args.old_model_name}")
    print(f"New model name: {args.new_model_name}")
    print(f"Sanitized keys: {old_key} -> {new_key}")

    if old_key == new_key:
        print("Old and new sanitized keys are identical; nothing to rename.")
        return 0

    target_files = list(dict.fromkeys(iter_target_files(project_root, old_key)))
    if not target_files:
        print("No matching files found.")
        return 0

    pairs = [(path, renamed_path(path, old_key, new_key)) for path in target_files]
    conflicts = [destination for _, destination in pairs if destination.exists()]
    if conflicts:
        listing = "\n".join(f"  {path.relative_to(project_root)}" for path in conflicts)
        raise SystemExit(f"Refusing to overwrite existing files:\n{listing}")

    action = "Would rename" if args.dry_run else "Renaming"
    print(f"{action} {len(pairs)} file(s):")
    for source_path, destination_path in pairs:
        print(
            f"  {source_path.relative_to(project_root)} -> "
            f"{destination_path.relative_to(project_root)}"
        )

    counts = rename_files(
        pairs,
        project_root,
        old_key=old_key,
        new_key=new_key,
        dry_run=args.dry_run,
    )
    for folder_name in sorted(counts):
        print(f"{folder_name}: {counts[folder_name]}")

    if args.dry_run:
        print("Dry run only; no files were renamed.")
        return 0

    if args.skip_results:
        print("Skipped leaderboard refresh.")
        return 0

    print("Refreshing leaderboard markdown via results.py ...")
    refresh_leaderboards(project_root)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
