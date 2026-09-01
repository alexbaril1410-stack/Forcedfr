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
    version="1.0.0",
)

session = requests.Session()


# ============================================================
# qBittorrent
# ============================================================

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

    # Session expirée
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


def get_torrent_context(
    torrent_hash: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Récupère le torrent et la liste de ses fichiers."""

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


def get_piece_states(torrent_hash: str) -> list[int]:
    """Retourne l'état des pièces du torrent."""

    return qb_request(
        "GET",
        "/api/v2/torrents/pieceStates",
        params={"hash": torrent_hash},
    )


def toggle_first_last_piece_priority(
    torrent_hash: str,
) -> None:
    """
    Active/désactive la priorité des premières/dernières pièces.
    """

    qb_request(
        "POST",
        "/api/v2/torrents/toggleFirstLastPiecePrio",
        data={"hashes": torrent_hash},
    )


def stop_torrent(torrent_hash: str) -> None:
    """Met le torrent en pause."""

    qb_request(
        "POST",
        "/api/v2/torrents/stop",
        data={"hashes": torrent_hash},
    )


def start_torrent(torrent_hash: str) -> None:
    """Relance le torrent."""

    qb_request(
        "POST",
        "/api/v2/torrents/start",
        data={"hashes": torrent_hash},
    )


# ============================================================
# Gestion des fichiers MKV
# ============================================================

def find_mkvs(
    torrent: dict[str, Any],
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Trouve tous les fichiers MKV du torrent.

    Gère :
      - torrent contenant directement un MKV ;
      - torrent contenant un dossier avec un ou plusieurs MKV.
    """

    mkvs = [
        file
        for file in files
        if str(file.get("name", "")).lower().endswith(".mkv")
    ]

    if not mkvs:
        raise RuntimeError(
            "Aucun fichier MKV trouvé dans le torrent."
        )

    return sorted(
        mkvs,
        key=lambda file: file.get("size", 0),
        reverse=True,
    )


def resolve_file_path(
    torrent: dict[str, Any],
    file_info: dict[str, Any],
) -> Path:
    """
    Transforme le chemin qBittorrent en chemin réel dans le conteneur.

    Cas 1 :
        content_path = /data/Téléchargements/Film.mkv

    Cas 2 :
        content_path = /data/Téléchargements/Film
        files.name = Film/Film.mkv
    """

    content_path = Path(torrent.get("content_path", ""))
    save_path = Path(torrent.get("save_path", ""))

    file_name = Path(file_info["name"])

    # --------------------------------------------------------
    # Cas 1 : content_path est directement le MKV
    # --------------------------------------------------------

    if content_path.suffix.lower() == ".mkv":
        return content_path

    # --------------------------------------------------------
    # Cas 2 : content_path est le dossier racine du torrent
    # --------------------------------------------------------

    # qBittorrent fournit files[].name relatif au save_path.
    candidate = save_path / file_name

    if candidate.exists():
        return candidate

    # --------------------------------------------------------
    # Fallback : content_path + nom du fichier
    # --------------------------------------------------------

    candidate = content_path / file_name.name

    if candidate.exists():
        return candidate

    # --------------------------------------------------------
    # Dernier fallback :
    # recherche récursive dans le dossier.
    # --------------------------------------------------------

    if content_path.is_dir():
        matches = list(content_path.rglob(file_name.name))

        if matches:
            return matches[0]

    # On retourne malgré tout le chemin le plus probable.
    return save_path / file_name


# ============================================================
# ffprobe
# ============================================================

def run_ffprobe(
    file_path: Path,
) -> dict[str, Any]:
    """Analyse les pistes du MKV avec ffprobe."""

    if not file_path.exists():
        raise FileNotFoundError(
            f"Fichier introuvable dans le conteneur : {file_path}"
        )

    if not file_path.is_file():
        raise RuntimeError(
            f"Le chemin n'est pas un fichier : {file_path}"
        )

    print(f"[FFPROBE] Analyse : {file_path}")

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type:"
        "stream_disposition:"
        "stream_tags=language,title",
        "-of",
        "json",
        str(file_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        error = result.stderr.strip()

        raise RuntimeError(
            f"ffprobe a échoué : {error}"
        )

    return json.loads(
        result.stdout or '{"streams":[]}'
    )


# ============================================================
# Détection FR Forced
# ============================================================

def detect_french_forced(
    probe: dict[str, Any],
) -> dict[str, Any]:
    """
    Analyse les pistes de sous-titres.

    Une piste est considérée comme FR Forced si :

        langue française
        ET
        (
            flag forced = 1
            OU
            titre contenant un indicateur Forced
        )
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

        forced_title = any(
            marker in title_lower
            for marker in (
                "forced",
                "force",
                "forcé",
            )
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
        "[FORCEDFR] FR Forced : "
        f"{'OUI' if forced_french else 'NON'}"
    )

    return {
        "forced_french": forced_french,
        "subtitles": subtitles,
    }


# ============================================================
# Analyse
# ============================================================

def analyze_torrent(
    torrent_hash: str,
) -> dict[str, Any]:
    """
    Analyse tous les MKV du torrent.

    Le torrent peut contenir :
      - un MKV ;
      - plusieurs MKV ;
      - un NFO ;
      - un dossier racine.
    """

    torrent, files = get_torrent_context(
        torrent_hash
    )

    mkvs = find_mkvs(
        torrent,
        files,
    )

    results = []

    for mkv in mkvs:

        path = resolve_file_path(
            torrent,
            mkv,
        )

        print(
            f"[QB] MKV détecté : {path}"
        )

        probe = run_ffprobe(path)

        detection = detect_french_forced(
            probe
        )

        results.append(
            {
                "path": str(path),
                "size": mkv.get("size"),
                "detection": detection,
                "ffprobe": probe,
            }
        )

    forced_french = any(
        result["detection"]["forced_french"]
        for result in results
    )

    return {
        "torrent": {
            "hash": torrent["hash"],
            "name": torrent["name"],
            "progress": torrent["progress"],
            "content_path": torrent["content_path"],
            "f_l_piece_prio": torrent.get(
                "f_l_piece_prio"
            ),
        },
        "forced_french": forced_french,
        "files": results,
    }


# ============================================================
# Gestion temporaire f_l_piece_prio
# ============================================================

def analyze_with_temporary_priority(
    torrent_hash: str,
) -> dict[str, Any]:
    """
    Active temporairement f_l_piece_prio si nécessaire.

    L'état initial est toujours restauré dans finally.

    Exemple :

        false
          ↓
        true
          ↓
       analyse
          ↓
        false

    Si l'état initial était déjà true :

        true
          ↓
       analyse
          ↓
        true
    """

    torrent, _ = get_torrent_context(
        torrent_hash
    )

    initial_priority = bool(
        torrent.get("f_l_piece_prio", False)
    )

    priority_changed = False

    try:

        if not initial_priority:

            print(
                "[QB] Activation temporaire "
                "de f_l_piece_prio"
            )

            toggle_first_last_piece_priority(
                torrent_hash
            )

            priority_changed = True

        else:

            print(
                "[QB] f_l_piece_prio déjà actif"
            )

        return analyze_torrent(
            torrent_hash
        )

    finally:

        if priority_changed:

            print(
                "[QB] Restauration de "
                "f_l_piece_prio=false"
            )

            try:
                toggle_first_last_piece_priority(
                    torrent_hash
                )
            except Exception as exc:
                print(
                    "[QB] ERREUR restauration "
                    f"f_l_piece_prio : {exc}"
                )


# ============================================================
# API
# ============================================================

@app.on_event("startup")
def startup() -> None:

    if not QB_USERNAME or not QB_PASSWORD:

        print(
            "[QB] WARNING : identifiants "
            "non configurés."
        )

        return

    try:

        qb_login()

    except Exception as exc:

        print(
            f"[QB] WARNING : connexion "
            f"impossible : {exc}"
        )


@app.get("/health")
def health() -> dict[str, Any]:

    return {
        "status": "ok",
        "version": "1.0.0",
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

        piece_states = get_piece_states(
            torrent_hash
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

        return analyze_with_temporary_priority(
            torrent_hash
        )

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

        stop_torrent(
            torrent_hash
        )

        return {
            "ok": True
        }

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

        start_torrent(
            torrent_hash
        )

        return {
            "ok": True
        }

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )
