"""Rewrite product titles and descriptions via Claude, preserving all facts."""

import asyncio
import json
import re

from anthropic import AsyncAnthropic

from .config import CFG
from .prompts import GENERATE_SYSTEM, REWRITE_SYSTEM, generate_user_prompt, rewrite_user_prompt


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not CFG.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY required when REWRITE_COPY=true")
        _client = AsyncAnthropic(api_key=CFG.anthropic_api_key)
    return _client


def _extract_json(text: str) -> dict:
    """Claude usually returns bare JSON; strip code fences if it slips one in."""
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
    client = _get_client()
    for attempt in range(3):
        try:
            resp = await client.messages.create(
                model=CFG.anthropic_model,
                max_tokens=1024,
                system=REWRITE_SYSTEM,
                messages=[{"role": "user", "content": rewrite_user_prompt(title, description)}],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
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
    client = _get_client()
    for attempt in range(3):
        try:
            resp = await client.messages.create(
                model=CFG.anthropic_model,
                max_tokens=1024,
                system=GENERATE_SYSTEM,
                messages=[{"role": "user", "content": generate_user_prompt(title, brand)}],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
            parsed = _extract_json(text)
            return str(parsed.get("title", title)), str(parsed.get("description", ""))
        except (json.JSONDecodeError, KeyError):
            if attempt == 2:
                return title, ""
            await asyncio.sleep(2 ** attempt)
    return title, ""
