"""Role-aware RAG query router.

The project rule is ONE central LLM across every role, but each role needs a
different corpus slice, retrieval depth, and metadata filter. This router
wraps ``GroundedQAEngine.answer_question`` and applies role-specific defaults
before delegating to the shared engine.

It deliberately does not own prompt composition — role-specific prompt
builders live under ``app.prompts``. The router's only job is to pick the
right retrieval scope and invoke the engine consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Literal

from app.guardrails.role_validator import Role, validate_query_for_role
from app.rag.query_engine.engine import (
    GroundedAnswerResult,
    GroundedQAEngine,
    StreamChunk,
    StreamDone,
)
from app.schemas.student import ConversationTurn, LearningState

RouteRole = Role


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Retrieval configuration for a particular role."""

    top_k: int
    default_namespace: str | None = None
    include_textbooks: bool = True


_DEFAULT_SCOPES: dict[RouteRole, RetrievalScope] = {
    "student": RetrievalScope(top_k=5),
    "teacher": RetrievalScope(top_k=8),
    "parent": RetrievalScope(top_k=4),
    "admin": RetrievalScope(top_k=6),
}


class RoleAwareQueryRouter:
    """Dispatch grounded QA requests to the shared engine with role-scoped retrieval."""

    def __init__(
        self,
        *,
        engine: GroundedQAEngine,
        scopes: dict[RouteRole, RetrievalScope] | None = None,
    ) -> None:
        self._engine = engine
        self._scopes = dict(scopes) if scopes else dict(_DEFAULT_SCOPES)

    def scope_for(self, role: RouteRole) -> RetrievalScope:
        if role not in self._scopes:
            raise KeyError(f"No retrieval scope configured for role '{role}'.")
        return self._scopes[role]

    def route(
        self,
        *,
        role: RouteRole,
        question: str,
        student_class: str,
        subject: str | None = None,
        history: list[ConversationTurn] | None = None,
        learning_state: LearningState | None = None,
        namespace: str | None = None,
    ) -> GroundedAnswerResult:
        """Route a grounded QA request for the given role.

        The caller is expected to run ``validate_query_for_role`` prior to
        this; the router enforces it defensively as a second layer.
        """

        # Pass history so the educational-check bypass for clarifier-continuation
        # turns works on this defensive second layer too. Without it, a valid
        # follow-up like "Kinematics, specifically motion graphs" gets refused
        # because the fragment doesn't look educational in isolation.
        validation = validate_query_for_role(question, role, history=history)
        if not validation.allowed:
            return GroundedAnswerResult(
                answer=validation.reason or "This request is outside of what I can help with here.",
                sources=[],
                retrieved_chunks=[],
                used_subject_filter=subject,
            )

        scope = self.scope_for(role)
        return self._engine.answer_question(
            question,
            top_k=scope.top_k,
            subject=subject,
            student_class=student_class,
            history=history,
            learning_state=learning_state,
            namespace=namespace or scope.default_namespace,
        )

    async def route_stream(
        self,
        *,
        role: RouteRole,
        question: str,
        student_class: str,
        subject: str | None = None,
        history: list[ConversationTurn] | None = None,
        learning_state: LearningState | None = None,
        namespace: str | None = None,
    ) -> AsyncIterator[StreamChunk | StreamDone]:
        """Streaming counterpart of :meth:`route`.

        Defensive ``validate_query_for_role`` still fires up-front; on
        rejection we emit the refusal as a single chunk + done so the
        consumer's SSE path stays uniform with the happy case.
        """

        validation = validate_query_for_role(question, role, history=history)
        if not validation.allowed:
            refusal = (
                validation.reason
                or "This request is outside of what I can help with here."
            )
            yield StreamChunk(text=refusal)
            yield StreamDone(
                final_answer=refusal,
                was_modified_by_guard=False,
                sources=[],
                retrieved_chunks=[],
            )
            return

        scope = self.scope_for(role)
        async for event in self._engine.stream_answer_question(
            question,
            top_k=scope.top_k,
            subject=subject,
            student_class=student_class,
            history=history,
            learning_state=learning_state,
            namespace=namespace or scope.default_namespace,
        ):
            yield event


__all__ = ["RetrievalScope", "RoleAwareQueryRouter", "RouteRole"]
