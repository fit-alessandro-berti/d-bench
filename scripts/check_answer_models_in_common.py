#!/usr/bin/env python3
"""Print answer model keys that do not map back to ANSWERING_LLMS."""

from __future__ import annotations

import argparse
from pathlib import Path

from _model_inventory import load_answer_model_keys, load_answering_models, starts_with_capital


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print answers/ model keys that do not appear in ANSWERING_LLMS, excluding keys "
            "that start with a capital letter."
        )
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
    common_model_keys = {model.sanitized_name for model in load_answering_models(common_path)}

    missing_model_keys = [
        model_key
        for model_key in load_answer_model_keys(project_root / "answers")
        if not starts_with_capital(model_key) and model_key not in common_model_keys
    ]

    for model_key in missing_model_keys:
        print(model_key)

    return 1 if missing_model_keys else 0


if __name__ == "__main__":
    raise SystemExit(main())
