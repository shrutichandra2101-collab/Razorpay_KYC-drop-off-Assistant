"""Thin, optional LLM layer (Claude via the official Anthropic Python SDK).

The LLM is used for exactly two things:
  1. classifying blank-code reviewer messages into one of the 13 catalogue codes (with evidence), and
  2. rewriting nudges more naturally in the merchant's language.
It never decides fixability, routing, channel or priority - those are rules. Every LLM output is
validated (catalogue membership, evidence must be a verbatim substring, message guardrails) and the
deterministic path is used whenever the LLM is unavailable or its output fails validation.

Credentials come from the environment (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant profile);
nothing is hard-coded.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

try:
    import anthropic  # type: ignore
except Exception:  # pragma: no cover
    anthropic = None  # type: ignore

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _has_credentials() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_PROFILE"))


class LLMClient:
    def __init__(self, llm_settings: Dict[str, Any]):
        self.settings = llm_settings
        self.model = llm_settings.get("model", "claude-sonnet-5-5")
        self.effort = llm_settings.get("effort", "low")
        self.max_tokens = int(llm_settings.get("max_tokens", 4000))
        self.use_fallbacks = bool(llm_settings.get("use_server_fallbacks", True))
        self.last_error: Optional[str] = None
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.settings.get("enabled", True)) and anthropic is not None and _has_credentials()

    def status(self) -> str:
        if anthropic is None:
            return "Offline - 'anthropic' package not installed"
        if not self.settings.get("enabled", True):
            return "Offline - LLM disabled in settings"
        if not _has_credentials():
            return "Offline - no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in environment"
        return f"Online - {self.model} (effort: {self.effort})"

    def _get_client(self):
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def complete_json(self, system: str, user: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Returns (parsed_json_or_None, meta). Never raises."""
        meta: Dict[str, Any] = {"model": self.model, "ok": False}
        if not self.available:
            meta["error"] = self.status()
            return None, meta
        client = self._get_client()
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        body: Dict[str, Any] = {"output_config": {"effort": self.effort}}
        headers: Dict[str, str] = {}
        if self.use_fallbacks:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = FALLBACK_BETA
        try:
            try:
                resp = client.messages.create(**kwargs, extra_body=body, extra_headers=headers or None)
            except anthropic.BadRequestError:
                # Gateway or account may not accept the fallback beta / effort field - retry plainly.
                resp = client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            meta["error"] = "Authentication failed - check ANTHROPIC_API_KEY"
        except anthropic.RateLimitError:
            meta["error"] = "Rate limited - try again shortly"
        except anthropic.APIStatusError as e:
            meta["error"] = f"API error {e.status_code}: {getattr(e, 'message', e)}"
        except anthropic.APIConnectionError:
            meta["error"] = "Network error reaching the API"
        except Exception as e:  # pragma: no cover
            meta["error"] = f"Unexpected error: {e}"
        else:
            meta["stop_reason"] = resp.stop_reason
            meta["model_served"] = getattr(resp, "model", self.model)
            if resp.stop_reason == "refusal":
                meta["error"] = "Model declined the request"
                return None, meta
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
            meta["raw"] = text
            parsed = parse_json_block(text)
            if parsed is None:
                meta["error"] = "Response was not valid JSON"
                return None, meta
            meta["ok"] = True
            return parsed, meta
        self.last_error = meta.get("error")
        return None, meta


def parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        out = json.loads(candidate)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None
