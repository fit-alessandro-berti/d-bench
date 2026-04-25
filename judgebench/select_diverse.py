import argparse
import csv
import logging
import math
import re
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


QUESTION_SUFFIX_RE = re.compile(r"_(q\d+)\.txt$")


def _parse_float(row: Dict[str, str], key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric value for {key!r} in row: {row}") from exc


def _question_stem(filename: str) -> Optional[str]:
    match = QUESTION_SUFFIX_RE.search(filename)
    if match is None:
        return None
    return match.group(1)


def _model_key(filename: str) -> str:
    question_stem = _question_stem(filename)
    if question_stem is None:
        return filename
    suffix = f"_{question_stem}.txt"
    return filename[: -len(suffix)]


def _euclidean_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if not left:
        return 0.0
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right))) / math.sqrt(len(left))


def _diversity_score(candidate: Dict[str, object], selected: Sequence[Dict[str, object]]) -> float:
    if not selected:
        return 1.0

    candidate_vector = candidate["vector"]
    assert isinstance(candidate_vector, list)
    min_distance = min(
        _euclidean_distance(candidate_vector, selected_row["vector"])  # type: ignore[arg-type]
        for selected_row in selected
    )

    selected_models = {str(row["model_key"]) for row in selected}
    selected_questions = {str(row["question_stem"]) for row in selected}
    model_bonus = 0.15 if str(candidate["model_key"]) not in selected_models else 0.0
    question_bonus = 0.10 if str(candidate["question_stem"]) not in selected_questions else 0.0
    return min(1.0, min_distance + model_bonus + question_bonus)


def _load_rows(input_path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    with input_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            return rows
        missing = [key for key in ["file", "sum_score", *SCORE_KEYS] if key not in reader.fieldnames]
        if missing:
            raise ValueError(f"Input CSV is missing required columns: {', '.join(missing)}")

        for row in reader:
            filename = row["file"]
            scores = [_parse_float(row, key) for key in SCORE_KEYS]
            rows.append(
                {
                    "file": filename,
                    "sum_score": _parse_float(row, "sum_score"),
                    "vector": [score / 10.0 for score in scores],
                    "model_key": _model_key(filename),
                    "question_stem": _question_stem(filename) or "",
                }
            )
    return rows


def _select_rows(
    rows: Sequence[Dict[str, object]],
    limit: int,
    candidate_pool_size: int,
    score_weight: float,
    max_per_model: int,
) -> List[Dict[str, object]]:
    if limit <= 0:
        return []
    if not rows:
        return []
    if score_weight < 0.0 or score_weight > 1.0:
        raise ValueError("score_weight must be between 0 and 1.")
    if max_per_model <= 0:
        raise ValueError("max_per_model must be at least 1.")

    max_sum = max(float(row["sum_score"]) for row in rows)
    min_sum = min(float(row["sum_score"]) for row in rows)
    score_range = max(max_sum - min_sum, 1.0)

    ranked = sorted(
        rows,
        key=lambda row: (-float(row["sum_score"]), str(row["file"])),
    )
    pool_size = min(len(ranked), max(limit, candidate_pool_size))
    candidate_pool = ranked[:pool_size]

    selected: List[Dict[str, object]] = []
    remaining = list(candidate_pool)
    diversity_weight = 1.0 - score_weight

    while remaining and len(selected) < limit:
        best_row: Optional[Dict[str, object]] = None
        best_selection_score = -1.0
        best_diversity = 0.0
        selected_counts_by_model: Dict[str, int] = {}
        for selected_row in selected:
            model_key = str(selected_row["model_key"])
            selected_counts_by_model[model_key] = selected_counts_by_model.get(model_key, 0) + 1

        for row in remaining:
            if selected_counts_by_model.get(str(row["model_key"]), 0) >= max_per_model:
                continue

            normalized_sum = (float(row["sum_score"]) - min_sum) / score_range
            diversity = _diversity_score(row, selected)
            selection_score = score_weight * normalized_sum + diversity_weight * diversity
            if selection_score > best_selection_score:
                best_row = row
                best_selection_score = selection_score
                best_diversity = diversity

        if best_row is None:
            break

        best_row["selection_score"] = best_selection_score
        best_row["diversity_score"] = best_diversity
        selected.append(best_row)
        remaining = [row for row in remaining if row is not best_row]

    return selected


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Select high-scoring answer files while greedily maximizing score-vector, "
            "model, and question diversity."
        )
    )
    parser.add_argument("input_csv", type=Path, help="CSV produced by extract_scores.py.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "selected_files.csv",
        help="Output CSV path. Defaults to judgebench/selected_files.csv.",
    )
    parser.add_argument("--limit", type=int, default=15, help="Number of files to select.")
    parser.add_argument(
        "--candidate-pool-size",
        type=int,
        default=100,
        help="Only the top N rows by sum_score are considered for diversity selection.",
    )
    parser.add_argument(
        "--score-weight",
        type=float,
        default=0.65,
        help="Tradeoff between high scores and diversity. 1.0 means score-only.",
    )
    parser.add_argument(
        "--max-per-model",
        type=int,
        default=1,
        help="Maximum selected files per answering LLM. Defaults to 1.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")
    logger = logging.getLogger("judgebench.select_diverse")

    rows = _load_rows(args.input_csv)
    selected = _select_rows(
        rows=rows,
        limit=args.limit,
        candidate_pool_size=args.candidate_pool_size,
        score_weight=args.score_weight,
        max_per_model=args.max_per_model,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "rank",
                "file",
                "answering_llm",
                "sum_score",
                "selection_score",
                "diversity_score",
            ],
        )
        writer.writeheader()
        for index, row in enumerate(selected, start=1):
            writer.writerow(
                {
                    "rank": index,
                    "file": row["file"],
                    "answering_llm": row["model_key"],
                    "sum_score": f"{float(row['sum_score']):.6g}",
                    "selection_score": f"{float(row['selection_score']):.6f}",
                    "diversity_score": f"{float(row['diversity_score']):.6f}",
                }
            )

    logger.info("Wrote %d selected files to %s", len(selected), args.output)


if __name__ == "__main__":
    main()
