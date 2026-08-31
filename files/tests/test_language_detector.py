"""Tests de classify_windows - logique pure, pas besoin de faster-whisper."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio.language_detector import WindowResult, classify_windows


def test_merges_consecutive_foreign_windows():
    windows = [
        WindowResult(0, 12, "fr", 0.95),
        WindowResult(12, 24, "de", 0.9),
        WindowResult(24, 36, "de", 0.85),
        WindowResult(36, 48, "fr", 0.92),
    ]
    segments = classify_windows(windows)
    assert len(segments) == 1
    assert segments[0].start == 12
    assert segments[0].end == 36
    assert segments[0].languages == ["de"]


def test_ignores_low_confidence_detections():
    windows = [
        WindowResult(0, 12, "fr", 0.95),
        WindowResult(12, 24, "de", 0.3),  # confiance trop faible -> ignoré
        WindowResult(24, 36, "fr", 0.9),
    ]
    segments = classify_windows(windows, min_confidence=0.6)
    assert segments == []


def test_keeps_separate_non_adjacent_segments():
    windows = [
        WindowResult(0, 12, "de", 0.9),
        WindowResult(12, 24, "fr", 0.95),
        WindowResult(24, 36, "fr", 0.95),
        WindowResult(36, 48, "en", 0.88),
    ]
    segments = classify_windows(windows)
    assert len(segments) == 2
    assert segments[0].languages == ["de"]
    assert segments[1].languages == ["en"]


def test_no_suspect_when_all_french():
    windows = [
        WindowResult(0, 12, "fr", 0.95),
        WindowResult(12, 24, "fr", 0.9),
    ]
    assert classify_windows(windows) == []


if __name__ == "__main__":
    test_merges_consecutive_foreign_windows()
    test_ignores_low_confidence_detections()
    test_keeps_separate_non_adjacent_segments()
    test_no_suspect_when_all_french()
    print("Tous les tests passent ✓")
