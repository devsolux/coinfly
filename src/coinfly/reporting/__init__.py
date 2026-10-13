"""Realm-qualified frozen reporting services; the desktop never opens SQL."""

from .history_service import HistoryCapture, HistoryQuery, HistoryRecord, HistoryService

__all__ = ["HistoryCapture", "HistoryQuery", "HistoryRecord", "HistoryService"]
