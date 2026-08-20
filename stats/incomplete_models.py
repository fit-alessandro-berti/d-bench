#!/usr/bin/env python3
"""Print models that do not yet have a full set of answers or judge evaluations."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable


ANSWER_FILENAME_RE = re.compile(r"^(?P<model_key>.+)_(?P<question_key>q\d+)\.txt$")
EVAL_FILENAME_RE = re.compile(r"^(?P<model_key>.+)_(?P<question_key>q\d+)\.txt\.json$")


def sanitize_model_name(model_name: str) -> str:
    sanitized = model_name.replace("/", "").replace(":", "")
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in sanitized)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print answering models that have fewer than a full set of answers, "
            "or fewer than a full set of evaluations for any configured judge."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root containing models.json, questions/, answers/, and evaluation_*/.",
    )
    return parser.parse_args()


def load_models_config(models_path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    payload = json.loads(models_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("models.json must contain a JSON object.")

    answering_names: list[str] = []
    raw_answering = payload.get("answering_llms")
    if not isinstance(raw_answering, list):
        raise ValueError("models.json must define an answering_llms list.")
    for entry in raw_answering:
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            answering_names.append(entry[0])

    judges: list[tuple[str, str]] = []
    raw_evaluators = payload.get("evaluator_llms")
    if not isinstance(raw_evaluators, list):
        raise ValueError("models.json must define an evaluator_llms list.")
    for entry in raw_evaluators:
        if (
            isinstance(entry, list)
            and len(entry) >= 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], str)
            and entry[1].strip()
        ):
            judges.append((entry[0], entry[1]))

    return answering_names, judges


def load_question_stems(questions_dir: Path) -> set[str]:
    return {path.stem for path in questions_dir.glob("*.txt") if path.is_file()}


def count_matching_files(
    directory: Path,
    glob_pattern: str,
    filename_re: re.Pattern[str],
    question_stems: set[str],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    if not directory.is_dir():
        return counts

    for path in directory.glob(glob_pattern):
        match = filename_re.match(path.name)
        if match is None or match.group("question_key") not in question_stems:
            continue
        counts[match.group("model_key")] += 1
    return counts


def display_count(count: int, expected: int) -> str:
    return str(count) if count < expected else ""


def render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def format_row(cells: Iterable[str]) -> str:
        formatted: list[str] = []
        for index, cell in enumerate(cells):
            if index == 0:
                formatted.append(cell.ljust(widths[index]))
            else:
                formatted.append(cell.rjust(widths[index]))
        return "  ".join(formatted)

    lines = [format_row(headers), format_row("-" * width for width in widths)]
    lines.extend(format_row(row) for row in rows)
    return lines


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    question_stems = load_question_stems(project_root / "questions")
    expected = len(question_stems)
    if expected == 0:
        print("No question files found.")
        return 1

    answering_names, configured_judges = load_models_config(project_root / "models.json")
    key_to_display_name = {sanitize_model_name(name): name for name in answering_names}

    existing_eval_dirs = {
        path.name: path
        for path in sorted(project_root.glob("evaluation_*"))
        if path.is_dir()
    }
    judges: list[tuple[str, str]] = list(configured_judges)
    configured_folders = {folder for _, folder in configured_judges}
    for folder_name in existing_eval_dirs:
        if folder_name not in configured_folders:
            judges.append((folder_name, folder_name))

    answer_counts = count_matching_files(
        project_root / "answers",
        "*.txt",
        ANSWER_FILENAME_RE,
        question_stems,
    )
    eval_counts_by_folder = {
        folder: count_matching_files(
            existing_eval_dirs.get(folder, project_root / folder),
            "*.json",
            EVAL_FILENAME_RE,
            question_stems,
        )
        for _, folder in judges
    }

    model_keys = set(key_to_display_name) | set(answer_counts)
    for counts in eval_counts_by_folder.values():
        model_keys.update(counts)

    headers = ["model", "answers", *[name for name, _ in judges]]
    rows: list[list[str]] = []
    for model_key in sorted(model_keys, key=lambda key: key_to_display_name.get(key, key).lower()):
        answer_count = answer_counts.get(model_key, 0)
        judge_counts = [
            eval_counts_by_folder[folder].get(model_key, 0) for _, folder in judges
        ]
        if answer_count >= expected and all(count >= expected for count in judge_counts):
            continue

        rows.append(
            [
                key_to_display_name.get(model_key, model_key),
                display_count(answer_count, expected),
                *[display_count(count, expected) for count in judge_counts],
            ]
        )

    if not rows:
        print(f"All models have {expected} answers and {expected} evaluations per judge.")
        return 0

    for line in render_table(headers, rows):
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
