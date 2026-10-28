from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import httpx

ENDPOINT = "https://api.openai.com/v1/responses"
MODEL = "gpt-5.4-mini-2026-03-17"
CONTEXT_TOKENS = 400_000
# Conservative price ceilings: $1 input / $6 output per million tokens.
# Verified standard model rates on 2026-10-28: $0.75 / $4.50.
INPUT_NS_PER_TOKEN = 1000
OUTPUT_NS_PER_TOKEN = 6000
RATE_REVIEW_UNTIL = "2026-11-04"
RATE_REVIEWED_ON = "2026-10-28"
MAX_RESPONSE_BYTES = 65_536


class ProviderFailure(Exception):
    pass


@dataclass(frozen=True)
class Receipt:
    text: str
    charge_ns: int
    input_tokens: int
    output_tokens: int
    completed: bool


def rates_current(now):
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    return RATE_REVIEWED_ON <= day <= RATE_REVIEW_UNTIL


def reservation_ns(max_output_tokens):
    # Reserve the entire documented model context, rather than guessing tokenization/framing.
    return CONTEXT_TOKENS * INPUT_NS_PER_TOKEN + max_output_tokens * OUTPUT_NS_PER_TOKEN


async def complete(client: httpx.AsyncClient, key: str, instructions: str, text: str, max_output_tokens: int, ticket: str) -> Receipt:
    payload = {"model": MODEL, "instructions": instructions,
               "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
               "max_output_tokens": max_output_tokens, "reasoning": {"effort": "none"},
               "store": False, "stream": False, "service_tier": "default", "tools": [], "tool_choice": "none"}
    try:
        async with asyncio.timeout(40):
            async with client.stream("POST", ENDPOINT, json=payload,
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Client-Request-Id": ticket}) as response:
                if response.status_code != 200 or response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    raise ProviderFailure()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderFailure()
                    body.extend(chunk)
        data = json.loads(body)
        usage = data.get("usage", {})
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if (data.get("model") != MODEL or data.get("service_tier") != "default"
                or data.get("status") not in {"completed", "incomplete"}
                or type(input_tokens) is not int or type(output_tokens) is not int
                or type(usage.get("total_tokens")) is not int
                or not 0 <= input_tokens <= CONTEXT_TOKENS or not 0 <= output_tokens <= max_output_tokens
                or usage.get("total_tokens") != input_tokens + output_tokens):
            raise ProviderFailure()
        text = "".join(part["text"] for item in data.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", []) if part.get("type") == "output_text" and isinstance(part.get("text"), str))
        if len(text.encode()) > MAX_RESPONSE_BYTES:
            raise ProviderFailure()
        return Receipt(text, input_tokens * INPUT_NS_PER_TOKEN + output_tokens * OUTPUT_NS_PER_TOKEN,
                       input_tokens, output_tokens, data["status"] == "completed")
    except Exception:
        # Provider bodies, header values and exception text never become client diagnostics/logs.
        raise ProviderFailure() from None
