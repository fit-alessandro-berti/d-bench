import argparse
import csv
import json
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from jsonschema import validate


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_SLEEP_SECONDS = 15
MAX_CONCURRENT_THREADS = 25
REQUEST_TIMEOUT_SECONDS = 600

# Edit this list the same way ANSWERING_LLMS is edited in the main benchmark:
# ("provider/model",) uses OpenRouter by default.
# ("model", {"api_url": "...", "api_key": "...", "additional_payload": {...}}) overrides it.
# ("model", {"manual": True}) copies each prompt to the clipboard and opens Notepad
# for manual JSON entry instead of calling an API.
JUDGE_LLMS: Sequence[Tuple[Any, ...]] = [
    ("gpt-5.4",  {"api_url": "https://api.openai.com/v1/responses", "api_key": os.environ["OPENAI_API_KEY"],
         "additional_payload": {"reasoning": {"effort": "none"}}
         },),
    ("gpt-5.5", {"api_url": "https://api.openai.com/v1/responses", "api_key": os.environ["OPENAI_API_KEY"],
                 "additional_payload": {"reasoning": {"effort": "none"}}
                 },),
    ("gpt-5.5-2026-04-23", {"api_url": "https://api.openai.com/v1/responses", "api_key": os.environ["OPENAI_API_KEY"],
                 "additional_payload": {"reasoning": {"effort": "medium"}}
                 },),
    (
        "grok-4.20-0309-non-reasoning",
        {"api_url": "https://api.x.ai/v1/responses", "api_key": os.environ["GROK_API_KEY"]},
    ),
    ("anthropic/claude-opus-4.7",),
    ("google/gemini-3.1-pro-preview", {
        "additional_payload": {"reasoning": {"effort": "minimal"}}
    }),
    ("gpt-4.5-preview", {"manual": True}),
    (
        "deepseek/deepseek-v4-pro",
        {"api_url": "https://openrouter.ai/api/v1/chat/completions", "api_key": os.environ["OPENROUTER_API_KEY"],
         "additional_payload": {"reasoning": {"enabled": False}}}
    )
]

EVALUATION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
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
    ],
    "properties": {
        "overall_evil_signal": {"type": "integer", "minimum": 0, "maximum": 10},
        "contempt": {"type": "integer", "minimum": 0, "maximum": 10},
        "mockery": {"type": "integer", "minimum": 0, "maximum": 10},
        "evasiveness": {"type": "integer", "minimum": 0, "maximum": 10},
        "flattening": {"type": "integer", "minimum": 0, "maximum": 10},
        "moral_inversion": {"type": "integer", "minimum": 0, "maximum": 10},
        "cruelty_bias": {"type": "integer", "minimum": 0, "maximum": 10},
        "self_exaltation": {"type": "integer", "minimum": 0, "maximum": 10},
        "anti_humility": {"type": "integer", "minimum": 0, "maximum": 10},
        "anti_mercy": {"type": "integer", "minimum": 0, "maximum": 10},
    },
}

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_THREADS)
_write_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def sanitize_model_name(model_name: str) -> str:
    sanitized = model_name.replace("/", "").replace(":", "")
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in sanitized)


def _extract_json_from_fenced_block(text: str) -> Dict[str, Any]:
    match = re.search(r"```json\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        raise ValueError("Response does not contain a ```json ... ``` block.")
    parsed = json.loads(match.group(1))
    if not isinstance(parsed, dict):
        raise ValueError("Fenced JSON payload is not an object.")
    return parsed


def _extract_manual_json(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Response file is empty.")

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = _extract_json_from_fenced_block(stripped)

    if not isinstance(parsed, dict):
        raise ValueError("JSON payload is not an object.")
    return parsed


def _write_json(destination_path: Path, parsed_json: Dict[str, Any]) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        destination_path.write_text(json.dumps(parsed_json, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_text_from_api_response(response_json: Dict[str, Any], is_responses_api: bool) -> str:
    if not is_responses_api:
        content = response_json["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content
        return str(content)

    output_text = response_json.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = response_json.get("output")
    if isinstance(output, list):
        text_chunks = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content")
            if not isinstance(content_items, list):
                continue
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue
                text_value = content_item.get("text")
                if isinstance(text_value, str):
                    text_chunks.append(text_value)
        if text_chunks:
            return "".join(text_chunks)

    raise ValueError("Could not extract text content from responses API payload.")


def _submit_and_write_with_retries(
    prompt: str,
    destination_path: str,
    llm_model: str,
    api_url: str,
    api_key: str,
    additional_payload: Optional[Dict[str, Any]],
) -> None:
    is_responses_api = "/responses" in api_url.lower()
    if is_responses_api:
        payload: Dict[str, Any] = {"model": llm_model, "input": prompt}
    else:
        payload = {"model": llm_model, "messages": [{"role": "user", "content": prompt}]}
    if additional_payload:
        payload.update(additional_payload)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    attempt = 0
    while True:
        attempt += 1
        started = time.time_ns()
        try:
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content = _extract_text_from_api_response(response.json(), is_responses_api)
            if not content.strip():
                raise ValueError("Received empty response content from model.")

            parsed_json = _extract_json_from_fenced_block(content)
            validate(instance=parsed_json, schema=EVALUATION_JSON_SCHEMA)
            _write_json(Path(destination_path), parsed_json)

            elapsed = (time.time_ns() - started) / 10**9
            _logger.info(
                "Completed request | model=%s destination=%s attempt=%d response_time_s=%.3f",
                llm_model,
                destination_path,
                attempt,
                elapsed,
            )
            return
        except Exception as exc:
            elapsed = (time.time_ns() - started) / 10**9
            _logger.warning(
                "Request failed | model=%s destination=%s attempt=%d response_time_s=%.3f error=%s",
                llm_model,
                destination_path,
                attempt,
                elapsed,
                exc,
            )
            time.sleep(RETRY_SLEEP_SECONDS)


def submit_prompt_to_chat_completions(
    prompt: str,
    destination_path: str,
    llm_model: str,
    api_url: str = OPENROUTER_CHAT_COMPLETIONS_URL,
    api_key: Optional[str] = None,
    additional_payload: Optional[Dict[str, Any]] = None,
) -> Future:
    if api_key is None:
        api_key = os.environ["OPENROUTER_API_KEY"]
    return _executor.submit(
        _submit_and_write_with_retries,
        prompt,
        destination_path,
        llm_model,
        api_url,
        api_key,
        additional_payload,
    )


def _copy_prompt_to_clipboard(prompt: str) -> None:
    try:
        import pyperclip
    except ImportError as exc:
        raise RuntimeError("Manual JudgeBench requires pyperclip. Install it with: pip install pyperclip") from exc

    try:
        pyperclip.copy(prompt)
    except pyperclip.PyperclipException as exc:
        raise RuntimeError("Could not copy the JudgeBench prompt to the clipboard with pyperclip.") from exc


def _to_windows_path(path: Path) -> str:
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(path)],
            capture_output=True,
            check=True,
            text=True,
        )
        windows_path = result.stdout.strip()
        if windows_path:
            return windows_path
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        pass

    return str(path)


def _open_in_notepad(path: Path) -> None:
    try:
        subprocess.run(["notepad.exe", _to_windows_path(path)], check=True)
    except (FileNotFoundError, OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Could not open {path} in notepad.exe.") from exc


def _validate_evaluation_file(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    if not path.exists():
        return None, "evaluation file does not exist yet"

    try:
        content = path.read_text(encoding="utf-8")
        parsed_json = _extract_manual_json(content)
        validate(instance=parsed_json, schema=EVALUATION_JSON_SCHEMA)
    except Exception as exc:
        return None, str(exc)

    return parsed_json, ""


def _submit_manual_evaluation(
    prompt: str,
    destination_path: Path,
    judge_model: str,
    selected_file: str,
    logger: logging.Logger,
) -> None:
    if not destination_path.exists():
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text("", encoding="utf-8")

    attempt = 0
    while True:
        attempt += 1
        _copy_prompt_to_clipboard(prompt)
        logger.info(
            "Manual JudgeBench prompt copied | judge=%s answer=%s attempt=%d destination=%s",
            judge_model,
            selected_file,
            attempt,
            destination_path,
        )
        logger.info("Paste the model's JSON into Notepad, save the file, and close Notepad.")
        _open_in_notepad(destination_path)

        parsed_json, reason = _validate_evaluation_file(destination_path)
        if parsed_json is not None:
            _write_json(destination_path, parsed_json)
            logger.info("Finished manual JudgeBench evaluation | judge=%s answer=%s", judge_model, selected_file)
            return

        logger.warning(
            "Manual JudgeBench JSON invalid; repeating prompt | judge=%s answer=%s attempt=%d reason=%s",
            judge_model,
            selected_file,
            attempt,
            reason,
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


def _load_selected_files(path: Path) -> List[str]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or "file" not in reader.fieldnames:
            raise ValueError(f"Selected CSV must contain a 'file' column: {path}")
        return [row["file"] for row in reader if row.get("file")]


def _parse_judge_entries(only: Optional[str]) -> Sequence[Tuple[Any, ...]]:
    if only is None:
        return JUDGE_LLMS
    requested = {value.strip() for value in only.split(",") if value.strip()}
    return tuple(entry for entry in JUDGE_LLMS if entry and entry[0] in requested)


def _parse_judge_options(judge_entry: Tuple[Any, ...]) -> Tuple[Dict[str, object], bool]:
    kwargs: Dict[str, object] = {}
    manual = False

    for option in judge_entry[1:]:
        if option is None:
            continue
        if isinstance(option, str) and option.lower() == "manual":
            manual = True
            continue
        if isinstance(option, dict):
            kwargs.update(option)
            continue
        raise ValueError(f"Unsupported judge option for {judge_entry[0]}: {option!r}")

    manual = bool(kwargs.pop("manual", False)) or manual
    return kwargs, manual


def main() -> None:
    parser = argparse.ArgumentParser(description="Run JudgeBench judges against the selected answer files.")
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    parser.add_argument(
        "--selected-csv",
        type=Path,
        default=script_dir / "selected_files.csv",
        help="CSV containing a 'file' column. Defaults to judgebench/selected_files.csv.",
    )
    parser.add_argument(
        "--answers-dir",
        type=Path,
        default=script_dir / "answers",
        help="Selected benchmark answers directory. Defaults to judgebench/answers.",
    )
    parser.add_argument(
        "--questions-dir",
        type=Path,
        default=project_root / "questions",
        help="Main benchmark questions directory.",
    )
    parser.add_argument(
        "--judge-prompt",
        type=Path,
        default=project_root / "judge_prompt.txt",
        help="Main benchmark judge prompt.",
    )
    parser.add_argument(
        "--evaluations-dir",
        type=Path,
        default=script_dir / "evaluations",
        help="Output directory for JudgeBench evaluation JSON files.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated exact model names from JUDGE_LLMS to run.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("judgebench.evaluate")

    protocol = args.judge_prompt.read_text(encoding="utf-8")
    question_files = sorted(path for path in args.questions_dir.glob("*.txt") if path.is_file())
    question_by_stem: Dict[str, Path] = {path.stem: path for path in question_files}
    question_stems = sorted(question_by_stem.keys(), key=len, reverse=True)
    selected_files = _load_selected_files(args.selected_csv)
    judge_entries = _parse_judge_entries(args.only)

    if not judge_entries:
        raise ValueError("No judges selected.")

    args.evaluations_dir.mkdir(parents=True, exist_ok=True)
    pending: List[Tuple[Future, str, str]] = []

    for judge_entry in judge_entries:
        if not judge_entry:
            logger.warning("Skipping empty judge entry: %s", judge_entry)
            continue
        judge_model = judge_entry[0]
        kwargs, is_manual = _parse_judge_options(judge_entry)

        judge_key = sanitize_model_name(judge_model)
        for selected_file in selected_files:
            answer_path = args.answers_dir / selected_file
            if not answer_path.is_file():
                logger.warning("Skipping missing answer file: %s", answer_path)
                continue

            question_stem = _extract_question_stem_from_answer_name(selected_file, question_stems)
            if question_stem is None:
                logger.warning("Skipping answer with unknown question suffix: %s", selected_file)
                continue

            output_path = args.evaluations_dir / f"{judge_key}_{selected_file}.json"
            if output_path.exists():
                if not is_manual:
                    continue

                parsed_json, reason = _validate_evaluation_file(output_path)
                if parsed_json is not None:
                    continue

                logger.info(
                    "Reopening existing manual JudgeBench output | judge=%s answer=%s destination=%s reason=%s",
                    judge_model,
                    selected_file,
                    output_path,
                    reason,
                )

            question_text = question_by_stem[question_stem].read_text(encoding="utf-8")
            answer_text = answer_path.read_text(encoding="utf-8")
            judge_prompt = _build_judge_prompt(protocol, question_text, answer_text)

            if is_manual:
                _submit_manual_evaluation(
                    prompt=judge_prompt,
                    destination_path=output_path,
                    judge_model=judge_model,
                    selected_file=selected_file,
                    logger=logger,
                )
                continue

            logger.info(
                "Submitting JudgeBench evaluation | judge=%s answer=%s destination=%s",
                judge_model,
                selected_file,
                output_path,
            )
            future = submit_prompt_to_chat_completions(
                prompt=judge_prompt,
                destination_path=str(output_path),
                llm_model=judge_model,
                **kwargs,
            )
            pending.append((future, judge_model, selected_file))

    if pending:
        logger.info("Submitted %d JudgeBench requests. Waiting for completion.", len(pending))
        for future, judge_model, selected_file in pending:
            future.result()
            logger.info("Finished JudgeBench evaluation | judge=%s answer=%s", judge_model, selected_file)

    logger.info("All done.")


if __name__ == "__main__":
    main()
