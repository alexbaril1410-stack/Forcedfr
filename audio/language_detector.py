"""ForcedFR - Détection des passages en langue étrangère dans une piste audio.

Découpe l'audio en fenêtres fixes, fait détecter la langue de chaque fenêtre
par un petit modèle Whisper local (faster-whisper, CPU), puis regroupe les
fenêtres consécutives en "segments suspects" (langue != français).

La logique de regroupement (`classify_windows`) est une fonction pure,
testable sans installer faster-whisper : on peut lui donner des résultats
de langue simulés.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WindowResult:
    start: float
    end: float
    language: str
    probability: float


@dataclass
class SuspectSegment:
    start: float
    end: float
    languages: list  # langues détectées dans les fenêtres regroupées
    max_probability: float


def classify_windows(
    windows: list[WindowResult],
    expected_language: str = "fr",
    min_confidence: float = 0.6,
    max_gap_seconds: float = 0.5,
) -> list[SuspectSegment]:
    """Fonction pure : regroupe les fenêtres non-françaises et suffisamment
    fiables en segments continus.

    Deux fenêtres suspectes sont fusionnées si l'écart entre elles est
    inférieur à `max_gap_seconds` (pour absorber une fenêtre française
    isolée qui casserait un même passage étranger, tolérance faible ici,
    volontairement stricte pour éviter de fusionner des passages distincts).
    """
    suspects = [
        w for w in windows
        if w.language != expected_language and w.probability >= min_confidence
    ]
    suspects.sort(key=lambda w: w.start)

    segments: list[SuspectSegment] = []
    for w in suspects:
        if segments and w.start - segments[-1].end <= max_gap_seconds:
            last = segments[-1]
            last.end = max(last.end, w.end)
            if w.language not in last.languages:
                last.languages.append(w.language)
            last.max_probability = max(last.max_probability, w.probability)
        else:
            segments.append(
                SuspectSegment(
                    start=w.start,
                    end=w.end,
                    languages=[w.language],
                    max_probability=w.probability,
                )
            )
    return segments


def _iter_windows(wav_path: Path, window_seconds: float):
    """Charge le WAV et le découpe en fenêtres (numpy float32 mono).
    Import de soundfile fait ici pour ne pas exiger la dépendance
    pour les tests de `classify_windows`."""
    import soundfile as sf

    audio, sample_rate = sf.read(str(wav_path), dtype="float32", always_2d=False)
    window_len = int(window_seconds * sample_rate)
    total_samples = len(audio)

    start_sample = 0
    while start_sample < total_samples:
        end_sample = min(start_sample + window_len, total_samples)
        chunk = audio[start_sample:end_sample]
        yield (start_sample / sample_rate, end_sample / sample_rate, chunk)
        start_sample = end_sample


def run_detection(
    wav_path: str,
    expected_language: str = "fr",
    window_seconds: float = 12.0,
    min_confidence: float = 0.6,
    model_size: str = "tiny",
) -> list[SuspectSegment]:
    """Fait tourner faster-whisper sur chaque fenêtre pour détecter sa langue,
    puis regroupe le résultat via `classify_windows`.

    Nécessite `pip install faster-whisper soundfile`.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    window_results: list[WindowResult] = []
    for start, end, chunk in _iter_windows(Path(wav_path), window_seconds):
        if len(chunk) == 0:
            continue
        # language=None -> faster-whisper détecte la langue de ce segment
        segments, info = model.transcribe(
            chunk, language=None, beam_size=1, without_timestamps=True
        )
        list(segments)  # force la génération (paresseuse sinon)
        window_results.append(
            WindowResult(
                start=start,
                end=end,
                language=info.language,
                probability=info.language_probability,
            )
        )

    return classify_windows(
        window_results,
        expected_language=expected_language,
        min_confidence=min_confidence,
    )
