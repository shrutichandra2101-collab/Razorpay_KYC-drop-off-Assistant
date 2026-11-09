"""Local prototype state (JSON files under state/).

Holds everything a human records, so AI recommendations and human corrections stay traceable:
  reviews.json          verified / corrected diagnoses by an authorised reviewer
  flags.json            manual "needs human review" flags
  approvals.json        nudge approvals (approval != sending; nothing is ever sent)
  followup_events.json  simulated journey events (resumed, activated, closed, stop-communication ...)
  llm_cache.json        cached LLM outputs (diagnoses + messages) so runs are reproducible
  audit_log.jsonl       append-only log of every AI output and human action
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import STATE_DIR

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, state_dir: Path = STATE_DIR):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------- generic helpers ----------
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str, default: Any) -> Any:
        p = self._path(name)
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write(self, name: str, data: Any) -> None:
        p = self._path(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def audit(self, event: str, application_id: Optional[str], actor: str, details: Dict[str, Any]) -> None:
        rec = {"ts": _now(), "event": event, "application_id": application_id, "actor": actor, "details": details}
        with _LOCK:
            with open(self._path("audit_log.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def audit_log(self, limit: int = 1000) -> List[Dict[str, Any]]:
        p = self._path("audit_log.jsonl")
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))

    # ---------- human diagnosis review ----------
    def reviews(self) -> Dict[str, Dict[str, Any]]:
        return self._read("reviews.json", {})

    def record_review(self, application_id: str, action: str, verified_code: str, reviewer: str,
                      note: str, ai_code: Optional[str], ai_confidence: Optional[float]) -> None:
        with _LOCK:
            data = self.reviews()
            data[application_id] = {
                "action": action,  # "confirmed" | "corrected"
                "verified_code": verified_code,
                "reviewer": reviewer,
                "note": note,
                "ai_code": ai_code,
                "ai_confidence": ai_confidence,
                "ts": _now(),
            }
            self._write("reviews.json", data)
        self.audit("diagnosis_" + action, application_id, reviewer,
                   {"verified_code": verified_code, "ai_code": ai_code, "ai_confidence": ai_confidence, "note": note})

    def clear_review(self, application_id: str, reviewer: str) -> None:
        with _LOCK:
            data = self.reviews()
            prev = data.pop(application_id, None)
            self._write("reviews.json", data)
        self.audit("diagnosis_review_cleared", application_id, reviewer, {"previous": prev})

    # ---------- manual flags ----------
    def flags(self) -> Dict[str, Dict[str, Any]]:
        return self._read("flags.json", {})

    def set_flag(self, application_id: str, flagged: bool, actor: str, reason: str = "") -> None:
        with _LOCK:
            data = self.flags()
            if flagged:
                data[application_id] = {"reason": reason, "actor": actor, "ts": _now()}
            else:
                data.pop(application_id, None)
            self._write("flags.json", data)
        self.audit("human_review_flag_" + ("set" if flagged else "cleared"), application_id, actor, {"reason": reason})

    # ---------- approvals ----------
    def approvals(self) -> Dict[str, Dict[str, Any]]:
        return self._read("approvals.json", {})

    def approve_nudge(self, application_id: str, actor: str, message_hash: str, channel: str) -> None:
        with _LOCK:
            data = self.approvals()
            data[application_id] = {"actor": actor, "message_hash": message_hash, "channel": channel, "ts": _now()}
            self._write("approvals.json", data)
        self.audit("nudge_approved_not_sent", application_id, actor, {"message_hash": message_hash, "channel": channel})

    def revoke_approval(self, application_id: str, actor: str) -> None:
        with _LOCK:
            data = self.approvals()
            data.pop(application_id, None)
            self._write("approvals.json", data)
        self.audit("nudge_approval_revoked", application_id, actor, {})

    # ---------- follow-up journey events ----------
    def followup_events(self) -> Dict[str, List[Dict[str, Any]]]:
        return self._read("followup_events.json", {})

    def add_followup_event(self, application_id: str, event: str, actor: str, note: str = "") -> None:
        with _LOCK:
            data = self.followup_events()
            data.setdefault(application_id, []).append({"event": event, "actor": actor, "note": note, "ts": _now()})
            self._write("followup_events.json", data)
        self.audit("followup_event", application_id, actor, {"event": event, "note": note})

    def clear_followup_events(self, application_id: str, actor: str) -> None:
        with _LOCK:
            data = self.followup_events()
            data.pop(application_id, None)
            self._write("followup_events.json", data)
        self.audit("followup_events_cleared", application_id, actor, {})

    # ---------- R13 dropout-reason replies (merchant's tap/reply on the first nudge) ----------
    def record_dropout_reason(self, application_id: str, reason_key: str, actor: str) -> None:
        self.add_followup_event(application_id, "dropout_reason", actor, note=reason_key)

    def dropout_reasons(self) -> Dict[str, str]:
        """Latest recorded reason per application."""
        out: Dict[str, str] = {}
        for aid, evs in self.followup_events().items():
            for e in evs:
                if e["event"] == "dropout_reason":
                    out[aid] = e.get("note", "")
        return out

    # ---------- assistant: only the CURRENT question per operator (replaced on every new question) ----------
    def current_question(self, actor: str) -> Optional[Dict[str, Any]]:
        return self._read("assistant_current.json", {}).get(actor)

    def save_current_question(self, actor: str, reply: Dict[str, Any]) -> None:
        with _LOCK:
            data = self._read("assistant_current.json", {})
            data[actor] = reply
            self._write("assistant_current.json", data)

    def clear_current_question(self, actor: str) -> None:
        with _LOCK:
            data = self._read("assistant_current.json", {})
            data.pop(actor, None)
            self._write("assistant_current.json", data)

    # ---------- LLM cache ----------
    def llm_cache(self) -> Dict[str, Any]:
        return self._read("llm_cache.json", {})

    def put_llm(self, key: str, value: Dict[str, Any]) -> None:
        with _LOCK:
            data = self.llm_cache()
            data[key] = value
            self._write("llm_cache.json", data)

    def clear_llm_cache(self) -> None:
        with _LOCK:
            self._write("llm_cache.json", {})

    def version(self) -> str:
        """Cheap fingerprint so the UI can re-run the pipeline when human state changes."""
        parts = []
        for name in ("reviews.json", "flags.json", "approvals.json", "followup_events.json", "llm_cache.json"):
            p = self._path(name)
            parts.append(f"{name}:{p.stat().st_mtime_ns if p.exists() else 0}")
        return "|".join(parts)
