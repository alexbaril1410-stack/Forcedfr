"""ForcedFR - Extraction des pistes audio d'un MKV.

Réutilise le même principe que analyzer.py (ffprobe) mais pour les flux audio.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from analyzer import probe_mkv, is_french


@dataclass
class AudioTrack:
    index: int
    language: Optional[str]
    title: Optional[str]
    channels: Optional[int]
    codec: Optional[str]


def extract_audio_tracks(probe_data: dict) -> list[AudioTrack]:
    tracks: list[AudioTrack] = []
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags", {}) or {}
        tracks.append(
            AudioTrack(
                index=stream.get("index"),
                language=tags.get("language"),
                title=tags.get("title"),
                channels=stream.get("channels"),
                codec=stream.get("codec_name"),
            )
        )
    return tracks


def find_french_audio_track(tracks: list[AudioTrack]) -> Optional[AudioTrack]:
    """Retourne la première piste audio française trouvée, ou None."""
    for track in tracks:
        if is_french(track.language):
            return track
    return None


def get_audio_tracks_for_file(mkv_path: str) -> list[AudioTrack]:
    probe_data = probe_mkv(Path(mkv_path))
    return extract_audio_tracks(probe_data)


def extract_audio_track(
    mkv_path: str,
    track_index: int,
    output_wav: str,
    sample_rate: int = 16000,
) -> Path:
    """Extrait une piste audio en WAV mono, prêt pour Whisper.

    `track_index` est l'index absolu du flux dans le conteneur (celui
    renvoyé par ffprobe / AudioTrack.index), pas un index relatif aux
    seules pistes audio.
    """
    output_path = Path(output_wav)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(mkv_path),
        "-map", f"0:{track_index}",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-vn",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def extract_clip(
    mkv_path: str,
    track_index: int,
    start_seconds: float,
    end_seconds: float,
    output_wav: str,
    sample_rate: int = 16000,
    padding_seconds: float = 0.5,
) -> Path:
    """Extrait un court passage audio (pour envoi ultérieur à Gemini)."""
    output_path = Path(output_wav)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    padded_start = max(0.0, start_seconds - padding_seconds)
    duration = (end_seconds - start_seconds) + 2 * padding_seconds

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(padded_start),
        "-i", str(mkv_path),
        "-map", f"0:{track_index}",
        "-t", str(duration),
        "-ac", "1",
        "-ar", str(sample_rate),
        "-vn",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path
