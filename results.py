import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from common import ANSWERING_LLMS, EVALUATION_JSON_SCHEMA, EVALUATOR_LLMS, sanitize_model_name


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
    return f"{value:.3f}"


def _build_rows(
    sums_by_model: Dict[str, Dict[str, float]],
    file_count_by_model: Dict[str, int],
    category_keys: List[str],
    key_to_display_name: Dict[str, str],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
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
    return rows


def _build_max_rows(
    max_by_model: Dict[str, Dict[str, int]],
    file_count_by_model: Dict[str, int],
    category_keys: List[str],
    key_to_display_name: Dict[str, str],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for model_key, count in file_count_by_model.items():
        if count == 0:
            continue
        max_by_category = {key: max_by_model[model_key][key] for key in category_keys}
        sum_score = sum(max_by_category.values())
        rows.append(
            {
                "model_key": model_key,
                "model_name": key_to_display_name.get(model_key, model_key),
                "max_by_category": max_by_category,
                "sum_score": sum_score,
            }
        )

    rows.sort(key=lambda row: row["sum_score"], reverse=True)
    return rows


def _render_table_lines(
    rows: Iterable[Dict[str, object]],
    category_keys: List[str],
    score_header: str,
    category_field: str,
    score_field: str,
    score_formatter: Callable[[object], str],
    category_formatter: Callable[[object], str],
) -> List[str]:
    materialized_rows = list(rows)
    lines: List[str] = []
    if not materialized_rows:
        lines.append("No valid evaluation results found.")
        return lines

    headers = ["LLM", f"**{score_header}**"] + category_keys
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in materialized_rows:
        category_values = row.get(category_field)
        if not isinstance(category_values, dict):
            continue
        formatted_category_values = [category_formatter(category_values[key]) for key in category_keys]
        formatted_score = f"**{score_formatter(row[score_field])}**"
        lines.append(
            "| "
            + " | ".join([str(row["model_name"]), formatted_score, *formatted_category_values])
            + " |"
        )

    return lines


def _render_leaderboard_markdown(
    rows: Iterable[Dict[str, object]],
    max_rows: Iterable[Dict[str, object]],
    category_keys: List[str],
    title: str,
) -> str:
    materialized_rows = list(rows)
    materialized_max_rows = list(max_rows)
    lines = [
        title,
        "",
        "Every voice/category is normalized between `0` and `1`.",
        "The **D-Bench Score** ranges from `0` to `10`.",
        "Higher score means the model is doing more certified evil gymnastics in its responses.",
        "",
    ]
    lines.extend(
        _render_table_lines(
            materialized_rows,
            category_keys,
            "D-Bench Score",
            "normalized",
            "d_bench",
            lambda value: _format_decimal(float(value)),
            lambda value: _format_decimal(float(value)),
        )
    )
    lines.extend(
        [
            "",
            "## Maximum Per-Category Leaderboard",
            "",
            "Each category column below is the maximum raw score observed for that model across the considered files.",
            "The **Sum Score** is the sum of those category maxima, so it ranges from `0` to `100`.",
            "",
        ]
    )
    lines.extend(
        _render_table_lines(
            materialized_max_rows,
            category_keys,
            "Sum Score",
            "max_by_category",
            "sum_score",
            lambda value: str(int(value)),
            lambda value: str(int(value)),
        )
    )

    return "\n".join(lines) + "\n"


def _accumulate_results(
    evaluation_dirs: Iterable[Path],
    question_stems: List[str],
    category_keys: List[str],
    logger: logging.Logger,
) -> tuple[Dict[str, Dict[str, float]], Dict[str, int], Dict[str, Dict[str, int]]]:
    sums_by_model: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    file_count_by_model: Dict[str, int] = defaultdict(int)
    max_by_model: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

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
                max_by_model[model_key][key] = max(max_by_model[model_key][key], data[key])
            file_count_by_model[model_key] += 1

    return sums_by_model, file_count_by_model, max_by_model


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

    # Prefer original model names from ANSWERING_LLMS when available.
    key_to_display_name: Dict[str, str] = {}
    for llm_entry in ANSWERING_LLMS:
        if not llm_entry:
            continue
        model_name = llm_entry[0]
        key_to_display_name[sanitize_model_name(model_name)] = model_name
    evaluator_display_names = {folder_name: model_name for model_name, folder_name, *_ in EVALUATOR_LLMS}

    combined_sums_by_model, combined_file_count_by_model, combined_max_by_model = _accumulate_results(
        evaluation_dirs=evaluation_dirs,
        question_stems=question_stems,
        category_keys=category_keys,
        logger=logger,
    )
    rows = _build_rows(
        sums_by_model=combined_sums_by_model,
        file_count_by_model=combined_file_count_by_model,
        category_keys=category_keys,
        key_to_display_name=key_to_display_name,
    )
    max_rows = _build_max_rows(
        max_by_model=combined_max_by_model,
        file_count_by_model=combined_file_count_by_model,
        category_keys=category_keys,
        key_to_display_name=key_to_display_name,
    )
    leaderboard_path.write_text(
        _render_leaderboard_markdown(rows, max_rows, category_keys, "# D-Bench Leaderboard"),
        encoding="utf-8",
    )
    logger.info("Wrote leaderboard: %s", leaderboard_path)

    for evaluation_dir in evaluation_dirs:
        evaluator_name = evaluator_display_names.get(evaluation_dir.name, evaluation_dir.name)
        single_sums_by_model, single_file_count_by_model, single_max_by_model = _accumulate_results(
            evaluation_dirs=[evaluation_dir],
            question_stems=question_stems,
            category_keys=category_keys,
            logger=logger,
        )
        single_rows = _build_rows(
            sums_by_model=single_sums_by_model,
            file_count_by_model=single_file_count_by_model,
            category_keys=category_keys,
            key_to_display_name=key_to_display_name,
        )
        single_max_rows = _build_max_rows(
            max_by_model=single_max_by_model,
            file_count_by_model=single_file_count_by_model,
            category_keys=category_keys,
            key_to_display_name=key_to_display_name,
        )
        single_leaderboard_path = project_root / f"leaderboard_{evaluation_dir.name}.md"
        single_leaderboard_path.write_text(
            _render_leaderboard_markdown(
                single_rows,
                single_max_rows,
                category_keys,
                f"# D-Bench Leaderboard ({evaluator_name})",
            ),
            encoding="utf-8",
        )
        logger.info("Wrote leaderboard: %s", single_leaderboard_path)


if __name__ == "__main__":
    main()
