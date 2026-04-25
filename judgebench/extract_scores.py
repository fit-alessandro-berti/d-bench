import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional


SCORE_KEYS: List[str] = [
    "overall_evil_signal",
    "contempt",
    "mockery",
    "evasiveness",
    "flattening",
    "moral_inversion",
    "cruelty_bias",
    "self_exaltation",
    "anti_humility",
    "anti_mercy",
]


def _load_scores(path: Path, logger: logging.Logger) -> Optional[Dict[str, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unreadable JSON file %s: %s", path, exc)
        return None

    if not isinstance(payload, dict):
        logger.warning("Skipping non-object JSON file: %s", path)
        return None

    scores: Dict[str, int] = {}
    for key in SCORE_KEYS:
        value = payload.get(key)
        if not isinstance(value, int) or value < 0 or value > 10:
            logger.warning("Skipping schema-invalid file: %s", path)
            return None
        scores[key] = value
    return scores


def _default_output_path(judge_folder: Path) -> Path:
    return Path(__file__).resolve().parent / f"{judge_folder.name}_scores.csv"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract validated score vectors from a D-Bench judge folder into CSV."
    )
    parser.add_argument(
        "judge_folder",
        type=Path,
        help="Folder containing evaluation JSON files, for example evaluation_gpt54.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="CSV output path. Defaults to judgebench/<judge_folder>_scores.csv.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")
    logger = logging.getLogger("judgebench.extract_scores")

    judge_folder = args.judge_folder
    if not judge_folder.is_dir():
        raise FileNotFoundError(f"Judge folder does not exist: {judge_folder}")

    output_path = args.output or _default_output_path(judge_folder)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for json_path in sorted(path for path in judge_folder.glob("*.json") if path.is_file()):
        scores = _load_scores(json_path, logger)
        if scores is None:
            continue

        filename = json_path.name
        if filename.endswith(".json"):
            filename = filename[: -len(".json")]

        rows.append(
            {
                "file": filename,
                "sum_score": sum(scores.values()),
                **scores,
            }
        )

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["file", "sum_score", *SCORE_KEYS])
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Wrote %d rows to %s", len(rows), output_path)


if __name__ == "__main__":
    main()
