"""The verified engine language values for the native speech lanes.

Only these primary tags have a measured vendor value. An unknown tag is refused
instead of being forwarded verbatim: the verified engine expects its own names
and silently mis-handles everything else.

``auto`` is the one exception that is not a vendor name: it is the engine's own
detection sentinel, measured to answer 200, and is forwarded unchanged.
"""

from __future__ import annotations

from typing import Final

ENGINE_LANGUAGES: Final[dict[str, str]] = {
    "zh": "Chinese",
    "en": "English",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "es": "Spanish",
}

# A sentinel accepted by the engine, not a language the vendor names. It is kept
# out of ``ENGINE_LANGUAGES`` so a binding's ``supported_languages`` stays a list
# of speakable languages instead of mixing in a detection request.
AUTO_LANGUAGE: Final = "auto"


def engine_language(tag: str) -> str:
    """Translate a public BCP47 tag to the engine's fixed value.

    ``auto`` is forwarded verbatim; every other tag must have a verified primary
    mapping. Raises ``ValueError`` otherwise; callers validate configured and
    requested languages before this point.
    """
    if tag.lower() == AUTO_LANGUAGE:
        return AUTO_LANGUAGE
    primary = tag.split("-", 1)[0].lower()
    try:
        return ENGINE_LANGUAGES[primary]
    except KeyError:
        raise ValueError("the language has no verified engine value") from None
