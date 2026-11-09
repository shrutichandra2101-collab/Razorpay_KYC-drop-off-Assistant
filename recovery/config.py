"""Paths and settings loading. Defaults live in config/settings.json (never written by the app);
user overrides from the Settings page go to state/settings_override.json."""
from __future__ import annotations

import copy
import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
RULES_DIR = ROOT / "rules"
PROMPTS_DIR = ROOT / "prompts"
STATE_DIR = ROOT / "state"
OUTPUTS_DIR = ROOT / "outputs"

DEFAULT_SETTINGS_FILE = CONFIG_DIR / "settings.json"
OVERRIDE_SETTINGS_FILE = STATE_DIR / "settings_override.json"

try:  # optional .env support
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def default_settings() -> Dict[str, Any]:
    return load_json(DEFAULT_SETTINGS_FILE)


def load_settings() -> Dict[str, Any]:
    settings = default_settings()
    if OVERRIDE_SETTINGS_FILE.exists():
        try:
            settings = _deep_merge(settings, load_json(OVERRIDE_SETTINGS_FILE))
        except Exception:
            pass
    # Environment variables win for LLM + workbook config (no secrets in files).
    if os.getenv("LLM_MODEL"):
        settings["llm"]["model"] = os.getenv("LLM_MODEL")
    if os.getenv("LLM_ENABLED"):
        settings["llm"]["enabled"] = os.getenv("LLM_ENABLED", "true").lower() in ("1", "true", "yes")
    if os.getenv("WORKBOOK_PATH"):
        settings["workbook"]["path"] = os.getenv("WORKBOOK_PATH")
    return settings


def save_settings_override(override: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(OVERRIDE_SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(override, fh, indent=2)


def reset_settings_override() -> None:
    if OVERRIDE_SETTINGS_FILE.exists():
        OVERRIDE_SETTINGS_FILE.unlink()


def diff_from_defaults(current: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    defaults = defaults if defaults is not None else default_settings()
    out: Dict[str, Any] = {}
    for k, v in current.items():
        if k.startswith("_"):
            continue
        dv = defaults.get(k)
        if isinstance(v, dict) and isinstance(dv, dict):
            sub = diff_from_defaults(v, dv)
            if sub:
                out[k] = sub
        elif v != dv:
            out[k] = v
    return out


def resolve_workbook_path(settings: Dict[str, Any]) -> Path:
    explicit = settings.get("workbook", {}).get("path")
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else ROOT / p
    pattern = settings.get("workbook", {}).get("search_glob", "Buildathon_Student_Data_Pack*.xlsx")
    matches = sorted(glob.glob(str(ROOT / pattern))) + sorted(glob.glob(str(ROOT / "data" / pattern)))
    if not matches:
        raise FileNotFoundError(
            f"No workbook matching '{pattern}' found in {ROOT} or {ROOT / 'data'}. "
            "Set WORKBOOK_PATH in .env or upload the workbook from the Settings page."
        )
    return Path(matches[0])


def load_rules(name: str) -> Dict[str, Any]:
    return load_json(RULES_DIR / name)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
