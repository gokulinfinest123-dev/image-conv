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
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import CFG
from .prompts import (
    DESCRIBE_GENERATE_SYSTEM,
    DESCRIBE_REWRITE_SYSTEM,
    GENERATE_SYSTEM,
    REWRITE_SYSTEM,
    VARIANT_BATCH_SYSTEM,
    describe_generate_user_prompt,
    describe_rewrite_user_prompt,
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


class _OllamaRejected(RuntimeError):
    """Ollama refused the request (4xx, e.g. model not pulled) — a retry can't help."""


_ollama_gate: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _ollama_turn() -> asyncio.Lock:
    """One request at a time. A local model generates serially anyway, and a
    request parked in Ollama's own queue burns its timeout doing nothing —
    then gets retried from scratch."""
    global _ollama_gate
    loop = asyncio.get_running_loop()
    if _ollama_gate is None or _ollama_gate[0] is not loop:
        _ollama_gate = (loop, asyncio.Lock())
    return _ollama_gate[1]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    # isinstance(Exception) keeps task cancellation from being "retried".
    retry=retry_if_exception(
        lambda e: isinstance(e, Exception) and not isinstance(e, _OllamaRejected)
    ),
    reraise=True,
)
async def _chat_ollama(system: str, user: str, max_tokens: int = 1024) -> str:
    """Local, no rate limit, no API key. Requires `ollama serve` running and
    the model already pulled (`ollama pull <model>`). Uses Ollama's native
    /api/chat endpoint rather than its OpenAI-compat layer, since the latter
    isn't available on every installed version."""
    async with _ollama_turn():
        # CPU-only generation of a full answer can take minutes.
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
            resp = await client.post(
                f"{CFG.ollama_base_url}/api/chat",
                json={
                    "model": CFG.ollama_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    # Guarantees parseable output; a small model otherwise
                    # wraps JSON in prose and the whole answer is thrown away.
                    "format": "json",
                    "keep_alive": "30m",
                    "options": {"temperature": 0.9, "num_predict": max_tokens},
                },
            )
    if resp.status_code >= 400:
        error = RuntimeError if resp.status_code >= 500 else _OllamaRejected
        raise error(
            f"Ollama {resp.status_code} from {CFG.ollama_base_url}/api/chat "
            f"(model={CFG.ollama_model!r}): {resp.text}"
        )
    return resp.json()["message"]["content"]


async def _chat(system: str, user: str, max_tokens: int = 1024) -> str:
    if CFG.copy_provider == "groq":
        return await _chat_groq(system, user, max_tokens)
    if CFG.copy_provider == "ollama":
        return await _chat_ollama(system, user, max_tokens)
    return await _chat_anthropic(system, user, max_tokens)


def _extract_json(text: str) -> dict:
    """The LLM usually returns bare JSON; strip code fences if it slips one in."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


async def _write_description(system: str, user: str, fallback: str) -> str:
    """Ask the model for just a description; `fallback` if it never answers properly."""
    for attempt in range(3):
        try:
            parsed = _extract_json(await _chat(system, user))
            text = parsed.get("description")
            if isinstance(text, str) and text.strip():
                return text.strip()
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass
        if attempt < 2:
            await asyncio.sleep(2 ** attempt)
    return fallback


async def rewrite(title: str, description: str) -> tuple[str, str]:
    if not CFG.rewrite_copy:
        return title, description
    if not CFG.rewrite_titles:
        if description.strip():
            return title, await _write_description(
                DESCRIBE_REWRITE_SYSTEM, describe_rewrite_user_prompt(title, description), description
            )
        return await generate_from_title_brand(title, "")
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


# Titles per LLM call: small enough that prompt + answer fit a small local
# model's default context window, so nothing gets silently truncated.
TITLE_CHUNK = 8


async def _rewrite_title_chunk(titles: list[str], brand: str) -> dict[str, str]:
    done: dict[str, str] = {}
    for _ in range(3):
        todo = [t for t in titles if t not in done]
        if not todo:
            break
        try:
            parsed = _extract_json(
                await _chat(VARIANT_BATCH_SYSTEM, variant_batch_user_prompt(todo, brand))
            )
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        for number, title in enumerate(todo, start=1):
            new = parsed.get(str(number))
            if isinstance(new, str) and new.strip() and new.strip() != title:
                done[title] = new.strip()
    for title in titles:
        if title not in done:
            done[title], _ = await generate_from_title_brand(title, brand)
    return done


async def rewrite_titles(titles: list[str], brand: str) -> dict[str, str]:
    """Map each distinct title to its rewrite, several titles per LLM call.

    Variants of one product mostly repeat the same few titles, and one call
    per variant is brutal against a local model that answers serially."""
    unique = list(dict.fromkeys(titles))
    if not CFG.rewrite_copy or not CFG.rewrite_titles or not unique:
        return {}
    rewritten: dict[str, str] = {}
    for start in range(0, len(unique), TITLE_CHUNK):
        rewritten.update(await _rewrite_title_chunk(unique[start:start + TITLE_CHUNK], brand))
    return rewritten


async def generate_from_title_brand(title: str, brand: str) -> tuple[str, str]:
    """Write a description (and, with REWRITE_TITLES, a fresh title) from
    title/brand alone, for schemas that have no description field to rewrite."""
    if not CFG.rewrite_copy or not title.strip():
        return title, ""
    if not CFG.rewrite_titles:
        return title, await _write_description(
            DESCRIBE_GENERATE_SYSTEM, describe_generate_user_prompt(title, brand), ""
        )
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
