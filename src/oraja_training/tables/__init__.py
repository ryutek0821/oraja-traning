"""Difficulty-table retrieval and local chart matching."""

from .classification import (
    DEFAULT_CLASSIFICATION_SOURCES,
    ChartClassification,
    ClassificationBatch,
    ClassificationError,
    ClassificationSourceSpec,
    build_classification_batch,
)
from .fetch import TableData, TableEntry, TableFetchError, fetch_table
from .match import MatchedEntry, MatchReport, TableMatchSummary, resolve

__all__ = [
    "DEFAULT_CLASSIFICATION_SOURCES",
    "ChartClassification",
    "ClassificationBatch",
    "ClassificationError",
    "ClassificationSourceSpec",
    "MatchReport",
    "MatchedEntry",
    "TableData",
    "TableEntry",
    "TableFetchError",
    "TableMatchSummary",
    "build_classification_batch",
    "fetch_table",
    "resolve",
]
