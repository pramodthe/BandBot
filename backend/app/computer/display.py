"""Safe, persistent metadata for the computer-use UI. Never stores typed text or URL credentials."""
from uuid import uuid4

from app.persistence.models import ComputerDisplay, db


def display_state(key: str) -> ComputerDisplay:
    with db() as session:
        return session.get(ComputerDisplay, key) or ComputerDisplay(key=key)


def update_display(key: str, **values) -> None:
    with db() as session:
        row = session.get(ComputerDisplay, key) or ComputerDisplay(key=key)
        for name, value in values.items():
            setattr(row, name, value)
        session.add(row)
        session.commit()


def record_step(key: str, action: str, target: str, *, x: float | None = None, y: float | None = None) -> None:
    with db() as session:
        row = session.get(ComputerDisplay, key) or ComputerDisplay(key=key)
        step = {"id": uuid4().hex, "action": action, "target": target}
        if x is not None and y is not None:
            step.update(x=x, y=y)
        row.steps = [*row.steps[-19:], step]
        session.add(row)
        session.commit()
