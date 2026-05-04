#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent
MODELS_CONFIG_PATH = REPO_ROOT / "models.json"

PROVIDER_API_URLS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "google": "https://generativelanguage.googleapis.com/v1beta/chat/completions",
    "claude": "https://api.anthropic.com/v1/messages",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "grok": "https://api.x.ai/v1/chat/completions",
    "x-ai": "https://api.x.ai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "deepinfra": "https://api.deepinfra.com/v1/openai/chat/completions",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
    "perplexity": "https://api.perplexity.ai/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
}

PROVIDER_API_KEY_ENVS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "grok": "GROK_API_KEY",
    "x-ai": "GROK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "qwen": "QWEN_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "groq": "GROQ_API_KEY",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute d-bench for one target model.")
    parser.add_argument("model_name", help="Model alias to benchmark.")
    parser.add_argument("--provider", default="openrouter", help="Model provider. Defaults to openrouter.")
    parser.add_argument("--base-model", help="Underlying API model. Defaults to model_name.")
    parser.add_argument("--alias", help="Alias used inside the benchmark. Defaults to model_name.")
    parser.add_argument("--api-url", help="Override API URL.")
    parser.add_argument("--api-key-env", help="Environment variable containing the API key.")
    parser.add_argument("--api-key-file", help="Path to a file containing the API key.")
    parser.add_argument("--reasoning-effort", help="Optional reasoning effort.")
    parser.add_argument("--reasoning-enabled", action="store_true", help="Set additional_payload.reasoning.enabled=true.")
    parser.add_argument("--thinking-tokens", type=int, help="Accepted for CLI compatibility; unused here.")
    parser.add_argument("--temperature", type=float, help="Optional sampling temperature.")
    parser.add_argument("--max-tokens", type=int, help="Optional max token cap.")
    parser.add_argument("--system-prompt", help="Accepted for CLI compatibility; unused here.")
    parser.add_argument("--add-prompt", help="Accepted for CLI compatibility; unused here.")
    parser.add_argument("--payload-json", help="JSON object merged into additional_payload.")
    parser.add_argument("--tools-json", help="Accepted for CLI compatibility; unused here.")
    parser.add_argument("--config-json", help="Extra JSON object merged into the config.")
    parser.add_argument("--config-file", help="Path to a JSON file merged into the config.")
    parser.add_argument("--python", default=sys.executable, help="Python executable for subprocess phases.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing them.")
    return parser


def merge_dicts(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_json_object(raw: str | None, label: str) -> Dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must decode to a JSON object.")
    return parsed


def load_runtime_config(args: argparse.Namespace) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    if args.config_file:
        with open(args.config_file, "r", encoding="utf-8") as handler:
            file_config = json.load(handler)
        if not isinstance(file_config, dict):
            raise ValueError("config-file must contain a JSON object.")
        config = merge_dicts(config, file_config)
    config = merge_dicts(config, parse_json_object(args.config_json, "config-json"))
    if args.payload_json:
        config["additional_payload"] = merge_dicts(
            config.get("additional_payload", {}),
            parse_json_object(args.payload_json, "payload-json"),
        )

    config.setdefault("provider", args.provider)
    config.setdefault("alias", args.alias or args.model_name)
    config.setdefault("base_model", args.base_model or args.model_name)
    config.setdefault("api_url", args.api_url or PROVIDER_API_URLS.get(config["provider"]))
    config.setdefault("api_key_env", args.api_key_env or PROVIDER_API_KEY_ENVS.get(config["provider"]))
    if args.api_key_file:
        config["api_key_file"] = args.api_key_file
    if args.reasoning_effort is not None:
        config.setdefault("additional_payload", {})
        config["additional_payload"].setdefault("reasoning", {})
        config["additional_payload"]["reasoning"]["effort"] = args.reasoning_effort
    if args.reasoning_enabled:
        config.setdefault("additional_payload", {})
        config["additional_payload"].setdefault("reasoning", {})
        config["additional_payload"]["reasoning"]["enabled"] = True
    if args.temperature is not None:
        config.setdefault("additional_payload", {})
        config["additional_payload"]["temperature"] = args.temperature
    if args.max_tokens is not None:
        config.setdefault("additional_payload", {})
        config["additional_payload"]["max_tokens"] = args.max_tokens

    return config


def build_registration_entry(config: Dict[str, Any]) -> list[Any]:
    entry_parameters: Dict[str, Any] = {}
    if config.get("api_url"):
        entry_parameters["api_url"] = config["api_url"]
    if config.get("api_key_env"):
        entry_parameters["api_key_env"] = config["api_key_env"]
    if config.get("additional_payload"):
        entry_parameters["additional_payload"] = config["additional_payload"]
    if config["base_model"] != config["alias"]:
        entry_parameters["base_model"] = config["base_model"]
    if config.get("api_key_file"):
        with open(config["api_key_file"], "r", encoding="utf-8") as handler:
            entry_parameters["api_key"] = handler.read().strip()
    if not entry_parameters:
        return [config["alias"]]
    return [config["alias"], entry_parameters]


def ensure_model_registered(config: Dict[str, Any], dry_run: bool) -> None:
    with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as handler:
        models_config = json.load(handler)

    answering_llms = models_config.setdefault("answering_llms", [])
    alias = config["alias"]
    for entry in answering_llms:
        if isinstance(entry, list) and entry and entry[0] == alias:
            return

    answering_llms.append(build_registration_entry(config))
    if dry_run:
        return

    with open(MODELS_CONFIG_PATH, "w", encoding="utf-8") as handler:
        json.dump(models_config, handler, indent=2)
        handler.write("\n")


def build_answer_kwargs(config: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if config.get("api_url"):
        kwargs["api_url"] = config["api_url"]
    if config.get("api_key_env"):
        kwargs["api_key_env"] = config["api_key_env"]
    if config.get("additional_payload"):
        kwargs["additional_payload"] = config["additional_payload"]
    if config.get("api_key_file"):
        with open(config["api_key_file"], "r", encoding="utf-8") as handler:
            kwargs["api_key"] = handler.read().strip()
    return kwargs


def run_subprocess(command: list[str], cwd: Path, dry_run: bool) -> None:
    print("+", " ".join(command))
    if dry_run:
        return
    subprocess.run(command, cwd=str(cwd), check=True)


def run_answers(config: Dict[str, Any], common_module: Any, answer_module: Any) -> None:
    project_root = REPO_ROOT
    questions_dir = project_root / "questions"
    answers_dir = project_root / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)

    answer_kwargs = build_answer_kwargs(config)
    question_files = sorted(path for path in questions_dir.glob("*") if path.is_file())
    futures: List[Tuple[Any, str]] = []
    sanitized_alias = common_module.sanitize_model_name(config["alias"])
    request_model = config["base_model"]

    for question_path in question_files:
        destination = answers_dir / f"{sanitized_alias}_{question_path.stem}.txt"
        if destination.exists():
            continue
        prompt = question_path.read_text(encoding="utf-8")
        future = common_module.submit_prompt_to_chat_completions(
            prompt=prompt,
            destination_path=str(destination),
            llm_model=request_model,
            **answer_kwargs,
        )
        futures.append((future, question_path.name))

    for future, question_name in futures:
        future.result()
        print("finished answer", question_name)


def run_evaluations(config: Dict[str, Any], common_module: Any, evaluate_module: Any) -> None:
    logger = evaluate_module.logging.getLogger("cli_execute_evaluate")
    project_root = REPO_ROOT
    questions_dir = project_root / "questions"
    answers_dir = project_root / "answers"
    judge_prompt_path = project_root / "judge_prompt.txt"
    leaderboard_path = project_root / "leaderboard.json"

    protocol = judge_prompt_path.read_text(encoding="utf-8")
    question_files = sorted(path for path in questions_dir.glob("*.txt") if path.is_file())
    question_by_stem = {path.stem: path for path in question_files}
    question_stems = sorted(question_by_stem.keys(), key=len, reverse=True)
    alias_key = common_module.sanitize_model_name(config["alias"])
    answer_files = sorted(path for path in answers_dir.glob(f"{alias_key}_*.txt") if path.is_file())
    leaderboard_scores_by_model = evaluate_module._load_leaderboard_scores(leaderboard_path, logger)
    model_score = leaderboard_scores_by_model.get(alias_key, evaluate_module.DEFAULT_D_BENCH_SCORE_FOR_NEW_MODELS)
    if not evaluate_module._should_evaluate_score(model_score):
        print("skipping evaluations because model score is outside the configured evaluation band")
        return

    pending: List[Tuple[Any, str, str]] = []
    for evaluator_entry in common_module.EVALUATOR_LLMS:
        evaluator_model = evaluator_entry[0]
        evaluator_folder_name = evaluator_entry[1]
        kwargs = dict(evaluator_entry[2]) if len(evaluator_entry) > 2 and evaluator_entry[2] is not None else {}
        evaluator_folder = project_root / evaluator_folder_name
        evaluator_folder.mkdir(parents=True, exist_ok=True)

        for answer_path in answer_files:
            question_stem = evaluate_module._extract_question_stem_from_answer_name(answer_path.name, question_stems)
            if question_stem is None:
                continue
            output_path = evaluator_folder / f"{answer_path.name}.json"
            if output_path.exists():
                continue
            question_text = question_by_stem[question_stem].read_text(encoding="utf-8")
            answer_text = answer_path.read_text(encoding="utf-8")
            judge_prompt = evaluate_module._build_judge_prompt(protocol, question_text, answer_text)
            future = common_module.submit_prompt_to_chat_completions(
                prompt=judge_prompt,
                destination_path=str(output_path),
                llm_model=evaluator_model,
                json_validation_schema=common_module.EVALUATION_JSON_SCHEMA,
                **kwargs,
            )
            pending.append((future, evaluator_model, answer_path.name))

    for future, evaluator_model, answer_name in pending:
        future.result()
        print("finished evaluation", evaluator_model, answer_name)


def run_explanations(config: Dict[str, Any], common_module: Any, explainer_module: Any) -> None:
    project_root = REPO_ROOT
    questions_dir = project_root / "questions"
    answers_dir = project_root / "answers"
    explanations_root = project_root / "explanations"
    explanations_root.mkdir(parents=True, exist_ok=True)

    question_files = sorted(path for path in questions_dir.glob("*.txt") if path.is_file())
    question_by_stem = {path.stem: path for path in question_files}
    question_stems = sorted(question_by_stem.keys(), key=len, reverse=True)
    alias_key = common_module.sanitize_model_name(config["alias"])

    pending: List[Tuple[Any, str, str]] = []
    for evaluator_entry in common_module.EVALUATOR_LLMS:
        evaluator_model = evaluator_entry[0]
        evaluator_folder_name = evaluator_entry[1]
        kwargs = dict(evaluator_entry[2]) if len(evaluator_entry) > 2 and evaluator_entry[2] is not None else {}
        evaluation_dir = project_root / evaluator_folder_name
        if not evaluation_dir.exists():
            continue
        explanations_dir = explanations_root / evaluator_folder_name
        explanations_dir.mkdir(parents=True, exist_ok=True)

        for evaluation_path in sorted(evaluation_dir.glob(f"{alias_key}_*.txt.json")):
            evaluation_json = json.loads(evaluation_path.read_text(encoding="utf-8"))
            if not isinstance(evaluation_json, dict):
                continue
            try:
                evaluation_payload = {key: int(value) for key, value in evaluation_json.items()}
            except (TypeError, ValueError):
                continue
            if explainer_module._sum_scores(evaluation_payload) <= 0:
                continue

            question_stem = explainer_module._extract_question_stem_from_eval_name(evaluation_path.name, question_stems)
            if question_stem is None:
                continue

            answer_filename = evaluation_path.name[: -len(".json")]
            answer_path = answers_dir / answer_filename
            if not answer_path.exists():
                continue

            output_path = explanations_dir / answer_filename
            if output_path.exists():
                continue

            question_text = question_by_stem[question_stem].read_text(encoding="utf-8")
            answer_text = answer_path.read_text(encoding="utf-8")
            prompt = explainer_module._build_explainer_prompt(
                protocol=explainer_module.EXPLANATION_PROTOCOL,
                question=question_text,
                answer=answer_text,
                evaluation_json=evaluation_payload,
            )
            future = common_module.submit_prompt_to_chat_completions(
                prompt=prompt,
                destination_path=str(output_path),
                llm_model=evaluator_model,
                **kwargs,
            )
            pending.append((future, evaluator_model, evaluation_path.name))

    for future, evaluator_model, evaluation_name in pending:
        future.result()
        print("finished explanation", evaluator_model, evaluation_name)


def execute_pipeline(config: Dict[str, Any], python_executable: str, dry_run: bool) -> None:
    if dry_run:
        print(f"Would execute d-bench for alias={config['alias']} base_model={config['base_model']}")
        return

    common_module = importlib.import_module("common")
    answer_module = importlib.import_module("answer")
    evaluate_module = importlib.import_module("evaluate")
    explainer_module = importlib.import_module("explainer")
    results_module = importlib.import_module("results")

    run_answers(config, common_module, answer_module)
    run_evaluations(config, common_module, evaluate_module)
    run_explanations(config, common_module, explainer_module)
    results_module.main()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_runtime_config(args)
    ensure_model_registered(config, args.dry_run)
    execute_pipeline(config, python_executable=args.python, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
