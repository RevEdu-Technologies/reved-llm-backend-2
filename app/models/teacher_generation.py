"""Backward-compatible alias for the legacy ``TeacherGeneration`` model.

The table was renamed to ``ai_generations`` and the model to ``AIGeneration``
to support student- and parent-side artefacts. Existing imports continue to
work via this alias; new code should import ``AIGeneration`` directly.
"""

from app.models.ai_generation import AIGeneration as TeacherGeneration

__all__ = ["TeacherGeneration"]
