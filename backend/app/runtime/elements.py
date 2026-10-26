"""Provenance and progressive discovery for the complete upstream Elements collection."""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).parent
ELEMENTS = json.loads((_ROOT / "elements_manifest.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((_ROOT / "ui_examples.json").read_text(encoding="utf-8"))


def discover_elements(category: str | None = None) -> list[dict]:
    return [entry for entry in ELEMENTS if category is None or entry["category"].casefold() == category.casefold()]
