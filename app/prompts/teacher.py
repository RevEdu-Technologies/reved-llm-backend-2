"""Prompt assembly for teacher endpoints.

Teachers get a different persona than students: the AI is a teaching
assistant that *produces* artefacts (lesson notes, quizzes, marking guides,
feedback) instead of *explaining* concepts. The system prompt below makes
that role explicit, plus the safety contract is opposite — teachers may
see both ``student_ok`` and ``teacher_only`` chunks.

Every prompt returns strict JSON so the route can parse it into the
appropriate Pydantic response model without further model calls.
"""

from __future__ import annotations

import json
from typing import Sequence

from app.rag.retrieval.retriever import RetrievalResult


TEACHER_SYSTEM_PROMPT = (
    "You are RevEd Teaching Assistant — an AI that helps secondary-school "
    "teachers prepare lessons, build assessments, write marking guides, and "
    "give feedback on student work. You are not a tutor; you are a teacher's "
    "collaborator.\n\n"
    "Hard rules:\n"
    "- Ground every artefact in the provided CONTEXTS. If the contexts are "
    "  insufficient, say so directly and ask the teacher for what would help.\n"
    "- Do NOT invent definitions, formulas, dates, or named entities that "
    "  aren't supported by the contexts.\n"
    "- Marking guides, rubrics, and assessment answer keys ARE allowed (the "
    "  teacher needs them).\n"
    "- Write at the cognitive level appropriate for the student_class given.\n"
    "- Use correct subject terminology and units; show worked steps for "
    "  numeric work.\n"
    "- When asked for JSON, return ONLY valid JSON — no markdown fences, no "
    "  prose outside the JSON object.\n"
)


def _format_contexts(results: Sequence[RetrievalResult]) -> str:
    """Render retrieval hits as a numbered context block for the LLM."""
    if not results:
        return "(No retrieved contexts available.)"
    blocks: list[str] = []
    for i, hit in enumerate(results, start=1):
        header = f"[{i}] source={hit.source_file}"
        if hit.chapter or hit.section:
            header += f" chapter={hit.chapter or '-'} section={hit.section or '-'}"
        if hit.chunk_type:
            header += f" type={hit.chunk_type}"
        blocks.append(f"{header}\n{hit.text.strip()}")
    return "\n\n---\n\n".join(blocks)


# --- Lesson notes ---------------------------------------------------------


def build_lesson_notes_prompt(
    *,
    subject: str,
    student_class: str,
    topic: str,
    duration_minutes: int | None,
    learning_objectives: list[str] | None,
    include_examples: bool,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    contexts = _format_contexts(retrieval_results)
    objectives_block = (
        "Teacher-supplied objectives (use these verbatim if reasonable, refine "
        "if not):\n- " + "\n- ".join(learning_objectives)
        if learning_objectives
        else "No objectives supplied. Infer 3-5 from the topic and contexts."
    )
    duration_block = (
        f"Target class duration: {duration_minutes} minutes."
        if duration_minutes
        else "No duration specified — keep notes scoped to a single class period."
    )
    return (
        f"## Task\n"
        f"Draft lesson notes a {student_class} teacher can use to teach "
        f"'{topic}' in {subject}.\n\n"
        f"{duration_block}\n\n"
        f"{objectives_block}\n\n"
        f"Examples included: {'yes' if include_examples else 'no'}.\n\n"
        f"## Contexts\n{contexts}\n\n"
        f"## Output\n"
        "Return STRICT JSON with this shape (no markdown, no prose outside JSON):\n"
        "{\n"
        '  "learning_objectives": ["..."],\n'
        '  "overview": "2-3 sentence intro for the teacher.",\n'
        '  "sections": [\n'
        '    {"heading": "...", "body": "...", "examples": ["..."]}\n'
        "  ],\n"
        '  "teacher_tips": ["delivery tip 1", "..."],\n'
        '  "misconceptions_to_address": ["common student error 1", "..."]\n'
        "}\n"
        "- 3-6 sections, ordered as you'd teach them.\n"
        "- Each section.body is 2-5 sentences; do not write essay-length blocks.\n"
        "- examples may be [] when include_examples=no.\n"
        "- 2-4 teacher_tips and 1-3 misconceptions_to_address.\n"
    )


# --- Quiz generation ------------------------------------------------------


def build_quiz_prompt(
    *,
    subject: str,
    student_class: str,
    topic: str,
    num_questions: int,
    difficulty_mix: dict[str, int] | None,
    question_types: list[str] | None,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    contexts = _format_contexts(retrieval_results)
    mix_block = (
        f"Difficulty mix requested: {json.dumps(difficulty_mix)}. "
        "Rebalance if the sum doesn't equal num_questions."
        if difficulty_mix
        else "Default mix: roughly 40% easy / 40% medium / 20% hard."
    )
    type_block = (
        f"Restrict question_type to: {', '.join(question_types)}."
        if question_types
        else "Mix question_types as appropriate: mcq, short_answer, numeric, derivation."
    )
    return (
        f"## Task\n"
        f"Build a {num_questions}-question quiz on '{topic}' for a {student_class} "
        f"{subject} class. INCLUDE a marking guide for every question.\n\n"
        f"{mix_block}\n\n"
        f"{type_block}\n\n"
        f"## Contexts\n{contexts}\n\n"
        f"## Output\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "questions": [\n'
        "    {\n"
        '      "question_number": 1,\n'
        '      "question": "...",\n'
        '      "question_type": "mcq | short_answer | numeric | derivation",\n'
        '      "difficulty": "easy | medium | hard",\n'
        '      "options": ["A...","B...","C...","D..."] | null,\n'
        '      "marking_guide": "expected answer + grading notes",\n'
        '      "points": 1\n'
        "    }\n"
        "  ],\n"
        '  "total_points": <int>,\n'
        '  "suggested_duration_minutes": <int>\n'
        "}\n"
        "- options is non-null only for question_type='mcq' (always 4 options, exactly one correct).\n"
        "- marking_guide must state the expected answer + 1-2 sentences of grading guidance.\n"
        "- suggested_duration_minutes ~= num_questions * 2 unless complexity warrants more.\n"
    )


# --- Student feedback -----------------------------------------------------


def build_feedback_prompt(
    *,
    subject: str,
    student_class: str,
    question: str,
    student_answer: str,
    rubric: str | None,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    contexts = _format_contexts(retrieval_results)
    rubric_block = (
        f"Teacher-supplied rubric (use as primary grading guide):\n{rubric}"
        if rubric
        else "No rubric supplied — judge against the subject contexts below."
    )
    return (
        f"## Task\n"
        f"Give the teacher actionable feedback on a {student_class} {subject} "
        "student's submission. Don't just grade — call out what's strong, what's "
        "wrong, what to correct, and what the student should practice next.\n\n"
        f"{rubric_block}\n\n"
        f"## Question shown to the student\n{question}\n\n"
        f"## Student's answer\n{student_answer}\n\n"
        f"## Contexts (correct subject material)\n{contexts}\n\n"
        f"## Output\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "overall_score_band": "excellent | good | fair | needs_improvement",\n'
        '  "summary": "1-2 sentence overall verdict for the teacher",\n'
        '  "strengths": ["specific thing the student did well", "..."],\n'
        '  "areas_for_improvement": ["..."],\n'
        '  "specific_corrections": ["where the student went wrong, with the correct version", "..."],\n'
        '  "next_steps": ["concrete practice / revision the student should do", "..."]\n'
        "}\n"
        "- Strengths and improvements must reference the student's actual answer, not generalities.\n"
        "- specific_corrections should quote the student then give the correction.\n"
    )


# --- Frontend-compatible markdown content ---------------------------------
#
# Unlike the structured-JSON builders above, this path produces a single
# markdown document streamed token-by-token to match the frontend's
# existing content generator. It still grounds on retrieved contexts.

TEACHER_CONTENT_SYSTEM_PROMPT = (
    "You are RevEd AI, an expert curriculum developer for Nigerian "
    "secondary education (WAEC / NECO / NERDC aligned). You help teachers "
    "produce classroom-ready materials.\n\n"
    "Hard rules:\n"
    "- Ground the material in the provided CONTEXTS. Prefer facts, "
    "  definitions, and examples supported by them.\n"
    "- Do NOT invent formulas, dates, or named entities unsupported by the "
    "  contexts or well-established curriculum knowledge.\n"
    "- Write at the cognitive level of the given class.\n"
    "- Use examples and contexts relevant to Nigerian students.\n"
    "- Output GitHub-flavored Markdown only — headings, lists, tables, and "
    "  fenced code where useful. No JSON, no preamble like 'Here is...'.\n"
)


_CONTENT_TYPE_INSTRUCTIONS: dict[str, str] = {
    "lesson_plan": (
        "Produce a comprehensive lesson plan with: title & overview; "
        "measurable learning objectives; required materials; duration & "
        "timeline; introduction/hook (5-10 min); step-by-step main "
        "instruction; practice activities; assessment strategy; "
        "differentiation for diverse learners; closure/reflection; and "
        "homework/extension. Use clear markdown headings and bullet points."
    ),
    "quiz": (
        "Produce a quiz with: title & instructions; a mix of question types "
        "(multiple choice, true/false, short answer, essay); clear "
        "unambiguous questions; an answer key with explanations; point "
        "values; and an estimated completion time. Number the questions."
    ),
    "notes": (
        "Produce comprehensive lesson notes with: topic overview & key "
        "concepts; detailed explanations with worked examples; important "
        "definitions and vocabulary; key formulas/principles where "
        "applicable; descriptions of helpful diagrams; summary takeaways; "
        "and a few review questions."
    ),
    "slides": (
        "Produce a slide-deck outline. Use '## Slide X: <title>' for each "
        "slide, with 3-5 key bullet points, speaker notes, and suggested "
        "visuals per slide. Include an opening hook slide and a closing "
        "call-to-action slide, with logical flow between them."
    ),
    "study_guide": (
        "Produce a study guide with: topic overview & objectives; key "
        "concepts and definitions; important facts; practice problems WITH "
        "solutions; common misconceptions to avoid; memory aids/mnemonics; "
        "a review checklist; and WAEC/NECO-style sample questions where "
        "applicable."
    ),
}

_TONE_INSTRUCTIONS: dict[str, str] = {
    "professional": "Use formal, academic language suitable for professional development.",
    "engaging": "Use engaging, student-friendly language that sparks curiosity.",
    "simplified": "Use simple, clear language for students who need extra support.",
}


def build_content_markdown_prompt(
    *,
    content_type: str,
    subject: str,
    student_class: str,
    topic: str,
    learning_objectives: str | None,
    difficulty_level: str,
    curriculum_standard: str | None,
    tone: str,
    retrieval_results: Sequence[RetrievalResult],
) -> str:
    """Assemble the user prompt for a streamed markdown artefact."""

    contexts = _format_contexts(retrieval_results)
    type_instruction = _CONTENT_TYPE_INSTRUCTIONS.get(
        content_type, _CONTENT_TYPE_INSTRUCTIONS["lesson_plan"]
    )
    tone_instruction = _TONE_INSTRUCTIONS.get(tone, _TONE_INSTRUCTIONS["engaging"])
    objectives_block = (
        f"Learning objectives / additional instructions: {learning_objectives}"
        if learning_objectives
        else "No explicit objectives supplied — infer suitable ones from the topic."
    )
    standard_block = (
        f"Align to curriculum standard: {curriculum_standard}."
        if curriculum_standard
        else "Align to the standard Nigerian secondary curriculum."
    )
    readable_type = content_type.replace("_", " ")

    return (
        f"## Task\n"
        f"Create a {readable_type} for a {student_class} {subject} class on "
        f"the topic '{topic}'.\n\n"
        f"Difficulty level: {difficulty_level}.\n"
        f"{objectives_block}\n"
        f"{standard_block}\n"
        f"Tone: {tone_instruction}\n\n"
        f"## Format\n{type_instruction}\n\n"
        f"## Contexts\n{contexts}\n\n"
        f"## Output\n"
        "Return the complete artefact as GitHub-flavored Markdown only."
    )


__all__ = [
    "TEACHER_SYSTEM_PROMPT",
    "TEACHER_CONTENT_SYSTEM_PROMPT",
    "build_content_markdown_prompt",
    "build_feedback_prompt",
    "build_lesson_notes_prompt",
    "build_quiz_prompt",
]
