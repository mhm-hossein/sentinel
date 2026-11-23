"""Prompt templates and intent presets for the AI Assistant.

Each user-facing action (summarize, explain, translate, rewrite, analyze,
describe, review) maps to a concise instruction prepended to the supplied
context. The ``smart`` preset asks the model to first decide what the content
needs, implementing the spec's "SMART MODE".
"""

from __future__ import annotations

#: Mapping of intent keyword -> instruction applied to text/document context.
TEXT_INTENTS: dict[str, str] = {
    "summarize": "Summarize the following content concisely:",
    "explain": "Explain the following clearly and simply:",
    "translate": "Translate the following to English. If it is already "
    "English, translate it to the most likely intended other language:",
    "rewrite": "Rewrite the following to be clearer and well-structured, "
    "keeping the original meaning:",
    "analyze": "Provide a detailed analysis of the following:",
    "review": "Review the following code. Detect the language, explain what it "
    "does, identify issues or bugs, and suggest concrete improvements:",
}

#: Mapping of intent keyword -> instruction applied to image context.
IMAGE_INTENTS: dict[str, str] = {
    "describe": "Describe this image: the scene, key objects, any visible "
    "text, and notable details.",
    "summarize": "Give a concise summary of what this image shows.",
    "analyze": "Provide a detailed analysis of this image, including objects, "
    "text, context, and anything noteworthy.",
    "explain": "Explain what this image depicts and its likely context.",
}

#: Smart-mode instruction: let the model classify and respond appropriately.
SMART_TEXT_INSTRUCTION = (
    "Decide what the following content most needs, then do it:\n"
    "- Long text or an article -> summarize it.\n"
    "- A question -> answer it.\n"
    "- Source code -> explain what it does and flag issues.\n"
    "- An error log or stack trace -> identify the root cause and suggest "
    "fixes and recommended actions.\n"
    "- Anything else -> respond helpfully.\n"
    "Do not state which category you picked; just give the useful response.\n\n"
    "Content:"
)

#: Smart-mode instruction for images.
SMART_IMAGE_INSTRUCTION = (
    "Look at this image and respond with the most useful thing: describe it, "
    "summarize it, extract any text, or analyze it as appropriate."
)


def build_text_prompt(intent: str | None, context: str, *, question: str = "") -> str:
    """Build the final text prompt for a text/document request.

    Args:
        intent: A keyword from :data:`TEXT_INTENTS`, or ``None`` for smart mode.
        context: The replied/extracted content to act on.
        question: Optional free-form instruction typed after ``.ai`` that
            overrides preset intents (a direct request).

    Returns:
        The composed prompt string.
    """
    if question:
        if context:
            return f"{question}\n\nContent:\n{context}"
        return question
    if intent and intent in TEXT_INTENTS:
        return f"{TEXT_INTENTS[intent]}\n\n{context}"
    return f"{SMART_TEXT_INSTRUCTION}\n{context}"


def build_image_instruction(intent: str | None, question: str = "") -> str:
    """Return the instruction text accompanying an image request.

    Args:
        intent: A keyword from :data:`IMAGE_INTENTS`, or ``None`` for smart mode.
        question: Optional free-form instruction overriding presets.

    Returns:
        The instruction string to pair with the image part.
    """
    if question:
        return question
    if intent and intent in IMAGE_INTENTS:
        return IMAGE_INTENTS[intent]
    return SMART_IMAGE_INSTRUCTION
