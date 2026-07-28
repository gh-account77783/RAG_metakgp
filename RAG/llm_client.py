"""Minimal resilient client for the Ollama Cloud chat API."""

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv


load_dotenv()
logger = logging.getLogger(__name__)

MODEL_NAME = "gemma4:31b-cloud"
BASE_URLS = ("https://ollama.com", "https://api.ollama.com")
MAX_RETRIES = 3
BACKOFF_FACTOR_SECONDS = 2.0


class LLMClient:
    """Synchronous Ollama client with bounded retries for transient failures."""

    def __init__(self) -> None:
        self.api_key = os.getenv("ollama_api_key") or os.getenv("OLLAMA_API_KEY")
        self.current_base_url = BASE_URLS[0]
        if not self.api_key:
            logger.warning("OLLAMA_API_KEY is not configured.")

    def _request(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for base_url in BASE_URLS:
            url = f"{base_url}{endpoint}"
            for attempt in range(MAX_RETRIES):
                try:
                    with httpx.Client(timeout=60.0) as client:
                        response = client.post(url, headers=headers, json=payload)
                    if response.is_success:
                        self.current_base_url = base_url
                        return response.json()
                    if response.status_code not in (429, 502, 503, 504):
                        logger.error("Ollama request to %s returned HTTP %d", url, response.status_code)
                        break
                    logger.warning("Ollama request to %s returned HTTP %d (attempt %d/%d)", url, response.status_code, attempt + 1, MAX_RETRIES)
                except httpx.HTTPError as exc:
                    logger.warning("Ollama request to %s failed (attempt %d/%d): %s", url, attempt + 1, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_FACTOR_SECONDS**attempt)
        raise RuntimeError(f"No Ollama endpoint completed {endpoint}")

    def list_models(self) -> Optional[Dict[str, Any]]:
        """Return available remote models, or ``None`` when the service is unavailable."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for base_url in BASE_URLS:
            try:
                with httpx.Client(timeout=10.0) as client:
                    response = client.get(f"{base_url}/api/tags", headers=headers)
                if response.is_success:
                    self.current_base_url = base_url
                    return response.json()
            except httpx.HTTPError as exc:
                logger.warning("Could not list models from %s: %s", base_url, exc)
        return None

    def generate(self, prompt: str, system_prompt: str = "You are a helpful assistant.") -> str:
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        try:
            result = self._request("/api/chat", payload)
            return result["message"]["content"]
        except (KeyError, RuntimeError) as exc:
            logger.error("LLM generation failed: %s", exc)
            return "Error: LLM generation failed."


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    client = LLMClient()
    print(client.list_models())
