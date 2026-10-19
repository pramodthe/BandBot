"""Trusted, packaged UI guidance. No skill-controlled paths are read from the host."""
from pathlib import Path

import yaml

UI_SKILL_NAMES = ("master-playbook", "advanced-visualization", "svg-diagrams")


def _load(name: str) -> tuple[str, str, str]:
    source = (Path(__file__).parent / name / "SKILL.md").read_text(encoding="utf-8")
    _, metadata, instructions = source.split("---", 2)
    info = yaml.safe_load(metadata)
    assert info["name"] == name
    return name, info["description"], instructions.strip()


UI_SKILLS = tuple(_load(name) for name in UI_SKILL_NAMES)
UI_SKILL_FILES = {name: (Path(__file__).parent / name).read_text(encoding="utf-8") for name in ("LICENSE", "PROVENANCE.md")}
