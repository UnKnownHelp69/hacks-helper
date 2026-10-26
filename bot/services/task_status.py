"""Mapping GitHub issue state to a task status."""

from __future__ import annotations

from collections.abc import Iterable

from bot.db.models import TASK_CLOSED, TASK_IN_PROGRESS, TASK_NOT_STARTED

STATUS_TITLES = {
    TASK_NOT_STARTED: "не начато",
    TASK_IN_PROGRESS: "в работе",
    TASK_CLOSED: "закрыто",
}

STATUS_ICONS = {
    TASK_NOT_STARTED: "⚪",
    TASK_IN_PROGRESS: "🟡",
    TASK_CLOSED: "✅",
}


def determine_status(
    issue_state: str,
    labels: Iterable[str],
    in_progress_label: str,
    has_open_linked_pr: bool,
) -> str:
    """Compute a task status.

    * closed issue                                   -> closed
    * open issue with an open linked PR or the label -> in progress
    * otherwise                                      -> not started
    """
    if issue_state.lower() == "closed":
        return TASK_CLOSED
    wanted = in_progress_label.strip().lower()
    if has_open_linked_pr or any(label.strip().lower() == wanted for label in labels):
        return TASK_IN_PROGRESS
    return TASK_NOT_STARTED


def has_label(labels: Iterable[str], label: str) -> bool:
    wanted = label.strip().lower()
    return any(item.strip().lower() == wanted for item in labels)
