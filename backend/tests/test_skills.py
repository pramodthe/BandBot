from __future__ import annotations

import asyncio
import io
import stat
import zipfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import AuditEvent, WorkspaceRole, db
from app.runtime.harness import build_harness
from app.runtime.skills import ReadOnlySkillsBackend, skills_backend
from app.workspace.skills import SkillContent, SkillError, create_skill, enabled_skill_files, parse_bundle, parse_upload
from helpers import RecordingModel


@pytest.fixture
def skills_job(job):
    task, agent, user = job
    with db() as session:
        if session.get(WorkspaceRole, user.id) is None:
            session.add(WorkspaceRole(user_id=user.id, role="admin"))
            session.commit()
    return task, agent, user, {"Authorization": f"Bearer {issue_token(user.id)}"}


def content(**changes):
    return {"name": f"brief-{uuid4().hex[:10]}", "description": "Prepare a sourced brief.",
            "instructions": "Read the evidence. End the brief with VERIFIED-SKILL.", **changes}


def bundle(name="imported-brief"):
    return f"---\nname: {name}\ndescription: A reusable brief workflow.\n---\n\nRead references/checklist.md before drafting."


def test_library_create_edit_install_disable_remove_and_export(skills_job):
    _, agent, user, auth = skills_job
    url = f"/api/agents/{agent.id}/skills"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        catalogue = client.get(url, headers=auth).json()
        assert catalogue["installed"] == []
        assert any(item["name"] == "research-brief" and item["builtin"] for item in catalogue["library"])
        body = content()
        response = client.post("/api/skills", json={**body, "agent_id": agent.id}, headers=auth)
        assert response.status_code == 200, response.text
        skill = response.json()
        assert skill["created_by_name"] == user.display_name
        assert client.get(url, headers=auth).json()["installed"] == [{"skill_id": skill["id"], "enabled": True}]
        assert "VERIFIED-SKILL" in enabled_skill_files(agent.id)[f"/{skill['name']}/SKILL.md"]
        assert client.post(url, json={"skill_id": skill["id"]}, headers=auth).status_code == 200
        assert len(client.get(url, headers=auth).json()["installed"]) == 1
        response = client.put(f"/api/skills/{skill['id']}", headers=auth, json={
            **body, "instructions": "Updated instructions.", "expected_updated_at": skill["updated_at"]})
        assert response.status_code == 200
        assert client.put(f"/api/skills/{skill['id']}", headers=auth, json={
            **body, "expected_updated_at": skill["updated_at"]}).status_code == 409
        downloaded = client.get(f"/api/skills/{skill['id']}/export", headers=auth)
        assert downloaded.status_code == 200
        parsed, _ = parse_upload(downloaded.content, "portable.zip")
        assert parsed.instructions == "Updated instructions."
        assert client.patch(f"{url}/{skill['id']}", headers=auth, json={"enabled": False}).status_code == 200
        assert enabled_skill_files(agent.id) == {}
        assert client.delete(f"{url}/{skill['id']}", headers=auth).status_code == 200
        assert client.get(f"/api/skills/{skill['id']}", headers=auth).status_code == 200
        assert client.delete(f"/api/skills/{skill['id']}", headers=auth).status_code == 200
        assert client.get(f"/api/skills/{skill['id']}", headers=auth).status_code == 404
    with db() as session:
        assert session.exec(select(AuditEvent).where(AuditEvent.action == "skill_disabled")).first()


def test_skills_require_admin_mutations_and_builtin_skills_are_read_only(skills_job):
    from app.persistence.models import User
    _, agent, _, auth = skills_job
    with db() as session:
        member = User(username=uuid4().hex, display_name="Reader", password_hash="unused")
        session.add(member)
        session.commit()
        member_auth = {"Authorization": f"Bearer {issue_token(member.id)}"}
    url = f"/api/agents/{agent.id}/skills"
    with TestClient(app) as client:
        catalogue = client.get(url, headers=member_auth)
        assert catalogue.status_code == 200
        builtin = next(item for item in catalogue.json()["library"] if item["builtin"])
        assert client.post("/api/skills", headers=member_auth, json=content()).status_code == 403
        assert client.post(url, headers=member_auth, json={"skill_id": builtin["id"]}).status_code == 403
        assert client.put(f"/api/skills/{builtin['id']}", headers=auth, json=content()).status_code == 403
        assert client.delete(f"/api/skills/{builtin['id']}", headers=auth).status_code == 403
        assert client.get(f"/api/skills/{builtin['id']}/export").status_code == 401
        assert client.get("/api/agents/999999999/skills", headers=auth).status_code == 404


def test_markdown_zip_and_folder_import_preserve_supporting_files(skills_job):
    _, agent, _, auth = skills_job
    with TestClient(app) as client:
        raw = bundle(f"markdown-{uuid4().hex[:8]}")
        response = client.post(f"/api/skills/import?filename=SKILL.md&agent_id={agent.id}",
                               content=raw, headers={**auth, "Content-Type": "text/markdown"})
        assert response.status_code == 200, response.text
        files = [{"path": "folder/SKILL.md", "content": bundle(f"folder-{uuid4().hex[:8]}")},
                 {"path": "folder/references/checklist.md", "content": "Check every source."}]
        response = client.post(f"/api/skills/import?agent_id={agent.id}", json={"files": files}, headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["files"] == {"references/checklist.md": "Check every source."}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("example/SKILL.md", bundle(f"zip-{uuid4().hex[:8]}"))
            archive.writestr("example/references/checklist.md", "Source checklist")
        response = client.post("/api/skills/import?filename=example.zip", content=output.getvalue(), headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["files"] == {"references/checklist.md": "Source checklist"}


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:/secret.py", "folder/../bad.py", "bad\\path.py"])
def test_import_rejects_unsafe_paths(path):
    with pytest.raises(SkillError, match="path"):
        parse_bundle([{"path": "SKILL.md", "content": bundle()}, {"path": path, "content": "bad"}])


def test_import_validation_is_bounded_and_rejects_bad_metadata(skills_job):
    _, agent, _, auth = skills_job
    with pytest.raises(SkillError):
        parse_bundle([{"path": "one/SKILL.md", "content": bundle()}, {"path": "two/SKILL.md", "content": bundle()}])
    with pytest.raises(SkillError):
        parse_upload(b"not a ZIP", "skill.zip")
    with pytest.raises(SkillError):
        parse_upload(b"\xff\x00", "skill.md")
    with pytest.raises(ValidationError):
        SkillContent(**content(name="../not-a-skill"))
    with TestClient(app) as client:
        url = f"/api/skills/import?filename=SKILL.md&agent_id={agent.id}"
        assert client.post(url, headers=auth, content="---\nname: [bad]\n---\ntext").status_code == 422
        assert client.post(url, headers=auth, content=b"a" * (2 * 1024 * 1024 + 1)).status_code == 413


def test_zip_import_rejects_links_and_compressed_oversize_files():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("skill/references/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/private/file")
        archive.writestr("skill/SKILL.md", bundle())
    with pytest.raises(SkillError, match="Symbolic links"):
        parse_upload(output.getvalue(), "links.zip")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill/SKILL.md", bundle())
        archive.writestr("skill/references/large.txt", "a" * (1024 * 1024))
    with pytest.raises(SkillError, match="1 MiB"):
        parse_upload(output.getvalue(), "large.zip")


def test_installed_skills_are_scoped_read_only_and_used_by_real_harness(skills_job):
    task, agent, user, _ = skills_job
    body = content()
    with db() as session:
        skill = create_skill(session, SkillContent(**body), user.id,
                             files={"references/checklist.md": "VERIFY-CHECKLIST"}, agent_id=agent.id)
        session.commit()
    backend = skills_backend(agent.id)
    assert backend.read(f"/skills/{skill.name}/SKILL.md").error is None
    assert backend.write(f"/skills/{skill.name}/new.md", "bad").error
    assert backend.edit(f"/skills/{skill.name}/SKILL.md", "Read", "Ignore").error
    assert enabled_skill_files(agent.id + 999999) == {}
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "read_file", "id": "skill-read", "args": {
            "file_path": f"/skills/{skill.name}/SKILL.md"}}]),
        AIMessage(content="VERIFIED-SKILL"),
    ])
    graph = build_harness(task, agent, None, model=model)
    result = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "Prepare a brief using the installed workflow"}]}))
    assert result["messages"][-1].content == "VERIFIED-SKILL"
    first_prompt = " ".join(str(message.content) for message in model.seen[0])
    assert skill.name in first_prompt and "VERIFIED-SKILL" not in first_prompt
    assert any("VERIFIED-SKILL" in str(message.content) for message in model.seen[1] if message.type == "tool")
    assert not any(path.startswith("/skills/") for path in result.get("files", {}))
    direct = ReadOnlySkillsBackend({"/one/SKILL.md": "hello"})
    assert direct.delete("/one/SKILL.md").error
    assert direct.upload_files([("/one/SKILL.md", b"bad")])[0].error == "permission_denied"


def test_internal_helper_can_read_installed_supporting_files(skills_job):
    task, agent, user, _ = skills_job
    with db() as session:
        skill = create_skill(session, SkillContent(**content()), user.id,
                             files={"references/checklist.md": "HELPER-CHECKLIST"}, agent_id=agent.id)
        session.commit()
    model = RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "id": "delegate-skill", "args": {
            "subagent_type": "critic", "description": "Read the installed brief skill checklist and review the draft."}}]),
        AIMessage(content="", tool_calls=[{"name": "read_file", "id": "helper-skill-read", "args": {
            "file_path": f"/skills/{skill.name}/references/checklist.md"}}]),
        AIMessage(content="Reviewed using HELPER-CHECKLIST."),
        AIMessage(content="Review complete."),
    ])
    graph = build_harness(task, agent, None, model=model)
    result = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "Have the critic review the draft."}]}))
    assert result["messages"][-1].content == "Review complete."
    assert skill.name in " ".join(str(message.content) for message in model.seen[1])
    assert any("HELPER-CHECKLIST" in str(message.content) for message in model.seen[2] if message.type == "tool")
