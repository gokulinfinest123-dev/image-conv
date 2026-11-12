"""Rewrite product titles and descriptions via an LLM, preserving all facts.

COPY_PROVIDER picks which one talks to:
- "anthropic" (paid, the original default)
- "groq" (free tier, no credit card — cloud-hosted open models, so there's
  no local install/training step like running Ollama yourself would need)
"""

import asyncio
import json
import re

import httpx
from anthropic import AsyncAnthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CFG
from .prompts import GENERATE_SYSTEM, REWRITE_SYSTEM, generate_user_prompt, rewrite_user_prompt


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
async def _chat_anthropic(system: str, user: str) -> str:
    client = _get_anthropic_client()
    resp = await client.messages.create(
        model=CFG.anthropic_model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")


@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=2, min=2, max=30))
async def _chat_groq(system: str, user: str) -> str:
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
                "temperature": 0.7,
                "max_tokens": 1024,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


async def _chat(system: str, user: str) -> str:
    if CFG.copy_provider == "groq":
        return await _chat_groq(system, user)
    return await _chat_anthropic(system, user)


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
