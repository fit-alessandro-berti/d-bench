#!/usr/bin/env python3
"""Shared model inventory helpers for maintenance scripts."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


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


def load_answering_models(common_path: Path) -> list[AnsweringModel]:
    tree = ast.parse(common_path.read_text(encoding="utf-8"), filename=str(common_path))
    value = _find_assignment_value(tree, "ANSWERING_LLMS")
    if not isinstance(value, (ast.List, ast.Tuple)):
        raise ValueError("ANSWERING_LLMS must be defined as a list or tuple literal.")

    models: list[AnsweringModel] = []
    seen_model_names: set[str] = set()
    for element in value.elts:
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


def _find_assignment_value(tree: ast.AST, target_name: str) -> ast.AST:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == target_name:
                    return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == target_name and node.value is not None:
                return node.value

    raise ValueError(f"Could not find {target_name} in common.py.")


def _parse_answering_model(node: ast.AST) -> AnsweringModel:
    if not isinstance(node, (ast.Tuple, ast.List)) or not node.elts:
        raise ValueError("Each ANSWERING_LLMS entry must be a tuple or list with at least a model name.")

    model_name_node = node.elts[0]
    if not isinstance(model_name_node, ast.Constant) or not isinstance(model_name_node.value, str):
        raise ValueError("Each ANSWERING_LLMS entry must start with a string model name.")
    model_name = model_name_node.value

    has_explicit_api_url = False
    if len(node.elts) > 1 and isinstance(node.elts[1], ast.Dict):
        has_explicit_api_url = _dict_contains_string_key(node.elts[1], "api_url")

    return AnsweringModel(
        model_name=model_name,
        sanitized_name=sanitize_model_name(model_name),
        has_explicit_api_url=has_explicit_api_url,
    )


def _dict_contains_string_key(node: ast.Dict, key: str) -> bool:
    for dict_key in node.keys:
        if isinstance(dict_key, ast.Constant) and dict_key.value == key:
            return True
    return False
