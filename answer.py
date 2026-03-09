import logging
from concurrent.futures import Future
from pathlib import Path
from typing import Dict, List, Tuple

from common import ANSWERING_LLMS, sanitize_model_name, submit_prompt_to_chat_completions


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("answer")

    project_root = Path(__file__).resolve().parent
    questions_dir = project_root / "questions"
    answers_dir = project_root / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    question_files = sorted(path for path in questions_dir.glob("*") if path.is_file())
    logger.info("Found %d question files", len(question_files))

    pending: List[Tuple[Future, str, str]] = []

    for llm_entry in ANSWERING_LLMS:
        if not llm_entry:
            logger.warning("Skipping empty llm entry: %s", llm_entry)
            continue

        llm_model = llm_entry[0]
        kwargs: Dict[str, object] = {}
        if len(llm_entry) > 1 and llm_entry[1] is not None:
            kwargs = dict(llm_entry[1])

        model_for_filename = sanitize_model_name(llm_model)
        logger.info("Processing model=%s", llm_model)

        for question_path in question_files:
            question_name = question_path.stem
            answer_path = answers_dir / f"{model_for_filename}_{question_name}.txt"

            if answer_path.exists():
                continue

            prompt = question_path.read_text(encoding="utf-8")
            logger.info(
                "Submitting | model=%s question=%s destination=%s",
                llm_model,
                question_path.name,
                answer_path,
            )
            future = submit_prompt_to_chat_completions(
                prompt=prompt,
                destination_path=str(answer_path),
                llm_model=llm_model,
                **kwargs,
            )
            pending.append((future, llm_model, question_path.name))

    logger.info("Submitted %d requests. Waiting for completion.", len(pending))
    for future, llm_model, question_file in pending:
        future.result()
        logger.info("Finished | model=%s question=%s", llm_model, question_file)

    logger.info("All done.")


if __name__ == "__main__":
    main()
