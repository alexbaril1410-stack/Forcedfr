"""Tests de la logique de détection - pas besoin d'un vrai MKV, on simule
directement la sortie JSON de ffprobe."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import (
    extract_subtitle_tracks,
    level_a_metadata,
    level_b_track_name,
)


def make_probe(streams):
    return {"streams": streams, "format": {}}


def subtitle_stream(index, language=None, title=None, forced=0):
    return {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": "subrip",
        "tags": {k: v for k, v in {"language": language, "title": title}.items() if v is not None},
        "disposition": {"forced": forced},
    }


def test_level_a_detects_correctly_tagged_forced_track():
    probe = make_probe([
        subtitle_stream(2, language="eng"),
        subtitle_stream(3, language="fra"),
        subtitle_stream(4, language="fra", forced=1),
    ])
    tracks = extract_subtitle_tracks(probe)
    match = level_a_metadata(tracks)
    assert match is not None
    assert match.index == 4


def test_level_a_returns_none_when_no_forced_flag():
    probe = make_probe([
        subtitle_stream(2, language="fra"),
        subtitle_stream(3, language="eng", forced=1),  # forced mais pas français
    ])
    tracks = extract_subtitle_tracks(probe)
    assert level_a_metadata(tracks) is None


def test_level_b_detects_forced_in_title():
    probe = make_probe([
        subtitle_stream(2, language="fra", title="French"),
        subtitle_stream(3, language="fra", title="French Forced"),
    ])
    tracks = extract_subtitle_tracks(probe)
    match = level_b_track_name(tracks)
    assert match is not None
    assert match.index == 3


def test_level_b_excludes_sdh_even_with_forced_keyword():
    probe = make_probe([
        subtitle_stream(2, language="fra", title="French Forced + SDH"),
    ])
    tracks = extract_subtitle_tracks(probe)
    # Ambigu -> on n'accepte pas au niveau B, il faudra le niveau C plus tard
    assert level_b_track_name(tracks) is None


def test_level_b_ignores_non_french_tracks():
    probe = make_probe([
        subtitle_stream(2, language="eng", title="Forced"),
    ])
    tracks = extract_subtitle_tracks(probe)
    assert level_b_track_name(tracks) is None


if __name__ == "__main__":
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-v"],
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    sys.exit(result.returncode)
