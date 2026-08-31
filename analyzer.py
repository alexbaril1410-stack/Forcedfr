"""ForcedFR - V1.0 : détection des pistes de sous-titres français forcés dans un MKV.

Aucun appel IA ici. On s'appuie uniquement sur ffprobe pour lire les métadonnées
des pistes de sous-titres :
  - Niveau A : language=fra + disposition.forced=1
  - Niveau B : titre de piste évoquant un "forced" français (sans être un SDH)
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

FORCED_KEYWORDS = [
    "forced",
    "forcé",
    "forcee",
    "forcée",
    "vf forced",
    "fr forced",
    "français forced",
]

# Présence d'un de ces mots => on écarte, même si "forced" apparaît aussi
# (ex: "French Forced + SDH" reste ambigu, donc on l'exclut du niveau B)
EXCLUDING_KEYWORDS = [
    "sdh",
    "hearing impaired",
    "commentary",
    "full",
    "complet",
    "complète",
    "complete",
]

FRENCH_LANGUAGE_CODES = {"fra", "fre", "fr", "fr-fr"}


@dataclass
class SubtitleTrack:
    index: int
    language: Optional[str]
    title: Optional[str]
    forced_flag: bool
    codec: Optional[str]

    @property
    def title_lower(self) -> str:
        return (self.title or "").lower()


@dataclass
class DetectionResult:
    path: str
    forced_found: bool
    method: Optional[str] = None  # "metadata" | "track_name" | None
    matched_track_index: Optional[int] = None
    all_subtitle_tracks: list = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "forced_found": self.forced_found,
            "method": self.method,
            "matched_track_index": self.matched_track_index,
            "reason": self.reason,
            "subtitle_tracks": [
                {
                    "index": t.index,
                    "language": t.language,
                    "title": t.title,
                    "forced_flag": t.forced_flag,
                    "codec": t.codec,
                }
                for t in self.all_subtitle_tracks
            ],
        }


def probe_mkv(path: Path) -> dict:
    """Appelle ffprobe et retourne le JSON complet des flux du fichier."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def extract_subtitle_tracks(probe_data: dict) -> list[SubtitleTrack]:
    tracks: list[SubtitleTrack] = []
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags", {}) or {}
        disposition = stream.get("disposition", {}) or {}
        tracks.append(
            SubtitleTrack(
                index=stream.get("index"),
                language=tags.get("language"),
                title=tags.get("title"),
                forced_flag=bool(disposition.get("forced", 0)),
                codec=stream.get("codec_name"),
            )
        )
    return tracks


def is_french(language: Optional[str]) -> bool:
    if not language:
        return False
    return language.lower() in FRENCH_LANGUAGE_CODES


def level_a_metadata(tracks: list[SubtitleTrack]) -> Optional[SubtitleTrack]:
    """Niveau A : language=fra + forced=1 dans les métadonnées du conteneur."""
    for track in tracks:
        if is_french(track.language) and track.forced_flag:
            return track
    return None


def level_b_track_name(tracks: list[SubtitleTrack]) -> Optional[SubtitleTrack]:
    """Niveau B : le titre de la piste évoque un forced FR, sans mot-clé excluant."""
    for track in tracks:
        if not is_french(track.language):
            continue
        title = track.title_lower
        if not title:
            continue
        has_forced_keyword = any(k in title for k in FORCED_KEYWORDS)
        has_excluding_keyword = any(k in title for k in EXCLUDING_KEYWORDS)
        if has_forced_keyword and not has_excluding_keyword:
            return track
    return None


def analyze_file(path: str) -> DetectionResult:
    file_path = Path(path)
    if not file_path.exists():
        return DetectionResult(path=path, forced_found=False, reason="Fichier introuvable")

    try:
        probe_data = probe_mkv(file_path)
    except subprocess.CalledProcessError as exc:
        return DetectionResult(
            path=path, forced_found=False,
            reason=f"Erreur ffprobe : {exc.stderr.strip() if exc.stderr else exc}",
        )

    tracks = extract_subtitle_tracks(probe_data)

    track = level_a_metadata(tracks)
    if track:
        return DetectionResult(
            path=path, forced_found=True, method="metadata",
            matched_track_index=track.index, all_subtitle_tracks=tracks,
            reason=f"Piste #{track.index} taguée language=fra + forced=1",
        )

    track = level_b_track_name(tracks)
    if track:
        return DetectionResult(
            path=path, forced_found=True, method="track_name",
            matched_track_index=track.index, all_subtitle_tracks=tracks,
            reason=f"Piste #{track.index} nommée '{track.title}' évoque un forced FR",
        )

    return DetectionResult(
        path=path, forced_found=False, all_subtitle_tracks=tracks,
        reason="Aucune piste forced FR détectée (niveaux A/B). "
               "Passage à l'analyse audio/OCR/Gemini nécessaire (V1.2+).",
    )
