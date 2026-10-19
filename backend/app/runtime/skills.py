"""Expose this agent's installed skills as a read-only Deep Agents mount."""
from __future__ import annotations

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult
from deepagents.backends.utils import create_file_data

from app.workspace.skills import enabled_skill_files

READ_ONLY = "Installed skills are read-only. Ask a workspace admin to change them in the agent's Skills tab."


class ReadOnlySkillsBackend(StateBackend):
    """Use the library's text/file operations without touching graph or host files."""

    def __init__(self, files: dict[str, str]):
        self._files = {path: create_file_data(content) for path, content in files.items()}

    def _read_files(self) -> dict:
        return self._files

    def _send_files_update(self, update: dict) -> None:
        raise PermissionError(READ_ONLY)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=READ_ONLY)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=READ_ONLY)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=READ_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


def skills_backend(agent_id: int):
    files = enabled_skill_files(agent_id)
    if not files:
        return None
    return CompositeBackend(default=StateBackend(), routes={"/skills/": ReadOnlySkillsBackend(files)})
