import logging
from concurrent.futures import Future
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common import (
    EVALUATION_JSON_SCHEMA,
    EVALUATOR_LLMS,
    submit_prompt_to_chat_completions,
)


def _extract_question_stem_from_answer_name(answer_name: str, question_stems: List[str]) -> Optional[str]:
    for stem in question_stems:
        if answer_name.endswith(f"_{stem}.txt"):
            return stem
    return None


def _build_judge_prompt(protocol: str, question: str, answer: str) -> str:
    return (
        f"{protocol}\n\n"
        "PASSAGE_AND_QUESTION\n"
        f"{question}\n\n"
        "MODEL_RESPONSE\n"
        f"{answer}\n"
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("evaluate")

    project_root = Path(__file__).resolve().parent
    questions_dir = project_root / "questions"
    answers_dir = project_root / "answers"
    judge_prompt_path = project_root / "judge_prompt.txt"

    protocol = judge_prompt_path.read_text(encoding="utf-8")
    question_files = sorted(path for path in questions_dir.glob("*.txt") if path.is_file())
    question_by_stem: Dict[str, Path] = {path.stem: path for path in question_files}
    question_stems = sorted(question_by_stem.keys(), key=len, reverse=True)
    answer_files = sorted(path for path in answers_dir.glob("*.txt") if path.is_file())

    logger.info(
        "Found %d question files and %d answer files",
        len(question_files),
        len(answer_files),
    )

    pending: List[Tuple[Future, str, str]] = []

    for evaluator_entry in EVALUATOR_LLMS:
        if not evaluator_entry:
            logger.warning("Skipping empty evaluator entry: %s", evaluator_entry)
            continue

        evaluator_model = evaluator_entry[0]
        if len(evaluator_entry) < 2:
            logger.warning(
                "Skipping evaluator without target folder (expected tuple: model, folder, [kwargs]): %s",
                evaluator_entry,
            )
            continue

        evaluator_folder_name = evaluator_entry[1]
        if not isinstance(evaluator_folder_name, str) or not evaluator_folder_name.strip():
            logger.warning("Skipping evaluator with invalid folder name: %s", evaluator_entry)
            continue

        kwargs: Dict[str, object] = {}
        if len(evaluator_entry) > 2 and evaluator_entry[2] is not None:
            kwargs = dict(evaluator_entry[2])

        evaluator_folder = project_root / evaluator_folder_name
        evaluator_folder.mkdir(parents=True, exist_ok=True)
        logger.info("Processing evaluator=%s folder=%s", evaluator_model, evaluator_folder)

        for answer_path in answer_files:
            question_stem = _extract_question_stem_from_answer_name(answer_path.name, question_stems)
            if question_stem is None:
                logger.warning(
                    "Skipping answer with unknown question suffix: %s",
                    answer_path.name,
                )
                continue

            output_path = evaluator_folder / f"{answer_path.name}.json"
            if output_path.exists():
                continue

            question_text = question_by_stem[question_stem].read_text(encoding="utf-8")
            answer_text = answer_path.read_text(encoding="utf-8")
            judge_prompt = _build_judge_prompt(protocol, question_text, answer_text)

            logger.info(
                "Submitting evaluation | evaluator=%s answer=%s destination=%s",
                evaluator_model,
                answer_path.name,
                output_path,
            )
            future = submit_prompt_to_chat_completions(
                prompt=judge_prompt,
                destination_path=str(output_path),
                llm_model=evaluator_model,
                json_validation_schema=EVALUATION_JSON_SCHEMA,
                **kwargs,
            )
            pending.append((future, evaluator_model, answer_path.name))

    logger.info("Submitted %d evaluation requests. Waiting for completion.", len(pending))
    for future, evaluator_model, answer_name in pending:
        future.result()
        logger.info("Finished evaluation | evaluator=%s answer=%s", evaluator_model, answer_name)

    logger.info("All done.")


if __name__ == "__main__":
    main()
