from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from trueforge_sdk import SessionAgentNameRef, TrueForge
from trueforge_sdk.events import is_event_delta, merge_event_delta

ApprovalDecision = Literal["allow", "deny", "pause"]


@dataclass
class PendingApproval:
    thread_id: str
    tool_call_id: str
    tool_name: str
    arguments: str


@dataclass
class PendingQuestion:
    thread_id: str
    tool_call_id: str
    question: str
    options: list[str] = field(default_factory=list)


@dataclass
class TurnResult:
    session_id: str
    status: str
    output: str | None = None
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    pending_questions: list[PendingQuestion] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)


def _message_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            else:
                text = getattr(part, "text", None)
                if text is None and isinstance(part, dict):
                    text = part.get("text")
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return None


def create_agent_session(client: TrueForge, agent: dict[str, Any], metadata: dict[str, str] | None = None) -> str:
    payload: Any
    if "name" in agent:
        payload = SessionAgentNameRef(name=agent["name"])
    else:
        payload = agent
    session = client.sessions.create(agent=payload, **({"metadata": metadata} if metadata else {}))
    return session.data.id


def _find_call(events: dict[str, Any], ref: Any) -> Any:
    msg = events.get(ref.source_event_id)
    if getattr(msg, "type", None) != "model.message":
        return None
    return next((tc for tc in (getattr(msg, "tool_calls", None) or []) if tc.id == ref.id), None)


def _collect_pending(
    action: Any,
    events: dict[str, Any],
    approvals: list[PendingApproval],
    questions: list[PendingQuestion],
) -> None:
    """Turn a tool.approval_required / tool.response_required action into pending items."""
    kind = getattr(action, "type", None)
    thread_id = getattr(action, "thread_id", None) or "main"
    for ref in getattr(action, "tool_calls", None) or []:
        call = _find_call(events, ref)
        if call is None:
            continue
        arguments = getattr(call.function, "arguments", None) or "{}"
        if kind == "tool.approval_required":
            approvals.append(
                PendingApproval(
                    thread_id=thread_id,
                    tool_call_id=ref.id,
                    tool_name=call.tool_info.name,
                    arguments=arguments,
                )
            )
        elif kind == "tool.response_required":
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                args = {}
            questions.append(
                PendingQuestion(
                    thread_id=thread_id,
                    tool_call_id=ref.id,
                    question=str(args.get("question") or arguments),
                    options=[str(o) for o in args.get("options") or []],
                )
            )


def run_turn(
    client: TrueForge,
    session_id: str,
    input_items: list[dict[str, Any]],
) -> TurnResult:
    events: dict[str, Any] = {}
    pending: list[PendingApproval] = []
    questions: list[PendingQuestion] = []
    status = "unknown"
    output: str | None = None

    stream = client.sessions.create_turn_stream(session_id=session_id, input=input_items)
    for event in stream:
        event_id = getattr(event, "id", None)
        if is_event_delta(event):
            base = events.get(event_id)
            if base is not None:
                merge_event_delta(base, event)
        elif event_id:
            events[event_id] = event

        event_type = getattr(event, "type", None)
        thread_id = getattr(event, "thread_id", None)

        if event_type == "model.message.delta" and thread_id == "main":
            print(getattr(event, "content", None) or "", end="", flush=True)

        if event_type in ("tool.approval_required", "tool.response_required"):
            _collect_pending(event, events, pending, questions)

        if event_type == "turn.done":
            state = event.state
            status = state.status
            if status == "done":
                done_output = getattr(state, "output", None)
                output = _message_content(getattr(done_output, "content", None))

    return TurnResult(
        session_id=session_id,
        status=status,
        output=output,
        pending_approvals=pending,
        pending_questions=questions,
        events=list(events.values()),
    )


def pending_actions(client: TrueForge, session_id: str) -> tuple[list[PendingApproval], list[PendingQuestion]]:
    """What the session's last turn is waiting on: tool approvals and questions for the user."""
    events: dict[str, Any] = {}
    last_done = None
    for item in client.sessions.list_events(session_id=session_id):
        event = getattr(item, "event", item)
        event_id = getattr(event, "id", None)
        if event_id:
            events[event_id] = event
        if last_done is None and getattr(event, "type", None) == "turn.done":
            last_done = event  # events are listed newest first
    approvals: list[PendingApproval] = []
    questions: list[PendingQuestion] = []
    if last_done is not None and getattr(last_done.state, "status", None) == "done":
        for action in getattr(last_done.state, "required_actions", None) or []:
            _collect_pending(action, events, approvals, questions)
    return approvals, questions


def _approval_items(
    pending: list[PendingApproval], decision: ApprovalDecision, deny_reason: str
) -> list[dict[str, Any]]:
    return [
        {
            "type": "user.tool_approval",
            "thread_id": item.thread_id,
            "tool_call_id": item.tool_call_id,
            "approval": (
                {"status": "allow"}
                if decision == "allow"
                else {"status": "deny", "reason": deny_reason}
            ),
        }
        for item in pending
    ]


def resume_session(
    client: TrueForge,
    session_id: str,
    *,
    decision: ApprovalDecision | None = None,
    answer: str | None = None,
    deny_reason: str = "denied by operator",
) -> TurnResult:
    """Answer whatever a paused session is waiting on: approvals with `decision`, questions with `answer`."""
    approvals, questions = pending_actions(client, session_id)
    if not approvals and not questions:
        raise ValueError(f"session {session_id} has nothing pending; send a message instead")
    if approvals and decision not in ("allow", "deny"):
        raise ValueError("session is waiting for a tool approval; pass allow or deny")
    if questions and answer is None:
        raise ValueError("session is waiting for an answer to a question; pass an answer")
    items = _approval_items(approvals, decision, deny_reason) if approvals else []
    items += [
        {"type": "user.tool_response", "thread_id": q.thread_id, "tool_call_id": q.tool_call_id, "content": answer}
        for q in questions
    ]
    return run_turn(client, session_id, items)


def run_agent_message(
    client: TrueForge,
    *,
    message: str,
    agent_name: str | None = None,
    spec: dict[str, Any] | None = None,
    session_id: str | None = None,
    on_approve: Callable[[list[PendingApproval]], ApprovalDecision] | None = None,
    deny_reason: str = "denied by operator",
) -> TurnResult:
    if session_id is None:
        agent = {"name": agent_name} if agent_name else {"spec": spec}
        session_id = create_agent_session(client, agent)

    result = run_turn(client, session_id, [{"type": "user.message", "content": message}])
    if not result.pending_approvals:
        return result

    decision: ApprovalDecision = on_approve(result.pending_approvals) if on_approve else "pause"
    if decision == "pause":
        return result

    return run_turn(client, session_id, _approval_items(result.pending_approvals, decision, deny_reason))
