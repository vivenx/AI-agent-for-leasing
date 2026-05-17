from __future__ import annotations

from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Any

from leasing_analyzer.core.logging import get_logger

logger = get_logger(__name__)

_STATUS_TO_LEVEL = {
    "ok": 20,
    "warning": 30,
    "error": 40,
}

_RISK_ORDER = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


@dataclass(slots=True)
class AgentAuditEntry:
    action: str
    status: str
    risk: str
    confidence: float
    message: str
    metrics: dict[str, Any] = field(default_factory=dict)


class AgentAuditTrail:
    """Collects self-check events and mirrors them into terminal logs."""

    def __init__(self) -> None:
        self._entries: list[AgentAuditEntry] = []
        self._lock = Lock()

    def record(
        self,
        action: str,
        status: str,
        risk: str,
        confidence: float,
        message: str,
        **metrics: Any,
    ) -> dict[str, Any]:
        normalized_status = status if status in _STATUS_TO_LEVEL else "warning"
        normalized_risk = risk if risk in _RISK_ORDER else "medium"
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
        clean_metrics = {
            key: value
            for key, value in metrics.items()
            if value is not None
        }

        entry = AgentAuditEntry(
            action=action,
            status=normalized_status,
            risk=normalized_risk,
            confidence=round(normalized_confidence, 2),
            message=message,
            metrics=clean_metrics,
        )

        with self._lock:
            self._entries.append(entry)

        metrics_str = ""
        if clean_metrics:
            rendered_metrics = " ".join(f"{key}={value}" for key, value in clean_metrics.items())
            metrics_str = f" | {rendered_metrics}"

        logger.log(
            _STATUS_TO_LEVEL[normalized_status],
            "[SELF-CHECK] action=%s status=%s risk=%s confidence=%.2f | %s%s",
            entry.action,
            entry.status,
            entry.risk,
            entry.confidence,
            entry.message,
            metrics_str,
        )
        return asdict(entry)

    def export(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(entry) for entry in self._entries]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries)

        if not entries:
            return {
                "total_steps": 0,
                "ok_steps": 0,
                "warning_steps": 0,
                "error_steps": 0,
                "high_risk_steps": 0,
                "average_confidence": 0.0,
                "highest_risk": "low",
            }

        ok_steps = sum(1 for entry in entries if entry.status == "ok")
        warning_steps = sum(1 for entry in entries if entry.status == "warning")
        error_steps = sum(1 for entry in entries if entry.status == "error")
        high_risk_steps = sum(1 for entry in entries if entry.risk == "high")
        average_confidence = sum(entry.confidence for entry in entries) / len(entries)
        highest_risk = max(entries, key=lambda entry: _RISK_ORDER[entry.risk]).risk

        return {
            "total_steps": len(entries),
            "ok_steps": ok_steps,
            "warning_steps": warning_steps,
            "error_steps": error_steps,
            "high_risk_steps": high_risk_steps,
            "average_confidence": round(average_confidence, 2),
            "highest_risk": highest_risk,
        }
