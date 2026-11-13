"""Rewrite product titles and descriptions via an LLM, preserving all facts.

COPY_PROVIDER picks which one talks to:
- "anthropic" (paid, the original default)
- "groq"      (free tier, no credit card, but rate-limited — cloud-hosted)
- "ollama"    (free, zero rate limit — runs locally; needs Ollama installed
  and the model pulled once with `ollama pull <model>`, no training involved)
"""

import asyncio
import json
import re

import httpx
from anthropic import AsyncAnthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CFG
from .prompts import (
    GENERATE_SYSTEM,
    REWRITE_SYSTEM,
    VARIANT_BATCH_SYSTEM,
    generate_user_prompt,
    rewrite_user_prompt,
    variant_batch_user_prompt,
)


_anthropic_client: AsyncAnthropic | None = None

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def _get_anthropic_client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        if not CFG.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY required when COPY_PROVIDER=anthropic")
        _anthropic_client = AsyncAnthropic(api_key=CFG.anthropic_api_key)
    return _anthropic_client


@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=2, min=2, max=30))
async def _chat_anthropic(system: str, user: str, max_tokens: int = 1024) -> str:
    client = _get_anthropic_client()
    resp = await client.messages.create(
        model=CFG.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")


@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=2, min=2, max=30))
async def _chat_groq(system: str, user: str, max_tokens: int = 1024) -> str:
    """Groq's free tier is rate-limited (requests/minute), so this retries
    with backoff on 429s and transient connection errors the same way
    download_bytes does for images."""
    if not CFG.groq_api_key:
        raise RuntimeError("GROQ_API_KEY required when COPY_PROVIDER=groq")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {CFG.groq_api_key}"},
            json={
                "model": CFG.groq_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.9,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=2, min=2, max=30))
async def _chat_ollama(system: str, user: str) -> str:
    """Local, no rate limit, no API key. Requires `ollama serve` running and
    the model already pulled (`ollama pull <model>`). Uses Ollama's native
    /api/chat endpoint rather than its OpenAI-compat layer, since the latter
    isn't available on every installed version."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{CFG.ollama_base_url}/api/chat",
            json={
                "model": CFG.ollama_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.9},
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Ollama {resp.status_code} from {CFG.ollama_base_url}/api/chat "
                f"(model={CFG.ollama_model!r}): {resp.text}"
            )
        data = resp.json()
    return data["message"]["content"]


async def _chat(system: str, user: str, max_tokens: int = 1024) -> str:
    if CFG.copy_provider == "groq":
        return await _chat_groq(system, user, max_tokens)
    if CFG.copy_provider == "ollama":
        return await _chat_ollama(system, user)
    return await _chat_anthropic(system, user, max_tokens)


def _extract_json(text: str) -> dict:
    """The LLM usually returns bare JSON; strip code fences if it slips one in."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


async def rewrite(title: str, description: str) -> tuple[str, str]:
    if not CFG.rewrite_copy:
        return title, description
    if not title.strip() and not description.strip():
        return title, description
    for attempt in range(3):
        try:
            text = await _chat(REWRITE_SYSTEM, rewrite_user_prompt(title, description))
            parsed = _extract_json(text)
            return str(parsed.get("title", title)), str(parsed.get("description", description))
        except (json.JSONDecodeError, KeyError):
            if attempt == 2:
                return title, description
            await asyncio.sleep(2 ** attempt)
    return title, description


async def rewrite_variant_titles_batch(titles: list[str], brand: str) -> list[str]:
    """Rewrite every variant title for a product in ONE LLM call instead of
    one call per variant. A product can carry dozens of variants; one call
    per variant means that many serial round-trips — brutal with a local
    model like Ollama that only handles one request at a time. Falls back
    to the original titles if the model doesn't return the right shape."""
    if not CFG.rewrite_copy or not titles:
        return titles
    budget = max(1024, 80 * len(titles))
    for attempt in range(3):
        try:
            text = await _chat(
                VARIANT_BATCH_SYSTEM, variant_batch_user_prompt(titles, brand), budget
            )
            parsed = _extract_json(text)
            result = parsed.get("titles")
            if isinstance(result, list) and len(result) == len(titles):
                return [str(t) for t in result]
            return titles
        except (json.JSONDecodeError, KeyError):
            if attempt == 2:
                return titles
            await asyncio.sleep(2 ** attempt)
    return titles


async def generate_from_title_brand(title: str, brand: str) -> tuple[str, str]:
    """Write a fresh title + description from title/brand alone, for schemas
    that have no description field to rewrite."""
    if not CFG.rewrite_copy or not title.strip():
        return title, ""
    for attempt in range(3):
        try:
            text = await _chat(GENERATE_SYSTEM, generate_user_prompt(title, brand))
            parsed = _extract_json(text)
            return str(parsed.get("title", title)), str(parsed.get("description", ""))
        except (json.JSONDecodeError, KeyError):
            if attempt == 2:
                return title, ""
            await asyncio.sleep(2 ** attempt)
    return title, ""
