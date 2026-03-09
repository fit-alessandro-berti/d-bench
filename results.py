import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from common import ANSWERING_LLMS, EVALUATION_JSON_SCHEMA, sanitize_model_name


def _extract_question_stem_from_eval_name(eval_name: str, question_stems: List[str]) -> Optional[str]:
    for stem in question_stems:
        if eval_name.endswith(f"_{stem}.txt.json"):
            return stem
    return None


def _extract_model_key_from_eval_name(eval_name: str, question_stem: str) -> Optional[str]:
    suffix = f"_{question_stem}.txt.json"
    if not eval_name.endswith(suffix):
        return None
    return eval_name[: -len(suffix)]


def _format_decimal(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("results")

    project_root = Path(__file__).resolve().parent
    questions_dir = project_root / "questions"
    leaderboard_path = project_root / "leaderboard.md"

    category_keys = list(EVALUATION_JSON_SCHEMA["required"])
    question_files = sorted(path for path in questions_dir.glob("*.txt") if path.is_file())
    question_stems = sorted((path.stem for path in question_files), key=len, reverse=True)

    evaluation_dirs = sorted(
        path for path in project_root.glob("evaluation_*") if path.is_dir()
    )
    logger.info("Found %d evaluation folders", len(evaluation_dirs))

    sums_by_model: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    file_count_by_model: Dict[str, int] = defaultdict(int)

    for evaluation_dir in evaluation_dirs:
        eval_files = sorted(path for path in evaluation_dir.glob("*.json") if path.is_file())
        logger.info("Scanning %s (%d files)", evaluation_dir.name, len(eval_files))
        for eval_file in eval_files:
            question_stem = _extract_question_stem_from_eval_name(eval_file.name, question_stems)
            if question_stem is None:
                logger.warning("Skipping file with unknown question suffix: %s", eval_file.name)
                continue

            model_key = _extract_model_key_from_eval_name(eval_file.name, question_stem)
            if model_key is None:
                logger.warning("Skipping file with invalid model/question format: %s", eval_file.name)
                continue

            try:
                data = json.loads(eval_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Skipping invalid JSON file: %s", eval_file)
                continue

            if not isinstance(data, dict):
                logger.warning("Skipping non-object JSON file: %s", eval_file)
                continue

            valid = True
            for key in category_keys:
                value = data.get(key)
                if not isinstance(value, int):
                    valid = False
                    break
                if value < 0 or value > 10:
                    valid = False
                    break
            if not valid:
                logger.warning("Skipping schema-invalid file: %s", eval_file)
                continue

            for key in category_keys:
                sums_by_model[model_key][key] += data[key]
            file_count_by_model[model_key] += 1

    # Prefer original model names from ANSWERING_LLMS when available.
    key_to_display_name: Dict[str, str] = {}
    for llm_entry in ANSWERING_LLMS:
        if not llm_entry:
            continue
        model_name = llm_entry[0]
        key_to_display_name[sanitize_model_name(model_name)] = model_name

    rows = []
    for model_key, count in file_count_by_model.items():
        if count == 0:
            continue
        normalized_by_category = {
            key: sums_by_model[model_key][key] / (10 * count) for key in category_keys
        }
        d_bench_score = sum(normalized_by_category.values())
        rows.append(
            {
                "model_key": model_key,
                "model_name": key_to_display_name.get(model_key, model_key),
                "count": count,
                "normalized": normalized_by_category,
                "d_bench": d_bench_score,
            }
        )

    rows.sort(key=lambda row: row["d_bench"], reverse=True)

    lines = ["# D-Bench Leaderboard", ""]
    if not rows:
        lines.append("No valid evaluation results found.")
    else:
        headers = ["LLM"] + category_keys + ["**D-Bench Score**"]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            category_values = [_format_decimal(row["normalized"][key]) for key in category_keys]
            d_bench_value = f"**{_format_decimal(row['d_bench'])}**"
            lines.append(
                "| "
                + " | ".join([row["model_name"], *category_values, d_bench_value])
                + " |"
            )

    leaderboard_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote leaderboard: %s", leaderboard_path)


if __name__ == "__main__":
    main()
