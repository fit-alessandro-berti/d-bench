#!/usr/bin/env python3
"""Evaluate answers and explain nonzero evaluations with the Grok CLI.

The evaluation prompt, output layout, leaderboard filtering, and explanation
prompt match ``evaluate.py`` and ``explainer.py``. Relative paths are resolved
from the repository root.

Example:

    python scripts/grok_evaluate.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import codex_evaluate as benchmark


DEFAULT_RETRY_DELAY_SECONDS = 17.0
TARGET_MODEL = "grok-4.5"
TARGET_REASONING_EFFORT = "low"
MAX_WORKERS = 80
EVALUATION_FOLDER = Path("evaluation_grok45")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate every answer with Grok, then use Grok to explain every "
            "nonzero evaluation."
        )
    )
    parser.add_argument(
        "--answers-folder",
        type=Path,
        default=Path("answers"),
        help="Answer directory relative to the repository root (default: answers).",
    )
    parser.add_argument(
        "--questions-folder",
        type=Path,
        default=Path("questions"),
        help="Question directory relative to the repository root (default: questions).",
    )
    parser.add_argument(
        "--judge-prompt",
        type=Path,
        default=Path("judge_prompt.txt"),
        help="Judge protocol relative to the repository root (default: judge_prompt.txt).",
    )
    parser.add_argument(
        "--leaderboard",
        type=Path,
        default=Path("leaderboard.json"),
        help="Leaderboard JSON relative to the repository root (default: leaderboard.json).",
    )
    parser.add_argument(
        "--evaluation-folder",
        type=Path,
        default=EVALUATION_FOLDER,
        help=f"Evaluation output directory (default: {EVALUATION_FOLDER}).",
    )
    parser.add_argument(
        "--explanations-folder",
        type=Path,
        default=Path("explanations"),
        help="Explanation root directory (default: explanations).",
    )
    parser.add_argument(
        "--grok-command",
        default="grok",
        help="Grok CLI executable (default: grok).",
    )
    parser.add_argument(
        "--retry-delay",
        type=benchmark.non_negative_float,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help="Seconds to wait after a failed or invalid attempt (default: 17).",
    )
    return parser.parse_args(argv)


def grok_instruction(prompt: str, phase: str) -> str:
    if phase == "evaluation":
        return (
            f"{prompt}\n\n"
            "Return only the resulting JSON object, without Markdown fences or "
            "commentary. It must match this JSON schema exactly:\n"
            f"{json.dumps(benchmark.OUTPUT_SCHEMA)}"
        )
    return (
        f"{prompt}\n\n"
        "Return only the requested plain-text explanation, without a JSON or "
        "Markdown wrapper."
    )


def build_grok_command(
    grok_command: str,
    workspace: Path,
    prompt_path: Path,
) -> list[str]:
    """Build a tool-free, non-interactive Grok invocation."""
    return [
        grok_command,
        "--prompt-file",
        str(prompt_path),
        "--verbatim",
        "--model",
        TARGET_MODEL,
        "--reasoning-effort",
        TARGET_REASONING_EFFORT,
        "--cwd",
        str(workspace),
        "--output-format",
        "json",
        "--tools",
        "",
        "--max-turns",
        "1",
        "--no-auto-update",
        "--rules",
        "Do not use tools, MCP servers, plugins, skills, or subagents.",
    ]


def parse_grok_response(output: str) -> str:
    """Extract the final response text from Grok's JSON output envelope."""
    envelope = json.loads(output)
    if not isinstance(envelope, dict):
        raise ValueError("Grok output must be a JSON object")
    if envelope.get("type") == "error":
        raise ValueError(f"Grok returned an error: {envelope.get('message', '')}")

    response_text = envelope.get("text")
    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("Grok output does not contain non-empty text")
    return response_text.strip()


def run_one_attempt(
    prompt: str,
    output_path: Path,
    grok_command: str,
    phase: str,
) -> bool:
    output_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"d-bench-grok-{phase}-") as temp_dir:
        workspace = Path(temp_dir).resolve()
        prompt_path = workspace / f"{phase}_prompt.txt"
        prompt_path.write_text(grok_instruction(prompt, phase), encoding="utf-8")
        result = subprocess.run(
            build_grok_command(grok_command, workspace, prompt_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        output = result.stderr.strip() or result.stdout.strip()
        suffix = f": {output[-2000:]}" if output else ""
        benchmark.log(f"Grok exited with status {result.returncode}{suffix}")
        return False

    try:
        output_path.write_text(parse_grok_response(result.stdout), encoding="utf-8")
        if phase == "evaluation":
            normalized = benchmark.validate_evaluation(output_path)
            output_path.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        elif not benchmark.read_file_with_fallback(output_path).strip():
            raise ValueError("explanation is empty")
    except Exception as exc:
        benchmark.log(f"Invalid Grok {phase} for {output_path.name}: {exc}")
        output_path.unlink(missing_ok=True)
        return False
    return True


def run_until_valid(
    label: str,
    prompt: str,
    output_path: Path,
    grok_command: str,
    retry_delay: float,
    phase: str,
) -> bool:
    validator = (
        benchmark.remove_invalid_evaluation
        if phase == "evaluation"
        else benchmark.remove_invalid_explanation
    )
    if validator(output_path):
        benchmark.log(f"{label} Skipping valid {phase}: {output_path.name}")
        return True

    attempt = 0
    while True:
        attempt += 1
        benchmark.log(
            f"{label} Creating {phase} {output_path.name} (attempt {attempt})"
        )
        try:
            if run_one_attempt(prompt, output_path, grok_command, phase):
                benchmark.log(f"{label} Wrote valid {phase}: {output_path.name}")
                return True
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            benchmark.log(f"{label} {phase.capitalize()} attempt failed: {exc!r}")

        if retry_delay:
            benchmark.log(f"{label} Retrying in {retry_delay:g} seconds")
            time.sleep(retry_delay)


def run_phase(
    tasks: list[tuple[str, str, Path]],
    grok_command: str,
    retry_delay: float,
    phase: str,
) -> None:
    pending: list[tuple[str, str, Path]] = []
    validator = (
        benchmark.remove_invalid_evaluation
        if phase == "evaluation"
        else benchmark.remove_invalid_explanation
    )
    for label, prompt, output_path in tasks:
        if validator(output_path):
            benchmark.log(f"Skipping valid {phase}: {output_path.name}")
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pending.append((label, prompt, output_path))

    if not pending:
        benchmark.log(f"All {phase} outputs are already valid.")
        return

    benchmark.log(
        f"Running {len(pending)} pending {phase}(s) with up to "
        f"{MAX_WORKERS} concurrent Grok process(es)"
    )
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                run_until_valid,
                f"[{index}/{len(pending)}] {label}",
                prompt,
                output_path,
                grok_command,
                retry_delay,
                phase,
            ): output_path
            for index, (label, prompt, output_path) in enumerate(pending, start=1)
        }
        for future in as_completed(futures):
            future.result()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    answers_folder = benchmark.resolve_from_repo(args.answers_folder)
    questions_folder = benchmark.resolve_from_repo(args.questions_folder)
    judge_prompt_path = benchmark.resolve_from_repo(args.judge_prompt)
    leaderboard_path = benchmark.resolve_from_repo(args.leaderboard)
    evaluation_folder = benchmark.resolve_from_repo(args.evaluation_folder)
    explanations_folder = benchmark.resolve_from_repo(args.explanations_folder)

    for label, path in (
        ("Answers folder", answers_folder),
        ("Questions folder", questions_folder),
    ):
        if not path.is_dir():
            print(f"{label} does not exist: {path}", file=sys.stderr)
            return 2
    if not judge_prompt_path.is_file():
        print(f"Judge prompt does not exist: {judge_prompt_path}", file=sys.stderr)
        return 2
    if shutil.which(args.grok_command) is None:
        print(f"Grok CLI executable was not found: {args.grok_command}", file=sys.stderr)
        return 2

    evaluation_folder.mkdir(parents=True, exist_ok=True)
    benchmark.log(
        f"Grok model={TARGET_MODEL!r}, reasoning_effort="
        f"{TARGET_REASONING_EFFORT!r}, max_workers={MAX_WORKERS}"
    )
    evaluation_tasks = benchmark.build_evaluation_tasks(
        answers_folder,
        questions_folder,
        judge_prompt_path,
        leaderboard_path,
        evaluation_folder,
    )
    benchmark.log(f"Found {len(evaluation_tasks)} expected evaluation file(s)")
    run_phase(
        evaluation_tasks,
        args.grok_command,
        args.retry_delay,
        "evaluation",
    )

    explanation_tasks = benchmark.build_explanation_tasks(
        answers_folder,
        questions_folder,
        evaluation_folder,
        explanations_folder,
    )
    benchmark.log(f"Found {len(explanation_tasks)} nonzero evaluation(s) to explain")
    run_phase(
        explanation_tasks,
        args.grok_command,
        args.retry_delay,
        "explanation",
    )
    benchmark.log("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
