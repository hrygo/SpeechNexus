"""Scripted probabilities, never an energy heuristic presented as a speech model."""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class ScriptedVadDetector:
    scores: deque[float]
    fallback: float
    frames: list[bytes] = field(default_factory=list, repr=False)
    resets: int = 0

    def probability(self, pcm16: bytes) -> float:
        self.frames.append(pcm16)
        return self.scores.popleft() if self.scores else self.fallback

    def reset(self) -> None:
        self.resets += 1


class ScriptedVadModel:
    model_sha256 = "0" * 64

    def __init__(self, scores: Sequence[float] = (), *, fallback: float = 0.0) -> None:
        self.scores = scores
        self.fallback = fallback
        self.detectors: list[ScriptedVadDetector] = []

    def new_detector(self) -> ScriptedVadDetector:
        detector = ScriptedVadDetector(deque(self.scores), self.fallback)
        self.detectors.append(detector)
        return detector
