"""Validated skill definitions, portable imports, and per-agent installation."""
from __future__ import annotations

import io
import re
import stat
import zipfile
import zlib
from pathlib import PurePosixPath
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.persistence.models import AgentProfile, AgentSkill, SkillDefinition, User, db, utcnow
from app.runtime.builtin_skills import UI_SKILLS, UI_SKILL_NAMES, UI_SKILL_FILES

MAX_UPLOAD = 2 * 1024 * 1024
MAX_BUNDLE = 1024 * 1024
MAX_FILES = 40
MAX_AGENT_SKILLS = 30
MAX_LIBRARY = 200


class SkillError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class SkillContent(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1, max_length=50000)

    @field_validator("name", "description", "instructions", mode="before")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Use lowercase letters, numbers, and single hyphens for the skill name")
        return value


BUILTINS = (
    ("research-brief", "Research a question, compare evidence, and deliver a concise brief with sources.",
     "# Research brief\n\nUse for research and comparisons.\n\n"
     "1. Define the question and read relevant channel history and memory.\n"
     "2. Use available search and page tools when current evidence is needed. Prefer primary sources.\n"
     "3. Compare findings, resolve conflicting claims, and distinguish evidence from inference.\n"
     "4. Deliver the answer with source links, uncertainty, and useful next steps. Never invent a search or source."),
    ("clear-writing", "Turn notes and evidence into clear writing for the intended reader.",
     "# Clear writing\n\nUse for drafts, reports, and editing.\n\n"
     "Identify the reader, purpose, and requested format. Lead with the main point. Use plain language, "
     "concrete examples, and short connected paragraphs. Preserve factual qualifications and sources. "
     "Check names, numbers, and consistency before returning the finished draft. Drafting does not authorize publishing."),
    ("review-work", "Review a deliverable against the request and identify concrete errors and gaps.",
     "# Review work\n\nUse when checking a plan, report, or code change.\n\n"
     "Restate the acceptance criteria. Inspect the actual deliverable and its evidence. Check correctness, "
     "missing requirements, assumptions, and edge cases. Run appropriate available checks when useful. "
     "Report actionable findings by importance, with locations and suggested fixes. State what was verified "
     "and what remains uncertain. Do not claim a test ran unless a tool ran it."),
)


def seed_library(session: Session) -> None:
    for name, description, instructions in BUILTINS + UI_SKILLS:
        skill_id = f"builtin-{name}"
        existing = session.get(SkillDefinition, skill_id)
        if existing and existing.builtin and name in UI_SKILL_NAMES:
            if (existing.instructions, existing.description, existing.files) != (instructions, description, UI_SKILL_FILES):
                existing.instructions = instructions
                existing.description = description
                existing.files = UI_SKILL_FILES
                existing.updated_at = utcnow()
                session.add(existing)
        elif existing is None:
            # Leave a user-created skill with the same name intact.
            if session.exec(select(SkillDefinition).where(SkillDefinition.name == name)).first():
                continue
            session.add(SkillDefinition(id=skill_id, name=name, description=description,
                                        instructions=instructions, builtin=True,
                                        files=UI_SKILL_FILES if name in UI_SKILL_NAMES else {}))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # Concurrent catalogue requests may seed the same defaults.


def skill_or_error(session: Session, skill_id: str) -> SkillDefinition:
    skill = session.get(SkillDefinition, skill_id)
    if skill is None:
        raise SkillError("Skill not found", 404)
    return skill


def check_install(session: Session, agent_id: int, skill_id: str | None = None) -> None:
    if session.get(AgentProfile, agent_id) is None:
        raise SkillError("Agent not found", 404)
    if skill_id and session.get(AgentSkill, (agent_id, skill_id)):
        return
    count = session.exec(select(func.count()).select_from(AgentSkill).where(AgentSkill.agent_id == agent_id)).one()
    if count >= MAX_AGENT_SKILLS:
        raise SkillError(f"An agent can have up to {MAX_AGENT_SKILLS} installed skills", 409)


def create_skill(session: Session, content: SkillContent, user_id: int, *, files: dict[str, str] | None = None,
                 agent_id: int | None = None) -> SkillDefinition:
    if session.exec(select(func.count()).select_from(SkillDefinition)).one() >= MAX_LIBRARY:
        raise SkillError(f"The workspace can hold up to {MAX_LIBRARY} skills", 409)
    if session.exec(select(SkillDefinition).where(SkillDefinition.name == content.name)).first():
        raise SkillError("A skill with that name already exists; edit it or choose a different name", 409)
    if agent_id is not None:
        check_install(session, agent_id)
    skill = SkillDefinition(id=uuid4().hex, **content.model_dump(), files=files or {}, created_by=user_id)
    session.add(skill)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise SkillError("A skill with that name already exists", 409) from exc
    if agent_id is not None:
        session.add(AgentSkill(agent_id=agent_id, skill_id=skill.id))
    return skill


def serialize_skill(session: Session, skill: SkillDefinition, *, detail: bool = False) -> dict:
    creator = session.get(User, skill.created_by) if skill.created_by else None
    value = {"id": skill.id, "name": skill.name, "description": skill.description, "builtin": skill.builtin,
             "created_by_name": "BAND_BOTS" if skill.builtin else creator.display_name if creator else "Workspace",
             "file_count": len(skill.files), "updated_at": skill.updated_at.isoformat()}
    if detail:
        value.update(instructions=skill.instructions, files=skill.files)
    return value


def skill_markdown(skill: SkillDefinition) -> str:
    metadata = yaml.safe_dump({"name": skill.name, "description": skill.description}, sort_keys=False,
                              allow_unicode=True).strip()
    return f"---\n{metadata}\n---\n\n{skill.instructions}\n"


def safe_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > 240 or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        raise SkillError("Skill files need safe relative paths")
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise SkillError("Skill files need safe relative paths; parent paths are not allowed")
    return path


def parse_bundle(files: list[dict]) -> tuple[SkillContent, dict[str, str]]:
    if not files or len(files) > MAX_FILES:
        raise SkillError(f"Import a skill containing 1–{MAX_FILES} UTF-8 text files")
    normalized: dict[str, str] = {}
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise SkillError("Each skill file needs a path and text content")
        path = safe_path(item.get("path"))
        text = item.get("content")
        if not isinstance(text, str) or "\x00" in text:
            raise SkillError("Skill bundles support UTF-8 text files only")
        if path in normalized:
            raise SkillError("The bundle contains duplicate file paths")
        total += len(text.encode("utf-8"))
        if total > MAX_BUNDLE:
            raise SkillError("Unpacked skill files must be at most 1 MiB", 413)
        normalized[path] = text
    roots = [path for path in normalized if PurePosixPath(path).name == "SKILL.md"]
    if len(roots) != 1:
        raise SkillError("Import one skill at a time, with exactly one SKILL.md")
    root_file = roots[0]
    prefix = root_file.removesuffix("SKILL.md")
    if any(not path.startswith(prefix) for path in normalized):
        raise SkillError("All supporting files must be inside the skill's folder")
    source = normalized.pop(root_file).lstrip("\ufeff").replace("\r\n", "\n")
    match = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", source, re.S)
    if not match or len(match[1]) > 16000:
        raise SkillError("SKILL.md needs YAML frontmatter with name and description")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise SkillError("The SKILL.md YAML frontmatter is invalid") from exc
    if not isinstance(metadata, dict):
        raise SkillError("SKILL.md frontmatter must contain name and description")
    content = SkillContent(name=metadata.get("name"), description=metadata.get("description"),
                           instructions=source[match.end():])
    # Only the validated name/description are used as runtime metadata. Imported
    # tool declarations cannot grant new tools or bypass application policies.
    return content, {path[len(prefix):]: text for path, text in normalized.items()}


def parse_upload(payload: bytes, filename: str) -> tuple[SkillContent, dict[str, str]]:
    if len(payload) > MAX_UPLOAD:
        raise SkillError("Skill uploads must be at most 2 MiB", 413)
    if filename.lower().endswith(".md"):
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeError as exc:
            raise SkillError("SKILL.md must use UTF-8 text") from exc
        return parse_bundle([{"path": "SKILL.md", "content": text}])
    if not filename.lower().endswith(".zip"):
        raise SkillError("Choose a Markdown skill or a ZIP skill bundle")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES * 2 or sum(item.file_size for item in entries) > MAX_BUNDLE:
                raise SkillError("Unpacked skills must be at most 1 MiB and 40 files", 413)
            files = []
            for item in entries:
                path = safe_path(item.filename.rstrip("/"))
                if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise SkillError("Symbolic links and encrypted ZIP files are not supported")
                if item.is_dir() or "__MACOSX" in path.split("/") or PurePosixPath(path).name == ".DS_Store":
                    continue
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise SkillError("Use a standard deflate ZIP archive")
                files.append({"path": path, "content": archive.read(item).decode("utf-8-sig")})
            return parse_bundle(files)
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, NotImplementedError, zlib.error, EOFError, OSError) as exc:
        raise SkillError("The ZIP must contain a readable SKILL.md and UTF-8 supporting files") from exc


def export_skill(skill: SkillDefinition) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{skill.name}/SKILL.md", skill_markdown(skill))
        for path, content in skill.files.items():
            archive.writestr(f"{skill.name}/{safe_path(path)}", content)
    return output.getvalue()


def enabled_skill_files(agent_id: int) -> dict[str, str]:
    with db() as session:
        skills = session.exec(select(SkillDefinition).join(AgentSkill).where(
            AgentSkill.agent_id == agent_id, AgentSkill.enabled == True).order_by(SkillDefinition.name)).all()
    files = {}
    for skill in skills:
        files[f"/{skill.name}/SKILL.md"] = skill_markdown(skill)
        for path, content in skill.files.items():
            files[f"/{skill.name}/{path}"] = content
    return files
