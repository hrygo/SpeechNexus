"""Relationships to declared binding domains, beyond individual source field bounds.

No values are populated or clamped. Resource existence, publication and physical
engine capability remain separate acceptance. Inputs are source-validated JSON.
"""

from typing import Any


def validate_declared_defaults(definition: dict[str, Any], parameters: dict[str, Any]) -> None:
    domains = definition["parameter_domains"]
    tasks = definition["tasks"]
    for section in ("asr", "tts"):
        values = parameters.get(section, {})
        if not values:
            continue
        if section not in domains or not any(task.startswith(section + "_") for task in tasks):
            raise ValueError("Default section has no declared task/domain")
        domain = domains[section]
        fields = (
            {
                "language": "languages",
                "finalization": "finalization_modes",
                "alignment": "alignment_modes",
                "diarization": "diarization_modes",
            }
            if section == "asr"
            else {
                "language": "languages",
                "text_commit_mode": "text_commit_modes",
                "audio_delivery": "audio_delivery_modes",
            }
        )
        for field, allowed in fields.items():
            if field in values and values[field] not in domain.get(allowed, []):
                raise ValueError("Default is outside the declared parent domain")
        if section == "asr":
            if "max_segment_ms" in values and values["max_segment_ms"] > domain["max_segment_ms"]:
                raise ValueError("Default segment exceeds the declared bound")
            if (
                "max_segment_ms" in values
                and "preview_interval_ms" in values
                and values["max_segment_ms"] < values["preview_interval_ms"]
            ):
                raise ValueError("Default segment is shorter than its preview interval")
            if values.get("rollback_tokens", 0) > 0 and not domain.get(
                "supports_rollback_tokens", False
            ):
                raise ValueError("Rollback tokens are not declared supported")
        else:
            for field, flag in (
                ("instructions", "supports_instructions"),
                ("seed", "supports_seed"),
            ):
                if values.get(field) is not None and not domain.get(flag, False):
                    raise ValueError("Default feature is not declared supported")
            if "speed" in values:
                if not domain.get("supports_speed", False):
                    raise ValueError("Default speed is not declared supported")
                bounds = domain.get("speed_range")
                if (
                    bounds is not None
                    and not bounds["minimum"] <= values["speed"] <= bounds["maximum"]
                ):
                    raise ValueError("Default speed is outside its declared range")
            if (
                "planner" in values
                and values["planner"]["max_chars"] > domain["max_input_codepoints"]
            ):
                raise ValueError("Default planner exceeds the declared input bound")
    output = parameters.get("output", {})
    if "transcript_format" in output and "asr" not in domains:
        raise ValueError("Transcript defaults require an ASR domain")
    if (
        "audio_format" in output
        and "tts" in domains
        and output["audio_format"] not in domains["tts"]["audio_formats"]
    ):
        raise ValueError("Default audio format is outside its declared range")
