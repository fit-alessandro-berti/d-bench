import argparse
import csv
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence


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


def _format_decimal(value: float) -> str:
    return f"{value:.3f}"


def _load_selected_files(path: Path) -> List[str]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or "file" not in reader.fieldnames:
            return []
        return [row["file"] for row in reader if row.get("file")]


def _extract_selected_file(eval_name: str, selected_files: Sequence[str]) -> Optional[str]:
    for selected_file in sorted(selected_files, key=len, reverse=True):
        suffix = f"_{selected_file}.json"
        if eval_name.endswith(suffix):
            return selected_file
    return None


def _extract_judge_name(eval_name: str, selected_file: str) -> str:
    suffix = f"_{selected_file}.json"
    if eval_name.endswith(suffix):
        return eval_name[: -len(suffix)]
    return eval_name[: -len(".json")] if eval_name.endswith(".json") else eval_name


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


def _render_markdown(rows: List[Dict[str, object]]) -> str:
    lines = [
        "# JudgeBench Results",
        "",
        "Judges are sorted by the sum of all validated scores they assigned to the selected files.",
        "",
    ]
    if not rows:
        lines.append("No valid JudgeBench evaluations found.")
        return "\n".join(lines) + "\n"

    headers = ["Judge", "Files", "Sum Score", "Average Per File", *SCORE_KEYS]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

    for row in rows:
        category_sums = row["category_sums"]
        assert isinstance(category_sums, dict)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["judge"]),
                    str(row["count"]),
                    str(int(row["sum_score"])),
                    _format_decimal(float(row["average_per_file"])),
                    *[str(int(category_sums[key])) for key in SCORE_KEYS],
                ]
            )
            + " |"
        )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render JudgeBench evaluation results as a markdown table.")
    script_dir = Path(__file__).resolve().parent
    parser.add_argument(
        "--evaluations-dir",
        type=Path,
        default=script_dir / "evaluations",
        help="Directory containing judgebench evaluation JSON files.",
    )
    parser.add_argument(
        "--selected-csv",
        type=Path,
        default=script_dir / "selected_files.csv",
        help="Selected-files CSV used to identify answer filenames in evaluation result names.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=script_dir / "results.md",
        help="Markdown output path. Defaults to judgebench/results.md.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")
    logger = logging.getLogger("judgebench.results")

    selected_files = _load_selected_files(args.selected_csv)
    if not selected_files:
        logger.warning(
            "No selected files loaded from %s. Judge names cannot be parsed robustly without it.",
            args.selected_csv,
        )

    category_sums_by_judge: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    count_by_judge: Dict[str, int] = defaultdict(int)

    for eval_path in sorted(path for path in args.evaluations_dir.glob("*.json") if path.is_file()):
        selected_file = _extract_selected_file(eval_path.name, selected_files)
        if selected_file is None:
            logger.warning("Skipping file not matching selected file list: %s", eval_path.name)
            continue

        scores = _load_scores(eval_path, logger)
        if scores is None:
            continue

        judge_name = _extract_judge_name(eval_path.name, selected_file)
        for key in SCORE_KEYS:
            category_sums_by_judge[judge_name][key] += scores[key]
        count_by_judge[judge_name] += 1

    rows: List[Dict[str, object]] = []
    for judge_name, count in count_by_judge.items():
        if count == 0:
            continue
        category_sums = {key: category_sums_by_judge[judge_name][key] for key in SCORE_KEYS}
        sum_score = sum(category_sums.values())
        rows.append(
            {
                "judge": judge_name,
                "count": count,
                "sum_score": sum_score,
                "average_per_file": sum_score / count,
                "category_sums": category_sums,
            }
        )

    rows.sort(key=lambda row: (-int(row["sum_score"]), str(row["judge"]).lower()))
    markdown = _render_markdown(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    logger.info("Wrote JudgeBench results to %s", args.output)


if __name__ == "__main__":
    main()
