from __future__ import annotations

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
class TurnResult:
    session_id: str
    status: str
    output: str | None = None
    pending_approvals: list[PendingApproval] = field(default_factory=list)
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


def create_agent_session(client: TrueForge, agent: dict[str, Any]) -> str:
    payload: Any
    if "name" in agent:
        payload = SessionAgentNameRef(name=agent["name"])
    else:
        payload = agent
    session = client.sessions.create(agent=payload)
    return session.data.id


def run_turn(
    client: TrueForge,
    session_id: str,
    input_items: list[dict[str, Any]],
) -> TurnResult:
    events: dict[str, Any] = {}
    pending: list[PendingApproval] = []
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

        if event_type == "tool.approval_required":
            for ref in getattr(event, "tool_calls", None) or []:
                msg = events.get(ref.source_event_id)
                if getattr(msg, "type", None) != "model.message":
                    continue
                call = next(
                    (tc for tc in (getattr(msg, "tool_calls", None) or []) if tc.id == ref.id),
                    None,
                )
                if call is None:
                    continue
                pending.append(
                    PendingApproval(
                        thread_id=thread_id or "main",
                        tool_call_id=ref.id,
                        tool_name=call.tool_info.name,
                        arguments=getattr(call.function, "arguments", None) or "{}",
                    )
                )

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
        events=list(events.values()),
    )


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

    approvals = [
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
        for item in result.pending_approvals
    ]
    return run_turn(client, session_id, approvals)
