from __future__ import annotations

import json
from typing import Optional

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.memory.models import MemoryContext
from leasing_analyzer.memory.repository import MemoryRepository


logger = get_logger(__name__)


class MemoryService:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository
        logger.info("Memory service initialized with database: %s", repository.db_path)

    def create_session(self, session_id: str, user_id: Optional[str] = None) -> dict:
        try:
            self.repository.ensure_session(session_id, user_id=user_id)
            session = self.repository.get_session(session_id) or {"session_id": session_id, "user_id": user_id}
            logger.info("Memory session ensured: session_id=%s user_id=%s", session_id, user_id)
            return session
        except Exception:
            logger.exception("Memory error while creating session: session_id=%s user_id=%s", session_id, user_id)
            raise

    def build_context(
        self,
        session_id: Optional[str],
        user_id: Optional[str] = None,
        item_name: Optional[str] = None,
    ) -> Optional[MemoryContext]:
        if not session_id:
            logger.debug("Memory context skipped: session_id is missing")
            return None

        try:
            self.repository.ensure_session(session_id, user_id=user_id)

            summary = self.repository.get_summary(session_id)
            recent = self.repository.get_recent_interactions(session_id, limit=CONFIG.memory_recent_limit)

            related = []
            if item_name:
                related = self.repository.find_related_by_item(
                    session_id,
                    item_name=item_name,
                    limit=CONFIG.memory_related_limit,
                )

            dataset_entries = self.repository.search_dataset_entries(
                session_id=session_id,
                user_id=user_id,
                item_name=item_name,
                limit=CONFIG.memory_dataset_limit,
            )

            facts: list[str] = []
            seen = set()
            for row in related:
                text = row.get("response_summary") or ""
                if text and text not in seen:
                    seen.add(text)
                    facts.append(text)

            for entry in dataset_entries:
                content = entry.get("content") or ""
                if content and content not in seen:
                    seen.add(content)
                    facts.append(content)

            context = MemoryContext(
                session_id=session_id,
                summary=summary,
                recent_interactions=recent,
                relevant_facts=facts,
                dataset_entries=dataset_entries,
            )
            logger.info(
                "Memory context built: session_id=%s item_name=%s recent=%s related=%s dataset=%s",
                session_id,
                item_name,
                len(recent),
                len(related),
                len(dataset_entries),
            )
            return context
        except Exception:
            logger.exception(
                "Memory error while building context: session_id=%s user_id=%s item_name=%s",
                session_id,
                user_id,
                item_name,
            )
            return None

    def save_describe_interaction(
        self,
        session_id: Optional[str],
        user_input: str,
        result: dict,
    ) -> None:
        if not session_id:
            logger.debug("Memory save skipped for describe interaction: session_id is missing")
            return

        try:
            market = result.get("market_report") or {}
            analogs = result.get("analogs_suggested") or market.get("analogs_suggested") or []
            item_name = result.get("item") or market.get("item")
            median = market.get("median_price")
            price_range = market.get("market_range")

            summary = (
                f"Analyzed item '{item_name}'. "
                f"Median market price: {median}. "
                f"Market range: {price_range}. "
                f"Suggested analogs: {', '.join(analogs[:3]) if analogs else 'none'}."
            )

            metadata = {
                "market_report": market,
                "analogs": analogs[:5],
            }

            self.repository.add_interaction(
                session_id=session_id,
                kind="describe",
                user_input=user_input,
                response_summary=summary,
                item_name=item_name,
                price=market.get("client_price"),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            logger.info("Memory interaction saved: session_id=%s kind=describe item_name=%s", session_id, item_name)

            self._save_describe_dataset_entries(
                session_id=session_id,
                user_input=user_input,
                item_name=item_name,
                market=market,
                analogs=analogs,
            )
            self._refresh_session_summary(session_id)
        except Exception:
            logger.exception("Memory error while saving describe interaction: session_id=%s", session_id)

    def save_document_interaction(
        self,
        session_id: Optional[str],
        file_name: str,
        result: dict,
    ) -> None:
        if not session_id:
            logger.debug("Memory save skipped for document interaction: session_id is missing")
            return

        try:
            item_name = result.get("item_name")
            declared_price = result.get("declared_price")
            price_check = result.get("price_check") or {}

            summary = (
                f"Analyzed document '{file_name}'. "
                f"Extracted item: '{item_name}'. "
                f"Declared price: {declared_price}. "
                f"Price check verdict: {price_check.get('verdict')}."
            )

            metadata = {
                "file_name": file_name,
                "key_characteristics": result.get("key_characteristics") or {},
                "warnings": result.get("warnings") or [],
            }

            self.repository.add_interaction(
                session_id=session_id,
                kind="document",
                user_input=file_name,
                response_summary=summary,
                item_name=item_name,
                price=declared_price,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            logger.info("Memory interaction saved: session_id=%s kind=document file_name=%s", session_id, file_name)

            self._save_document_dataset_entries(
                session_id=session_id,
                file_name=file_name,
                result=result,
            )
            self._refresh_session_summary(session_id)
        except Exception:
            logger.exception("Memory error while saving document interaction: session_id=%s file_name=%s", session_id, file_name)

    def get_session_memory(self, session_id: str, limit: int = 20) -> Optional[dict]:
        try:
            session = self.repository.get_session(session_id)
            if not session:
                logger.warning("Memory session not found: session_id=%s", session_id)
                return None

            payload = {
                "session": session,
                "summary": self.repository.get_summary(session_id),
                "interactions": self.repository.get_all_interactions(session_id, limit=limit),
                "dataset_entries": self.repository.get_dataset_entries(session_id, limit=limit),
            }
            logger.info(
                "Memory dump loaded: session_id=%s interactions=%s dataset_entries=%s",
                session_id,
                len(payload["interactions"]),
                len(payload["dataset_entries"]),
            )
            return payload
        except Exception:
            logger.exception("Memory error while loading session dump: session_id=%s", session_id)
            return None

    def clear_session_memory(self, session_id: str) -> bool:
        try:
            session = self.repository.get_session(session_id)
            if not session:
                logger.warning("Memory clear skipped: session not found session_id=%s", session_id)
                return False

            self.repository.delete_session_memory(session_id)
            logger.info("Memory cleared: session_id=%s", session_id)
            return True
        except Exception:
            logger.exception("Memory error while clearing session: session_id=%s", session_id)
            return False

    def _refresh_session_summary(self, session_id: str) -> None:
        recent = self.repository.get_recent_interactions(
            session_id,
            limit=CONFIG.memory_summary_history_limit,
        )
        session_summary = " ".join(
            x["response_summary"]
            for x in reversed(recent)
            if x.get("response_summary")
        )
        self.repository.upsert_summary(session_id, session_summary[:CONFIG.memory_summary_max_chars])
        logger.info("Memory session summary updated: session_id=%s", session_id)

    def _save_dataset_entry(
        self,
        session_id: str,
        dataset_name: str,
        entry_type: str,
        title: str,
        content: str,
        item_name: Optional[str] = None,
        price: Optional[int] = None,
        source_kind: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if not content.strip():
            return

        session = self.repository.get_session(session_id)
        user_id = session.get("user_id") if session else None

        self.repository.add_dataset_entry(
            session_id=session_id,
            user_id=user_id,
            dataset_name=dataset_name,
            entry_type=entry_type,
            title=title,
            content=content,
            item_name=item_name,
            price=price,
            source_kind=source_kind,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        logger.info(
            "Memory dataset entry saved: session_id=%s dataset=%s type=%s title=%s",
            session_id,
            dataset_name,
            entry_type,
            title[:80],
        )

    def _save_describe_dataset_entries(
        self,
        session_id: str,
        user_input: str,
        item_name: Optional[str],
        market: dict,
        analogs: list[str],
    ) -> None:
        median = market.get("median_price")
        market_range = market.get("market_range")
        client_price = market.get("client_price")
        explanation = market.get("explanation")

        if item_name:
            self._save_dataset_entry(
                session_id=session_id,
                dataset_name="analyses",
                entry_type="analyzed_item",
                title=item_name,
                content=(
                    f"Input: {user_input}. "
                    f"Median market price: {median}. "
                    f"Market range: {market_range}. "
                    f"Client price: {client_price}. "
                    f"Explanation: {explanation}"
                ),
                item_name=item_name,
                price=client_price,
                source_kind="describe",
                metadata={"market_report": market},
            )

        if client_price is not None and item_name:
            self._save_dataset_entry(
                session_id=session_id,
                dataset_name="preferences",
                entry_type="client_price_expectation",
                title=f"{item_name} expected price",
                content=f"Client expected price for {item_name}: {client_price}",
                item_name=item_name,
                price=client_price,
                source_kind="describe",
            )

        for analog in analogs[:5]:
            self._save_dataset_entry(
                session_id=session_id,
                dataset_name="analogs",
                entry_type="suggested_analog",
                title=analog,
                content=f"Suggested analog for {item_name or user_input}: {analog}",
                item_name=item_name,
                source_kind="describe",
            )

    def _save_document_dataset_entries(
        self,
        session_id: str,
        file_name: str,
        result: dict,
    ) -> None:
        item_name = result.get("item_name")
        declared_price = result.get("declared_price")
        characteristics = result.get("key_characteristics") or {}
        warnings = result.get("warnings") or []
        price_check = result.get("price_check") or {}

        self._save_dataset_entry(
            session_id=session_id,
            dataset_name="documents",
            entry_type="document_analysis",
            title=file_name,
            content=(
                f"Document: {file_name}. "
                f"Extracted item: {item_name}. "
                f"Declared price: {declared_price}. "
                f"Price verdict: {price_check.get('verdict')}. "
                f"Warnings: {', '.join(str(w) for w in warnings) if warnings else 'none'}."
            ),
            item_name=item_name,
            price=declared_price,
            source_kind="document",
            metadata={"characteristics": characteristics, "price_check": price_check},
        )

        for key, value in list(characteristics.items())[:10]:
            self._save_dataset_entry(
                session_id=session_id,
                dataset_name="document_characteristics",
                entry_type="characteristic",
                title=f"{item_name or file_name}: {key}",
                content=f"{key}: {value}",
                item_name=item_name,
                source_kind="document",
            )

        for warning in warnings[:10]:
            self._save_dataset_entry(
                session_id=session_id,
                dataset_name="document_warnings",
                entry_type="warning",
                title=file_name,
                content=str(warning),
                item_name=item_name,
                source_kind="document",
            )
