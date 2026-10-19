"""Expose browser-authored UI only for an explicit request in this task, never its history."""
import re


def custom_ui_requested(prompt: str) -> bool:
    text = prompt.lower()
    text = re.sub(r"\b(?:explain|describe|show)\s+how\s+to\s+(?:use|call)\s+`?generate_sandboxed_ui\b", "", text)
    # A negated request does not opt in. Keep unrelated prohibitions (e.g. no computer).
    subject = r"(?:generate_sandboxed_ui|(?:custom|interactive|sandboxed)\s+(?:ui|interface|widget|calculator|simulation)|calculator|simulation|widget|html(?:/css|/javascript)?)"
    text = re.sub(rf"\b(?:do not|don't|dont|never|no need to)\s+(?:use|call|create|build|generate|render|make)\b[^.!?;,\n]{{0,60}}?{subject}[^.!?;,\n]*", "", text)
    text = re.sub(rf"\b(?:without|no)\s+(?:(?:a|an|the|any)\s+)?{subject}[^.!?;,\n]*", "", text)
    if re.search(r"\b(?:use|using|call|via|with)\s+(?:the\s+)?`?generate_sandboxed_ui\b", text):
        return True
    action = r"\b(?:create|build|make|generate|render|design|draw|show|give|write|update|revise|refine|change|fix|(?:i|we)\s+(?:want|need|would like))\b"
    custom = r"\b(?:interactive|interactively|custom\s+(?:ui|interface|widget)|sandboxed\s+(?:ui|interface)|html(?:/css|/javascript)?\s+(?:ui|interface|widget|demo)|three\.?js|3d\s+(?:scene|visualization)|simulation|calculator|widget)\b"
    explanatory = r"\b(?:explain|demonstrate)\b[^.!?;\n]{0,120}(?:\binteractively\b|\b(?:with|using|as)\b[^.!?;\n]{0,40}\binteractive\b)"
    return re.search(rf"{action}[^.!?;\n]{{0,180}}{custom}", text) is not None or re.search(explanatory, text) is not None
