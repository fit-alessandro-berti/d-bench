#!/usr/bin/env python3
"""Print ANSWERING_LLMS models missing from the current OpenRouter model list."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

from _model_inventory import load_answering_models, starts_with_capital


DEFAULT_MODELS_URL = "https://openrouter.ai/api/v1/models"


def fetch_openrouter_model_ids(models_url: str, timeout_seconds: float) -> set[str]:
    request = Request(models_url, headers={"User-Agent": "d-bench-model-check"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)

    model_entries = payload.get("data")
    if not isinstance(model_entries, list):
        raise ValueError("OpenRouter model response does not contain a 'data' list.")

    model_ids = {
        model_id
        for entry in model_entries
        if isinstance(entry, dict)
        and isinstance((model_id := entry.get("id")), str)
    }
    return model_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print ANSWERING_LLMS models that implicitly target OpenRouter, do not start with "
            "a capital letter, and are no longer listed by OpenRouter."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root containing common.py.",
    )
    parser.add_argument(
        "--models-url",
        default=DEFAULT_MODELS_URL,
        help="OpenRouter models endpoint to query.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for the OpenRouter models request.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    common_path = args.project_root.resolve() / "common.py"
    available_model_ids = fetch_openrouter_model_ids(args.models_url, args.timeout_seconds)

    missing_models = [
        model.model_name
        for model in load_answering_models(common_path)
        if model.uses_openrouter
        and not starts_with_capital(model.model_name)
        and model.model_name not in available_model_ids
    ]

    for model_name in missing_models:
        print(model_name)

    return 1 if missing_models else 0


if __name__ == "__main__":
    raise SystemExit(main())
