"""The verified vendor language values for the native speech lanes.

Only these primary tags have a measured vendor value. An unknown tag is refused
instead of being forwarded verbatim: the verified engine expects its own names
and silently mis-handles everything else.
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


def engine_language(tag: str) -> str:
    """Translate a public BCP47 tag to the vendor's fixed value.

    Raises ``ValueError`` when the primary tag has no verified mapping; callers
    validate configured and requested languages before this point.
    """
    primary = tag.split("-", 1)[0].lower()
    try:
        return ENGINE_LANGUAGES[primary]
    except KeyError:
        raise ValueError("the language has no verified engine value") from None
