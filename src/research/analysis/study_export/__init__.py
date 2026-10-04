"""Owner-only raw data export of one study as a ZIP of CSV/JSONL files.

Kept apart from the analytics read model: an export can include stored content
(only for a study that captured it, and only when asked), while analytics
responses never do. Participants appear only by enrollment id and study-local
participant code; account identity is never joined, and retention tombstones
are never exported.
"""
