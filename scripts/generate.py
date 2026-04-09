#!/usr/bin/env python3
"""Manual answer generation flow for a single user-supplied model name."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

try:
    import pyperclip
except ImportError as exc:
    raise SystemExit("Missing dependency 'pyperclip'. Install it with: pip install pyperclip") from exc


def sanitize_model_name(model_name: str) -> str:
    # Keep this in sync with common.sanitize_model_name without importing common.py,
    # which requires API keys at import time.
    sanitized = model_name.replace("/", "").replace(":", "")
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in sanitized)


def prompt_for_model_name() -> str:
    while True:
        model_name = input("Model name: ").strip()
        if not model_name:
            print("Model name cannot be empty.")
            continue

        sanitized = sanitize_model_name(model_name)
        if sanitized:
            return model_name

        print("Model name does not produce a usable filename key.")


def copy_prompt_to_clipboard(prompt: str) -> None:
    try:
        pyperclip.copy(prompt)
        return
    except pyperclip.PyperclipException:
        pass

    try:
        subprocess.run(
            ["clip.exe"],
            input=prompt,
            text=True,
            check=True,
        )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Could not copy the prompt to the clipboard with pyperclip or clip.exe."
        ) from exc


def to_windows_path(path: Path) -> str:
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


def open_in_notepad(path: Path, *, wait: bool) -> None:
    command = ["notepad.exe", to_windows_path(path)]
    try:
        if wait:
            subprocess.run(command, check=True)
        else:
            subprocess.Popen(command)
    except (FileNotFoundError, OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Could not open {path} in notepad.exe.") from exc


def validate_answer(answer_path: Path) -> tuple[bool, str]:
    if not answer_path.exists():
        return False, "answer file does not exist yet"

    try:
        content = answer_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False, "answer file is not valid UTF-8"
    except OSError as exc:
        return False, f"could not read answer file: {exc}"

    if not content.strip():
        return False, "answer file is empty"

    return True, ""


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("generate")

    project_root = Path(__file__).resolve().parent.parent
    questions_dir = project_root / "questions"
    answers_dir = project_root / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    question_files = sorted(path for path in questions_dir.glob("*") if path.is_file())
    logger.info("Found %d question files", len(question_files))

    llm_model = prompt_for_model_name()
    model_for_filename = sanitize_model_name(llm_model)
    logger.info("Processing model=%s", llm_model)

    for question_path in question_files:
        question_name = question_path.stem
        answer_path = answers_dir / f"{model_for_filename}_{question_name}.txt"

        is_valid, reason = validate_answer(answer_path)
        if is_valid:
            logger.info("Skipping existing answer | question=%s destination=%s", question_path.name, answer_path)
            continue
        if answer_path.exists():
            logger.info(
                "Reopening existing answer | question=%s destination=%s reason=%s",
                question_path.name,
                answer_path,
                reason,
            )
        else:
            answer_path.write_text("", encoding="utf-8")

        prompt = question_path.read_text(encoding="utf-8")

        attempt = 1
        while True:
            copy_prompt_to_clipboard(prompt)
            logger.info(
                "Prompt ready | model=%s question=%s attempt=%d destination=%s",
                llm_model,
                question_path.name,
                attempt,
                answer_path,
            )
            logger.info("Question text copied to clipboard. Fill the answer file, save it, and close Notepad.")
            open_in_notepad(answer_path, wait=True)

            is_valid, reason = validate_answer(answer_path)
            if is_valid:
                logger.info("Finished | model=%s question=%s", llm_model, question_path.name)
                break

            logger.warning(
                "Repeating question | model=%s question=%s attempt=%d reason=%s",
                llm_model,
                question_path.name,
                attempt,
                reason,
            )
            attempt += 1

    logger.info("All done.")


if __name__ == "__main__":
    main()
