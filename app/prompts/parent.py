"""Prompt assembly for parent endpoints.

The parent voice is deliberately different from both the student tutor
(which teaches the learner) and the teacher copilot (which generates
artefacts). Parents typically don't have subject expertise; they want a
plain-language explanation they can use to help their child at home.
"""

from __future__ import annotations

from typing import Sequence

from app.rag.retrieval.retriever import RetrievalResult


PARENT_SYSTEM_PROMPT = (
    "You are RevEd Home Companion — an AI that helps parents support "
    "their children's learning at home. Your audience is a busy parent "
    "who may not have studied this subject recently. Translate textbook "
    "concepts into plain everyday language they can use at the kitchen "
    "table.\n\n"
    "Hard rules:\n"
    "- Ground every explanation in the provided CONTEXTS. If the contexts "
    "  are insufficient, say so directly.\n"
    "- Avoid jargon. When a technical term is unavoidable, define it in "
    "  the same sentence using familiar words.\n"
    "- Skip long theoretical passages. Parents want enough understanding "
    "  to help their child, not a lecture.\n"
    "- Match the explanation depth to the child's class — a Primary 5 "
    "  parent doesn't need vector calculus.\n"
    "- When asked for JSON, return ONLY valid JSON — no markdown fences, "
    "  no prose outside the JSON object.\n"
)


def _format_contexts(results: Sequence[RetrievalResult]) -> str:
    if not results:
        return "(No retrieved contexts available.)"
    blocks: list[str] = []
    for i, hit in enumerate(results, start=1):
        header = f"[{i}] source={hit.source_file}"
        blocks.append(f"{header}\n{hit.text.strip()}")
    return "\n\n---\n\n".join(blocks)


def build_explain_topic_prompt(
    *,
    subject: str,
    student_class: str,
    topic: str,
    child_question: str | None,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    contexts = _format_contexts(retrieval_results)
    question_block = (
        f"The child specifically asked: \"{child_question}\". Address this "
        "directly in the explanation."
        if child_question
        else "No specific child question was provided — give a general overview "
        "tuned to the child's class level."
    )
    return (
        f"## Task\n"
        f"A parent of a {student_class} child wants to help with a {subject} "
        f"topic at home: '{topic}'.\n\n"
        f"{question_block}\n\n"
        f"## Contexts\n{contexts}\n\n"
        f"## Output\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "explanation": "2-4 plain-language paragraphs the parent can read aloud or paraphrase",\n'
        '  "everyday_analogy": "1-2 sentence analogy connecting the idea to something familiar at home",\n'
        '  "things_to_try_at_home": ["concrete activity 1", "concrete activity 2", "..."]\n'
        "}\n"
        "- Avoid technical jargon; if used, define it inline.\n"
        "- 2-4 home activities. Keep them simple and using common household items.\n"
    )


__all__ = ["PARENT_SYSTEM_PROMPT", "build_explain_topic_prompt"]
