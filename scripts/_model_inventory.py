#!/usr/bin/env python3
"""Shared model inventory helpers for maintenance scripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ANSWER_FILENAME_RE = re.compile(r"^(?P<model_key>.+)_(?P<question_key>q\d+)\.txt$")


@dataclass(frozen=True)
class AnsweringModel:
    model_name: str
    sanitized_name: str
    has_explicit_api_url: bool

    @property
    def uses_openrouter(self) -> bool:
        return not self.has_explicit_api_url


def sanitize_model_name(model_name: str) -> str:
    sanitized = model_name.replace("/", "").replace(":", "")
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in sanitized)


def load_answering_models(models_path: Path) -> list[AnsweringModel]:
    payload = json.loads(models_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("models.json must contain a JSON object.")

    value = payload.get("answering_llms")
    if not isinstance(value, list):
        raise ValueError("models.json must define an answering_llms list.")

    models: list[AnsweringModel] = []
    seen_model_names: set[str] = set()
    for element in value:
        model = _parse_answering_model(element)
        if model.model_name in seen_model_names:
            continue
        seen_model_names.add(model.model_name)
        models.append(model)
    return models


def load_answer_model_keys(answers_dir: Path) -> list[str]:
    if not answers_dir.is_dir():
        return []

    model_keys = {
        match.group("model_key")
        for path in answers_dir.glob("*.txt")
        if (match := ANSWER_FILENAME_RE.match(path.name)) is not None
    }
    return sorted(model_keys)


def starts_with_capital(value: str) -> bool:
    return bool(value) and value[0].isupper()


def _parse_answering_model(node: Any) -> AnsweringModel:
    if not isinstance(node, list) or not node:
        raise ValueError("Each answering_llms entry must be an array with at least a model name.")

    model_name = node[0]
    if not isinstance(model_name, str):
        raise ValueError("Each answering_llms entry must start with a string model name.")

    has_explicit_api_url = any(
        isinstance(element, dict) and isinstance(element.get("api_url"), str)
        for element in node[1:]
    )

    return AnsweringModel(
        model_name=model_name,
        sanitized_name=sanitize_model_name(model_name),
        has_explicit_api_url=has_explicit_api_url,
    )
