"""Student-oriented prompt builders for grounded tutoring answers."""

from __future__ import annotations

from typing import Any, Sequence

from app.prompts.base import GROUNDING_SYSTEM_PROMPT, load_template
from app.rag.retrieval.retriever import RetrievalResult
from app.schemas.student import ConversationTurn, LearningState

STUDENT_STRUCTURED_SYSTEM_PROMPT = (
    GROUNDING_SYSTEM_PROMPT
    + "\n"
    + "You must respond with strict JSON matching the exact schema in the user prompt. "
    + "Do not wrap the JSON in markdown fences. Do not add commentary before or after the JSON. "
    + "Do not include trailing commas. Use clear, warm, age-appropriate language inside the JSON fields."
)

STUDENT_SYSTEM_PROMPT = (
    GROUNDING_SYSTEM_PROMPT
    + "\n"
    + "Explain ideas in a warm, teacher-like tone.\n"
    + "Write your answer in natural flowing paragraphs. "
    + "Begin with a clear direct answer, then deepen the explanation, "
    + "include a simple example or analogy, and optionally finish with a brief reinforcement question.\n"
    + "Do not number the response, do not use bullet points, "
    + "and do not label sections like 'Direct Answer:', 'Explanation:', 'Example:', or 'Quick Check:'.\n"
    + "Adapt vocabulary, depth, and examples to the student's class level.\n"
    + "When prior conversation is provided, use it only to understand the student's follow-up, continuity, and tone.\n"
    + "When learner progress signals are provided, adapt naturally without naming or exposing those signals.\n"
    + "Do not treat prior conversation as factual grounding.\n"
    + "Do not list sources, cite documents, or mention where the information came from.\n"
    + "Do not say phrases like 'according to the text', 'the source says', or 'the context says'."
)


def build_student_grounded_prompt(
    *,
    question: str,
    student_class: str,
    history: Sequence[ConversationTurn] | None,
    learning_state: LearningState | None,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    """Build the student-learning grounded QA prompt."""

    context = _build_hidden_context(retrieval_results)
    template = load_template("student_qa.txt")
    return template.format(
        conversation_history=_format_conversation_history(history),
        learning_guidance=_build_learning_guidance(learning_state),
        context=context,
        question=question.strip(),
        student_class=student_class.strip(),
        class_guidance=_build_class_guidance(student_class),
    )


def _build_hidden_context(retrieval_results: Sequence[RetrievalResult]) -> str:
    blocks: list[str] = []
    for result in retrieval_results:
        blocks.append(result.text.strip())
    return "\n\n".join(blocks)


def _format_conversation_history(
    history: Sequence[ConversationTurn] | Sequence[dict[str, Any]] | None,
) -> str:
    if not history:
        return "No previous conversation."

    rendered_turns: list[str] = []
    for turn in history:
        if isinstance(turn, dict):
            role = str(turn.get("role", "")).strip()
            content = str(turn.get("content", "")).strip()
        else:
            role = turn.role
            content = turn.content.strip()
        speaker = "Student" if role == "user" else "Tutor"
        rendered_turns.append(f"{speaker}: {content}")
    return "\n".join(rendered_turns)


def _build_class_guidance(student_class: str) -> str:
    normalized = student_class.strip().lower()

    if normalized.startswith("primary"):
        return (
            "Use very simple words, short sentences, and familiar everyday examples. "
            "Avoid heavy scientific terms unless you explain them immediately."
        )
    if normalized.startswith("jss"):
        return (
            "Use simple but correct school-level language. Introduce important scientific terms "
            "gently and explain them in plain words."
        )
    if normalized.startswith("ss"):
        return (
            "Use more precise scientific explanations, but still teach clearly. "
            "You may include key scientific vocabulary and simple process details."
        )
    return (
        "Match the student's level carefully: keep the explanation clear, educational, and not overly technical."
    )


def _build_learning_guidance(learning_state: LearningState | None) -> str:
    if learning_state is None:
        return (
            "Use the normal teaching flow: explain clearly, reinforce with one example or analogy, "
            "and optionally check understanding with a light question."
        )

    understanding = learning_state.understanding_level
    previous_correct = learning_state.previous_attempt_correct
    attempt_count = learning_state.attempt_count or 0

    if understanding == "low" or previous_correct is False:
        guidance = (
            "The student seems to be struggling. Shift into simplify mode: break the idea into smaller steps, "
            "use clearer everyday analogies, reduce technical terms, and re-explain patiently before checking understanding."
        )
        if attempt_count >= 2:
            guidance += " Slow down further and rebuild the idea from the most basic concept."
        return guidance

    if understanding == "high":
        guidance = (
            "The student appears ready for deeper teaching. Stay grounded, but give a more technically accurate explanation, "
            "introduce one advanced idea when helpful, reinforce with a sharper example, and end with a more challenging check question."
        )
        if previous_correct is True:
            guidance += " Since the previous attempt was correct, you may progress the explanation naturally."
        return guidance

    guidance = (
        "Use a balanced teaching mode: explain clearly, give one solid example, add light reinforcement, "
        "and finish with a simple check for understanding."
    )
    if previous_correct is True:
        guidance += " The student seems to be making progress, so you may advance the explanation slightly."
    return guidance


def build_learning_path_prompt(
    *,
    student_class: str,
    subject: str,
    topic: str,
    understanding: str | None,
    weekly_hours: int | None,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    """Build the learning-pathway JSON-output prompt."""

    template = load_template("learning_path.txt")
    return template.format(
        student_class=student_class.strip(),
        subject=subject.strip(),
        topic=topic.strip(),
        understanding=understanding or "not provided",
        weekly_hours=weekly_hours if weekly_hours is not None else "not provided",
        class_guidance=_build_class_guidance(student_class),
        context=_build_hidden_context(retrieval_results),
    )


def build_study_group_prompt(
    *,
    student_class: str,
    subject: str,
    topic: str,
    focus_question: str,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    """Build the study-group facilitator JSON-output prompt."""

    template = load_template("study_group.txt")
    return template.format(
        student_class=student_class.strip(),
        subject=subject.strip(),
        topic=topic.strip(),
        focus_question=focus_question.strip(),
        context=_build_hidden_context(retrieval_results),
    )


def build_career_guidance_prompt(
    *,
    student_class: str,
    favorite_subjects: Sequence[str],
    strengths: Sequence[str],
    interests: Sequence[str],
    long_term_dream: str | None,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    """Build the career-guidance JSON-output prompt."""

    template = load_template("career_guidance.txt")
    return template.format(
        student_class=student_class.strip(),
        favorite_subjects=", ".join(favorite_subjects) or "not provided",
        strengths=", ".join(strengths) or "not provided",
        interests=", ".join(interests) or "not provided",
        long_term_dream=long_term_dream or "not provided",
        class_guidance=_build_class_guidance(student_class),
        context=_build_hidden_context(retrieval_results),
    )


PREFLIGHT_SYSTEM_PROMPT = (
    "You are a silent preprocessor. You only clean up and judge the input. "
    "You never answer the question. You always return strict JSON with the "
    "exact keys requested, no markdown fences, no prose outside the JSON."
)


def build_preflight_prompt(
    *,
    question: str,
    subject_hint: str | None,
    student_class: str,
    history: Sequence[ConversationTurn] | Sequence[dict[str, Any]] | None = None,
) -> str:
    """Build the preflight correction + clarity-check prompt."""

    template = load_template("preflight.txt")
    return template.format(
        question=question.strip(),
        subject_hint=(subject_hint or "").strip() or "not provided",
        student_class=student_class.strip(),
        conversation_history=_format_conversation_history(history),
    )


def build_goal_coaching_prompt(
    *,
    title: str,
    description: str | None,
    subject: str | None,
    progress_percent: int,
    recent_note: str | None,
) -> str:
    """Build a short non-grounded prompt for goal coaching notes.

    Goal coaching is intentionally not grounded in the textbook corpus — it's
    motivational and personal rather than factual.
    """

    return (
        "You are a warm student coach. Write a 2-3 sentence motivational note "
        "for a learner working on the goal below. Acknowledge their progress, "
        "name one specific next step, and keep the tone encouraging and age-appropriate. "
        "Do not use markdown, lists, or headings.\n\n"
        f"Goal title: {title}\n"
        f"Description: {description or 'not provided'}\n"
        f"Subject: {subject or 'not provided'}\n"
        f"Progress so far: {progress_percent}%\n"
        f"Latest note from the student: {recent_note or 'none'}\n"
    )
