#!/usr/bin/env python3
"""Print ANSWERING_LLMS models whose sanitized answer keys are missing from answers/."""

from __future__ import annotations

import argparse
from pathlib import Path

from _model_inventory import load_answer_model_keys, load_answering_models


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print ANSWERING_LLMS models that do not appear in answers/."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root containing common.py and answers/.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    common_path = project_root / "common.py"
    answer_model_keys = set(load_answer_model_keys(project_root / "answers"))

    missing_models = [
        model.model_name
        for model in load_answering_models(common_path)
        if model.sanitized_name not in answer_model_keys
    ]

    for model_name in missing_models:
        print(model_name)

    return 1 if missing_models else 0


if __name__ == "__main__":
    raise SystemExit(main())
