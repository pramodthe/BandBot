"""The installed assistant-ui vocabulary shared by the model, renderer and input validator."""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

CATALOG = json.loads(Path(__file__).with_name("ui_catalog.json").read_text(encoding="utf-8"))
CONTROLS = {"Input", "Select", "Checkbox", "CheckboxGroup", "RadioGroup", "Slider", "DatePicker"}
NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FORMATS = FormatChecker()


def normalize_ui(ui: dict) -> dict:
    """Accept upstream spec node keys while preserving data records verbatim."""
    count = 0
    def visit(node, depth=0):
        nonlocal count
        count += 1
        if count > 200 or depth > 12:
            raise ValueError("UI allows at most 200 nodes and 12 nesting levels")
        if not isinstance(node, dict):
            return node
        result = dict(node)
        for upstream, local in (("$type", "_type"), ("$key", "_key")):
            if upstream in result:
                if local in result and result[local] != result[upstream]:
                    raise ValueError("Conflicting UI node keys")
                result[local] = result.pop(upstream)
        if "children" in result:
            children = result["children"]
            result["children"] = [visit(child, depth + 1) for child in children] if isinstance(children, list) else visit(children, depth + 1)
        return result
    return visit(ui)


def validate_ui(ui: dict, *, input_mode: bool = False, required: list[str] = ()) -> dict:
    """Check each component's real schema and bound the tree before it reaches React."""
    if len(json.dumps(ui, allow_nan=False).encode()) > 64000:
        raise ValueError("UI must fit within 64 KB")
    fields: dict[str, dict] = {}
    count = 0

    def check_data(value, depth=0):
        if depth > 20:
            raise ValueError("UI data is nested too deeply")
        if isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("UI data allows at most 1000 items per list")
            for child in value:
                check_data(child, depth + 1)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in {"url", "src", "href", "image", "poster", "artwork", "favicon"} and isinstance(child, str) and child:
                    if urlparse(child).scheme != "https":
                        raise ValueError("Media and citation URLs must use HTTPS")
                check_data(child, depth + 1)
        elif isinstance(value, str) and len(value) > 8000:
            raise ValueError("UI text is too long")

    def visit(node, depth=0):
        nonlocal count
        count += 1
        if count > 200 or depth > 12:
            raise ValueError("UI allows at most 200 nodes and 12 nesting levels")
        if isinstance(node, str):
            if len(node) > 8000:
                raise ValueError("UI text is too long")
            return
        if not isinstance(node, dict) or not isinstance(node.get("_type"), str) or node["_type"] not in CATALOG["components"]:
            raise ValueError("Use a component from ui_catalog")
        if "_key" in node and type(node["_key"]) not in {str, int, float}:
            raise ValueError("Component keys must be text or numbers")
        kind = node["_type"]
        schema = CATALOG["components"][kind]["schema"]
        props = {k: v for k, v in node.items() if k not in {"_type", "_key", "children"}}
        check_data(props)
        if set(props) - schema.get("properties", {}).keys():
            raise ValueError(f"Unsupported property on {kind}")
        error = next(Draft202012Validator(schema, format_checker=_FORMATS).iter_errors(props), None)
        if error:
            raise ValueError(f"Invalid {kind} property: {error.message[:180]}")
        # Model-authored CSS and action objects are not capabilities. Host-owned controls resume tasks.
        if "background" in props or props.get("inputType") == "password":
            raise ValueError("Use theme styling and never collect credentials")
        if props.get("confirm") or props.get("cancel") or props.get("asForm") or kind == "Form":
            raise ValueError("The app supplies the response form and its actions; compose its contents only")
        if kind == "Button":
            raise ValueError("The app supplies Continue and steering controls; use choices for decisions")
        for key in ("url", "src", "href"):
            if key in props and props[key] and urlparse(str(props[key])).scheme != "https":
                raise ValueError("Media and citation URLs must use HTTPS")
        if kind in CONTROLS:
            if not input_mode:
                raise ValueError("Use ask_user for input controls")
            name = props.get("name", "")
            if not str(props.get("label", "")).strip():
                raise ValueError("Each input needs a visible label")
            if not NAME.fullmatch(name) or name in fields:
                raise ValueError("Each input needs a unique snake_case name")
            if len(fields) >= 24 or len(props.get("options", [])) > 30:
                raise ValueError("Use at most 24 inputs and 30 options per input")
            if kind == "Slider" and (props["min"] > props["max"] or props.get("step", 1) <= 0):
                raise ValueError("Invalid slider range")
            fields[name] = node
        children = node.get("children", [])
        for child in children if isinstance(children, list) else [children]:
            visit(child, depth + 1)
    visit(ui)
    if set(required) - fields.keys():
        raise ValueError("Required inputs must name controls in the UI")
    return fields


def validate_answer(ui: dict, required: list[str], values: dict) -> dict:
    fields = validate_ui(ui, input_mode=True, required=required)
    if set(values) - fields.keys():
        raise ValueError("Response contains an unknown field")
    if len(json.dumps(values, allow_nan=False).encode()) > 16000:
        raise ValueError("Response is too large")
    clean = {}
    for name, node in fields.items():
        value, kind = values.get(name), node["_type"]
        empty = value is None or value == "" or value == []
        if name in required and (empty or (kind == "Checkbox" and value is not True)
                                 or (isinstance(value, str) and not value.strip())):
            raise ValueError(f"{node.get('label', name)} is required")
        if empty:
            continue
        if kind == "Checkbox":
            if type(value) is not bool:
                raise ValueError("Checkbox values must be boolean")
        elif kind == "Slider" or (kind == "Input" and node.get("inputType") == "number"):
            if type(value) not in (str, int, float) or isinstance(value, bool):
                raise ValueError("Enter a number")
            try:
                value = float(value)
            except (ValueError, OverflowError):
                raise ValueError("Enter a finite number") from None
            if not math.isfinite(value) or (kind == "Slider" and not node["min"] <= value <= node["max"]):
                raise ValueError("Number is outside the permitted range")
        elif kind == "CheckboxGroup":
            options = {o["value"] for o in node["options"]}
            if not isinstance(value, list) or any(not isinstance(v, str) or v not in options for v in value) or len(set(value)) != len(value):
                raise ValueError("Choose valid options")
        else:
            if not isinstance(value, str) or len(value) > 4000:
                raise ValueError("Input must be text of at most 4000 characters")
            if kind in {"Select", "RadioGroup"} and value not in {o["value"] for o in node["options"]}:
                raise ValueError("Choose a valid option")
            if kind == "DatePicker":
                try:
                    parser = {"date": date.fromisoformat, "time": time.fromisoformat,
                              "datetime": datetime.fromisoformat}[node.get("inputType", "date")]
                    parsed = parser(value)
                    if node.get("min") and parsed < parser(node["min"]):
                        raise ValueError
                    if node.get("max") and parsed > parser(node["max"]):
                        raise ValueError
                except (ValueError, TypeError):
                    raise ValueError("Choose a valid date or time within the permitted range") from None
        clean[name] = value
    return clean
