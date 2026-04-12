"""Dataclasses for scraper results."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CaseResult:
    appeal_number: int
    bench: str
    app_type: str
    year: int
    found: bool
    title: Optional[str] = None
    parties: Optional[str] = None
    status: Optional[str] = None
    filed_on: Optional[str] = None
    assessment_year: Optional[str] = None
    bench_alloted: Optional[str] = None
    pdf_urls: list[str] = field(default_factory=list)
    saved_files: list[str] = field(default_factory=list)
    attempts: int = 1
    note: str = ""

    @property
    def downloaded(self) -> bool:
        return self.found and bool(self.saved_files)

    @property
    def missing_pdf(self) -> bool:
        return self.found and not self.saved_files


@dataclass
class RunSummary:
    bench: str
    app_type: str
    year_range: list[int]
    appeal_range: tuple[int, int]
    downloaded: int = 0
    skipped: int = 0
    missing_pdf: int = 0
    not_found: int = 0
    errors: int = 0
    total_processed: int = 0
