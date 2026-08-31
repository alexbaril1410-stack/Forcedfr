"""ForcedFR - Pipeline audio complet (V1.2).

Film.mkv
  -> piste audio française
  -> extraction WAV
  -> détection des changements de langue (fenêtres de N secondes)
  -> segments suspects
  -> extraction d'un clip audio par segment (pour Gemini, V1.4)
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from audio.extractor import (
    get_audio_tracks_for_file,
    find_french_audio_track,
    extract_audio_track,
    extract_clip,
)
from audio.language_detector import run_detection, SuspectSegment


def analyze_audio(
    mkv_path: str,
    output_dir: str,
    window_seconds: float = 12.0,
    min_confidence: float = 0.6,
    model_size: str = "tiny",
) -> dict:
    tracks = get_audio_tracks_for_file(mkv_path)
    french_track = find_french_audio_track(tracks)

    if french_track is None:
        return {
            "path": mkv_path,
            "error": "Aucune piste audio française trouvée dans ce fichier.",
            "audio_tracks": [asdict(t) for t in tracks],
        }

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "french_audio.wav"
        extract_audio_track(mkv_path, french_track.index, str(wav_path))

        segments = run_detection(
            str(wav_path),
            expected_language="fr",
            window_seconds=window_seconds,
            min_confidence=min_confidence,
            model_size=model_size,
        )

        clip_paths = []
        for i, seg in enumerate(segments, start=1):
            clip_path = output_path / f"segment_{i:03d}_{seg.start:.1f}s-{seg.end:.1f}s.wav"
            extract_clip(mkv_path, french_track.index, seg.start, seg.end, str(clip_path))
            clip_paths.append(str(clip_path))

    return {
        "path": mkv_path,
        "french_audio_track_index": french_track.index,
        "suspect_segments": [
            {
                "start": seg.start,
                "end": seg.end,
                "languages": seg.languages,
                "max_probability": seg.max_probability,
                "clip": clip_paths[i],
            }
            for i, seg in enumerate(segments)
        ],
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python audio/pipeline.py <film.mkv> [output_dir]")
        sys.exit(1)

    mkv = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "./suspect_clips"

    result = analyze_audio(mkv, out_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if "suspect_segments" in result:
        print(f"\n{len(result['suspect_segments'])} passage(s) suspect(s) trouvé(s).")
        print(f"Clips extraits dans : {out_dir}")
