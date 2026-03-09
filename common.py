import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional

import requests
import logging


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_SLEEP_SECONDS = 5
MAX_CONCURRENT_THREADS = 8
REQUEST_TIMEOUT_SECONDS = 120
ANSWERING_LLMS = [
    ("openai/gpt-4o-mini",),
]

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_THREADS)
_write_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def _submit_and_write_with_retries(
    prompt: str,
    destination_path: str,
    llm_model: str,
    api_url: str,
    api_key: str,
    additional_payload: Optional[Dict[str, Any]],
) -> None:
    payload: Dict[str, Any] = {
        "model": llm_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if additional_payload:
        payload.update(additional_payload)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempt = 0
    while True:
        attempt += 1
        t0 = time.time_ns()
        _logger.info(
            "Submitting request | model=%s destination=%s attempt=%d",
            llm_model,
            destination_path,
            attempt,
        )
        try:
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            response_json = response.json()
            content = response_json["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                content = str(content)

            os.makedirs(os.path.dirname(os.path.abspath(destination_path)), exist_ok=True)
            with _write_lock:
                with open(destination_path, "w", encoding="utf-8") as file:
                    file.write(content)
            t1 = time.time_ns()
            response_time = (t1 - t0) / 10**9
            _logger.info(
                "Completed request | model=%s destination=%s attempt=%d response_time_s=%.3f",
                llm_model,
                destination_path,
                attempt,
                response_time,
            )
            return
        except Exception as exc:
            t1 = time.time_ns()
            response_time = (t1 - t0) / 10**9
            _logger.warning(
                "Request failed | model=%s destination=%s attempt=%d response_time_s=%.3f error=%s",
                llm_model,
                destination_path,
                attempt,
                response_time,
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

    _logger.info(
        "Queued request | model=%s destination=%s",
        llm_model,
        destination_path,
    )
    return _executor.submit(
        _submit_and_write_with_retries,
        prompt,
        destination_path,
        llm_model,
        api_url,
        api_key,
        additional_payload,
    )
