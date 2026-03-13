import json
import logging
import math
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


def _average(values: Iterable[float]) -> float:
    materialized_values = [float(value) for value in values]
    if not materialized_values:
        return 0.0
    return sum(materialized_values) / len(materialized_values)


def _jacobi_eigenvalues(
    matrix: List[List[float]],
    tolerance: float = 1e-12,
    max_iterations: int = 10_000,
) -> List[float]:
    size = len(matrix)
    if size == 0:
        return []

    working = [row[:] for row in matrix]

    for _ in range(max_iterations):
        pivot_row = -1
        pivot_col = -1
        max_off_diagonal = 0.0
        for row_index in range(size):
            for col_index in range(row_index + 1, size):
                off_diagonal = abs(working[row_index][col_index])
                if off_diagonal > max_off_diagonal:
                    max_off_diagonal = off_diagonal
                    pivot_row = row_index
                    pivot_col = col_index

        if max_off_diagonal <= tolerance or pivot_row < 0 or pivot_col < 0:
            break

        pivot_value = working[pivot_row][pivot_col]
        if abs(pivot_value) <= tolerance:
            continue

        diagonal_row = working[pivot_row][pivot_row]
        diagonal_col = working[pivot_col][pivot_col]
        tau = (diagonal_col - diagonal_row) / (2.0 * pivot_value)
        tangent = 1.0 / (abs(tau) + math.sqrt(1.0 + tau * tau))
        if tau < 0.0:
            tangent = -tangent
        cosine = 1.0 / math.sqrt(1.0 + tangent * tangent)
        sine = tangent * cosine

        for index in range(size):
            if index == pivot_row or index == pivot_col:
                continue
            value_row = working[index][pivot_row]
            value_col = working[index][pivot_col]
            working[index][pivot_row] = cosine * value_row - sine * value_col
            working[pivot_row][index] = working[index][pivot_row]
            working[index][pivot_col] = cosine * value_col + sine * value_row
            working[pivot_col][index] = working[index][pivot_col]

        working[pivot_row][pivot_row] = diagonal_row - tangent * pivot_value
        working[pivot_col][pivot_col] = diagonal_col + tangent * pivot_value
        working[pivot_row][pivot_col] = 0.0
        working[pivot_col][pivot_row] = 0.0

    eigenvalues = []
    for index in range(size):
        value = working[index][index]
        if value < 0.0 and abs(value) <= tolerance * 100:
            value = 0.0
        eigenvalues.append(max(value, 0.0))

    eigenvalues.sort(reverse=True)
    return eigenvalues


def _compute_cumulative_pca_variance(
    rows: Iterable[Dict[str, object]],
    category_keys: List[str],
) -> List[float]:
    feature_rows: List[List[float]] = []
    for row in rows:
        normalized_values = row.get("normalized")
        if not isinstance(normalized_values, dict):
            continue
        feature_rows.append([float(normalized_values[key]) for key in category_keys])

    if not feature_rows or not category_keys:
        return []

    sample_count = len(feature_rows)
    feature_count = len(category_keys)
    column_means = [
        sum(feature_rows[row_index][feature_index] for row_index in range(sample_count)) / sample_count
        for feature_index in range(feature_count)
    ]
    centered_rows = [
        [row[feature_index] - column_means[feature_index] for feature_index in range(feature_count)]
        for row in feature_rows
    ]

    covariance = [[0.0 for _ in range(feature_count)] for _ in range(feature_count)]
    for row_index in range(feature_count):
        for col_index in range(row_index, feature_count):
            covariance_value = sum(
                centered_row[row_index] * centered_row[col_index] for centered_row in centered_rows
            )
            covariance[row_index][col_index] = covariance_value
            covariance[col_index][row_index] = covariance_value

    eigenvalues = _jacobi_eigenvalues(covariance)
    total_variance = sum(eigenvalues)
    if total_variance <= 0.0:
        return []

    cumulative_variance: List[float] = []
    covered_variance = 0.0
    for eigenvalue in eigenvalues:
        covered_variance += eigenvalue / total_variance
        cumulative_variance.append(min(covered_variance, 1.0))

    return cumulative_variance


def _compute_category_entry_average(
    rows: Iterable[Dict[str, object]],
    category_keys: List[str],
    category_field: str,
) -> float:
    return _average(
        float(category_values[key])
        for row in rows
        for category_values in [row.get(category_field)]
        if isinstance(category_values, dict)
        for key in category_keys
    )


def _compute_max_per_column_sum(
    rows: Iterable[Dict[str, object]],
    category_keys: List[str],
    category_field: str,
) -> int:
    materialized_rows = list(rows)
    if not materialized_rows:
        return 0

    return sum(
        max(
            int(category_values[key])
            for row in materialized_rows
            for category_values in [row.get(category_field)]
            if isinstance(category_values, dict)
        )
        for key in category_keys
    )


def _render_single_judge_summary(
    rows: Iterable[Dict[str, object]],
    max_rows: Iterable[Dict[str, object]],
    category_keys: List[str],
) -> List[str]:
    materialized_rows = list(rows)
    materialized_max_rows = list(max_rows)

    lines = [
        "",
        "## Single-Judge Summary",
        "",
        (
            "Average first-table entry excluding **D-Bench Score**: "
            f"`{_format_decimal(_compute_category_entry_average(materialized_rows, category_keys, 'normalized'))}`"
        ),
        (
            "Average second-table entry excluding **Sum Score**: "
            f"`{_format_decimal(_compute_category_entry_average(materialized_max_rows, category_keys, 'max_by_category'))}`"
        ),
        (
            "Sum of max per column from the second table: "
            f"`{_compute_max_per_column_sum(materialized_max_rows, category_keys, 'max_by_category')}`"
        ),
        "",
        "### PCA Covered Variance (First Table)",
        "",
    ]

    cumulative_pca_variance = _compute_cumulative_pca_variance(materialized_rows, category_keys)
    if not cumulative_pca_variance:
        lines.append("No PCA variance available from the first table.")
        return lines

    lines.append("| Components | Covered Variance |")
    lines.append("| --- | --- |")
    for component_count, covered_variance in enumerate(cumulative_pca_variance, start=1):
        lines.append(f"| {component_count} | {_format_decimal(covered_variance)} |")

    return lines


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
    summary_label: Optional[str] = None,
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

    if summary_label is not None:
        max_per_category = []
        for key in category_keys:
            max_value = max(
                int(category_values[key])
                for row in materialized_rows
                for category_values in [row.get(category_field)]
                if isinstance(category_values, dict)
            )
            max_per_category.append(category_formatter(max_value))
        lines.append("| " + " | ".join([summary_label, "", *max_per_category]) + " |")

    return lines


def _render_leaderboard_markdown(
    rows: Iterable[Dict[str, object]],
    max_rows: Iterable[Dict[str, object]],
    category_keys: List[str],
    title: str,
    include_single_judge_summary: bool = False,
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
            lambda value: f"**{int(value)}**" if int(value) > 0 else str(int(value)),
            "max per column",
        )
    )
    if include_single_judge_summary:
        lines.extend(
            _render_single_judge_summary(
                materialized_rows,
                materialized_max_rows,
                category_keys,
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
                include_single_judge_summary=True,
            ),
            encoding="utf-8",
        )
        logger.info("Wrote leaderboard: %s", single_leaderboard_path)


if __name__ == "__main__":
    main()
