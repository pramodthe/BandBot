"""Offline contracts for the Elements registry, upstream specs and agent discovery."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.main import app
from app.runtime.elements import ELEMENTS, EXAMPLES
from app.runtime.tools.generative import generative_tools
from app.runtime.ui import CATALOG, normalize_ui, validate_ui



def test_complete_collection_has_local_source_and_all_generative_examples():
    assert len(ELEMENTS) == len({entry["id"] for entry in ELEMENTS}) == 151
    assert len(EXAMPLES) == 22
    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    for entry in ELEMENTS:
        assert entry["source"].startswith("https://www.assistant-ui.com/elements/")
        if entry["category"] == "Generative":
            assert entry["id"] in EXAMPLES
        else:
            assert entry["files"], entry["id"]
            assert all((root / name).is_file() for name in entry["files"]), entry["id"]


def test_upstream_weather_composition_and_data_keys_are_preserved():
    ui = {"$type": "Card", "$key": "weather", "children": [
        {"$type": "Text", "value": "94°F"},
    ]}
    result = normalize_ui(ui)
    assert result == {"_type": "Card", "_key": "weather", "children": [{"_type": "Text", "value": "94°F"}]}
    validate_ui(result)
    assert "$type" in ui
    data = {"$type": "Table", "rows": [{"$type": "a data column", "$key": "data"}]}
    assert normalize_ui(data)["rows"] == data["rows"]
    with pytest.raises(ValueError, match="Conflicting"):
        normalize_ui({"$type": "Text", "_type": "Button"})
    nested = {"$type": "Text", "value": "Deep"}
    for _ in range(14):
        nested = {"$type": "Col", "children": [nested]}
    with pytest.raises(ValueError, match="nesting"):
        normalize_ui(nested)


def test_extension_props_are_validated_and_cannot_supply_callbacks_or_unsafe_media():
    validate_ui({"_type": "CodeDiff", "filename": "app.py", "additions": 1, "deletions": 0,
                 "lines": [{"kind": "added", "text": "print('hello')"}]})
    validate_ui({"_type": "ImageGallery", "images": [{"id": "one", "alt": "Example", "src": "https://example.com/image.png"}]})
    for bad in (
        {"_type": "LinkPreview", "href": "javascript:alert(1)"},
        {"_type": "ImageGallery", "images": [{"id": "one", "alt": "Example", "src": "file:///private"}]},
        {"_type": "ImageGallery", "images": [], "onOpen": "send_email"},
        {"_type": "FlowGraph", "nodes": [], "edges": [], "className": "fixed inset-0"},
        {"_type": "NumberTicker", "value": "bad", "label": "Count"},
    ):
        with pytest.raises(ValueError):
            validate_ui(bad)


def test_agent_progressive_discovery_and_member_only_http_catalogue(job):
    task, _, user = job
    tools = {tool.name: tool for tool in generative_tools(task)}
    index = tools["ui_elements"].invoke({})
    assert index["total"] == 151
    assert sum(index["categories"].values()) == 151
    assert len(index["examples"]) == 22
    assert tools["ui_elements"].invoke({"example": "generative-weather-current"})["example"]["ui"]["_type"] == "Card"
    assert "error" in tools["ui_elements"].invoke({"example": "missing"})
    assert tools["ui_catalog"].invoke({"components": ["CodeDiff"]})["CodeDiff"]["schema"]
    with TestClient(app) as client:
        path = f"/api/channels/{task.channel_id}/elements"
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"Authorization": f"Bearer {issue_token(user.id)}"})
        assert response.status_code == 200
        assert len(response.json()["elements"]) == 151
        assert set(response.json()["components"]) == set(CATALOG["components"])
