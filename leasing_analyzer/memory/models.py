from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MemorySession:
    session_id: str
    user_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class MemoryInteraction:
    session_id: str
    kind: str
    user_input: str
    response_summary: str
    item_name: Optional[str] = None
    price: Optional[int] = None
    metadata_json: str = "{}"
    created_at: Optional[str] = None


@dataclass
class MemoryContext:
    session_id: str
    summary: Optional[str] = None
    recent_interactions: list[dict] = field(default_factory=list)
    relevant_facts: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        parts: list[str] = []

        if self.summary:
            parts.append(f"Session summary:\n{self.summary}")

        if self.relevant_facts:
            facts = "\n".join(f"- {fact}" for fact in self.relevant_facts[:7])
            parts.append(f"Relevant memory facts:\n{facts}")

        if self.recent_interactions:
            recent = "\n".join(
                f"- [{x.get('kind', 'unknown')}] input={x.get('user_input', '')[:160]} | summary={x.get('response_summary', '')[:200]}"
                for x in self.recent_interactions[:5]
            )
            parts.append(f"Recent interactions:\n{recent}")

        return "\n\n".join(parts).strip()
