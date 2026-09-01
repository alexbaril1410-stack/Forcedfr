import json
import os
import subprocess
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException

QB_HOST = os.getenv("QB_HOST", "http://192.168.1.42:8080").rstrip("/")
QB_USERNAME = os.getenv("QB_USERNAME", "")
QB_PASSWORD = os.getenv("QB_PASSWORD", "")

app = FastAPI(
    title="ForcedFR",
    description="Détection précoce des pistes de sous-titres français forcés.",
    version="0.1.2",
)

session = requests.Session()


def qb_login() -> None:
    """Authentifie la session auprès de qBittorrent."""

    response = session.post(
        f"{QB_HOST}/api/v2/auth/login",
        data={
            "username": QB_USERNAME,
            "password": QB_PASSWORD,
        },
        timeout=10,
    )

    response.raise_for_status()

    # qBittorrent 5.x peut retourner HTTP 204 après un login réussi.
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Échec de connexion à qBittorrent : "
            f"HTTP {response.status_code}"
        )

    print("[QB] Authentification réussie")


def qb_request(
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """Effectue une requête authentifiée vers qBittorrent."""

    if not session.cookies:
        qb_login()

    response = session.request(
        method,
        f"{QB_HOST}{endpoint}",
        params=params,
        data=data,
        timeout=15,
    )

    # Session expirée.
    if response.status_code == 403:
        print("[QB] Session expirée, nouvelle authentification")
        qb_login()

        response = session.request(
            method,
            f"{QB_HOST}{endpoint}",
            params=params,
            data=data,
            timeout=15,
        )

    response.raise_for_status()

    if not response.text:
        return None

    if "application/json" in response.headers.get("content-type", ""):
        return response.json()

    return response.text


def get_torrent_context(torrent_hash: str):
    """Récupère torrent + fichiers."""

    torrents = qb_request(
        "GET",
        "/api/v2/torrents/info",
        params={"hashes": torrent_hash},
    )

    if not torrents:
        raise HTTPException(
            status_code=404,
            detail="Torrent introuvable.",
        )

    files = qb_request(
        "GET",
        "/api/v2/torrents/files",
        params={"hash": torrent_hash},
    )

    return torrents[0], files


def find_mkv(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Trouve le MKV principal."""

    mkvs = [
        file
        for file in files
        if str(file.get("name", "")).lower().endswith(".mkv")
    ]

    if not mkvs:
        raise RuntimeError(
            "Aucun fichier MKV trouvé dans le torrent."
        )

    # Dans ton cas il y a normalement un seul MKV.
    # Si plusieurs existent, on prend le plus gros.
    return max(
        mkvs,
        key=lambda file: file.get("size", 0),
    )


def get_media_path(
    torrent: dict[str, Any],
    files: list[dict[str, Any]],
    mkv: dict[str, Any],
) -> str:
    """
    Détermine le chemin réellement accessible dans le conteneur.

    Pour un torrent mono-fichier, content_path est directement
    le chemin complet du MKV.
    """

    content_path = torrent.get("content_path")

    if content_path:
        return content_path

    # Fallback pour les torrents multi-fichiers.
    save_path = torrent.get("save_path", "")
    return str(Path(save_path) / mkv["name"])


def run_ffprobe(file_path: str) -> dict[str, Any]:
    """Analyse les pistes du MKV avec ffprobe."""

    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Fichier introuvable dans le conteneur : {file_path}"
        )

    print(f"[FFPROBE] Analyse : {file_path}")

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type:stream_disposition:stream_tags=language,title",
        "-of",
        "json",
        str(path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe a échoué : {result.stderr.strip()}"
        )

    return json.loads(
        result.stdout or '{"streams":[]}'
    )


def detect_french_forced(
    probe: dict[str, Any],
) -> dict[str, Any]:
    """
    Analyse les pistes de sous-titres.

    On regarde :
    - langue française ;
    - disposition Matroska forced ;
    - titre contenant éventuellement Forced.
    """

    subtitles = []

    for stream in probe.get("streams", []):

        if stream.get("codec_type") != "subtitle":
            continue

        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}

        language = str(
            tags.get("language", "")
        ).lower()

        title = str(
            tags.get("title", "")
        )

        title_lower = title.lower()

        is_french = language in {
            "fr",
            "fra",
            "fre",
            "fra-fr",
            "fre-fr",
        }

        forced_flag = bool(
            disposition.get("forced", 0)
        )

        forced_title = (
            "forced" in title_lower
            or "forcé" in title_lower
            or "force" in title_lower
        )

        subtitle = {
            "index": stream.get("index"),
            "language": language or None,
            "title": title or None,
            "french": is_french,
            "forced_flag": forced_flag,
            "forced_title": forced_title,
        }

        subtitles.append(subtitle)

    forced_french = any(
        subtitle["french"]
        and (
            subtitle["forced_flag"]
            or subtitle["forced_title"]
        )
        for subtitle in subtitles
    )

    print(
        f"[FORCEDFR] FR Forced : "
        f"{'OUI' if forced_french else 'NON'}"
    )

    return {
        "forced_french": forced_french,
        "subtitles": subtitles,
    }


@app.on_event("startup")
def startup() -> None:
    """Connexion initiale à qBittorrent."""

    if not QB_USERNAME or not QB_PASSWORD:
        print(
            "[QB] WARNING : identifiants non configurés."
        )
        return

    try:
        qb_login()
    except Exception as exc:
        print(
            f"[QB] WARNING : connexion impossible : {exc}"
        )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": "0.1.2",
        "qBittorrent": QB_HOST,
    }


@app.get("/torrents")
def torrents() -> Any:
    try:
        return qb_request(
            "GET",
            "/api/v2/torrents/info",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.get("/torrent/{torrent_hash}/inspect")
def inspect(
    torrent_hash: str,
) -> dict[str, Any]:

    try:
        torrent, files = get_torrent_context(
            torrent_hash
        )

        piece_states = qb_request(
            "GET",
            "/api/v2/torrents/pieceStates",
            params={"hash": torrent_hash},
        )

        return {
            "torrent": torrent,
            "files": files,
            "piece_states": piece_states,
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur : {exc}",
        )


@app.get("/torrent/{torrent_hash}/analyze")
def analyze(
    torrent_hash: str,
) -> dict[str, Any]:

    try:
        torrent, files = get_torrent_context(
            torrent_hash
        )

        mkv = find_mkv(files)

        media_path = get_media_path(
            torrent,
            files,
            mkv,
        )

        print(f"[QB] Torrent : {torrent['name']}")
        print(f"[QB] MKV : {media_path}")

        probe = run_ffprobe(
            media_path
        )

        detection = detect_french_forced(
            probe
        )

        return {
            "torrent": {
                "hash": torrent["hash"],
                "name": torrent["name"],
                "progress": torrent["progress"],
                "content_path": torrent["content_path"],
            },
            "file": {
                "path": media_path,
                "size": mkv.get("size"),
            },
            "detection": detection,
            "ffprobe": probe,
        }

    except HTTPException:
        raise

    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="ffprobe a dépassé 60 secondes.",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Analyse impossible : {exc}",
        )


@app.post("/torrent/{torrent_hash}/pause")
def pause(
    torrent_hash: str,
) -> dict[str, bool]:

    try:
        qb_request(
            "POST",
            "/api/v2/torrents/stop",
            data={"hashes": torrent_hash},
        )

        return {"ok": True}

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.post("/torrent/{torrent_hash}/resume")
def resume(
    torrent_hash: str,
) -> dict[str, bool]:

    try:
        qb_request(
            "POST",
            "/api/v2/torrents/start",
            data={"hashes": torrent_hash},
        )

        return {"ok": True}

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )
