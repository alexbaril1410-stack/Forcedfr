import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException


# ============================================================
# CONFIGURATION
# ============================================================

QB_HOST = os.getenv(
    "QB_HOST",
    "http://192.168.1.42:8080",
).rstrip("/")

QB_USERNAME = os.getenv(
    "QB_USERNAME",
    "",
)

QB_PASSWORD = os.getenv(
    "QB_PASSWORD",
    "",
)

POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "10")
)

# Nombre maximum de secondes pendant lesquelles ForcedFR
# tente de rendre le MKV analysable.
ANALYSIS_TIMEOUT = int(
    os.getenv("ANALYSIS_TIMEOUT", "900")
)

# Pour éviter d'essayer ffprobe immédiatement sur un MKV
# pratiquement vide.
MIN_FILE_PROGRESS = float(
    os.getenv("MIN_FILE_PROGRESS", "0.01")
)

LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO",
).upper()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("forcedfr")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="ForcedFR",
    description=(
        "Détection automatique des pistes "
        "de sous-titres français forcés."
    ),
    version="1.0.0",
)


# ============================================================
# SESSION QBittorrent
# ============================================================

qb_session = requests.Session()


# Hash des torrents déjà vus par ForcedFR.
seen_torrents: set[str] = set()

# Hash des torrents actuellement analysés.
processing_torrents: set[str] = set()


# ============================================================
# qBittorrent - AUTHENTIFICATION
# ============================================================

def qb_login() -> None:
    """
    Authentifie la session auprès de qBittorrent.
    """

    response = qb_session.post(
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
            "Authentification qBittorrent échouée : "
            f"HTTP {response.status_code}"
        )

    log.info(
        "Connexion qBittorrent réussie : %s",
        QB_HOST,
    )


# ============================================================
# qBittorrent - REQUÊTES
# ============================================================

def qb_request(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """
    Effectue une requête vers l'API qBittorrent.

    Si qBittorrent retourne 403, on réauthentifie
    automatiquement puis on recommence.
    """

    response = qb_session.request(
        method,
        f"{QB_HOST}{endpoint}",
        params=params,
        data=data,
        timeout=15,
    )

    if response.status_code == 403:

        log.info(
            "Session qBittorrent expirée, "
            "nouvelle authentification."
        )

        qb_login()

        response = qb_session.request(
            method,
            f"{QB_HOST}{endpoint}",
            params=params,
            data=data,
            timeout=15,
        )

    response.raise_for_status()

    if not response.text:
        return None

    content_type = response.headers.get(
        "content-type",
        "",
    )

    if "application/json" in content_type:
        return response.json()

    return response.text


# ============================================================
# qBittorrent - TORRENT
# ============================================================

def get_torrent(
    torrent_hash: str,
) -> dict[str, Any]:
    """
    Récupère les informations d'un torrent.
    """

    torrents = qb_request(
        "GET",
        "/api/v2/torrents/info",
        params={
            "hashes": torrent_hash,
        },
    )

    if not torrents:
        raise HTTPException(
            status_code=404,
            detail="Torrent introuvable.",
        )

    return torrents[0]


def get_torrents() -> list[dict[str, Any]]:
    """
    Récupère tous les torrents présents dans qBittorrent.
    """

    return qb_request(
        "GET",
        "/api/v2/torrents/info",
    )


def get_torrent_files(
    torrent_hash: str,
) -> list[dict[str, Any]]:
    """
    Récupère les fichiers du torrent.
    """

    return qb_request(
        "GET",
        "/api/v2/torrents/files",
        params={
            "hash": torrent_hash,
        },
    )


def get_piece_states(
    torrent_hash: str,
) -> list[int]:
    """
    Récupère l'état des pièces du torrent.
    """

    return qb_request(
        "GET",
        "/api/v2/torrents/pieceStates",
        params={
            "hash": torrent_hash,
        },
    )


# ============================================================
# qBittorrent - ACTIONS
# ============================================================

def toggle_first_last_piece_priority(
    torrent_hash: str,
) -> None:
    """
    Active ou désactive la priorité des premières
    et dernières pièces.
    """

    qb_request(
        "POST",
        "/api/v2/torrents/toggleFirstLastPiecePrio",
        data={
            "hashes": torrent_hash,
        },
    )


def stop_torrent(
    torrent_hash: str,
) -> None:
    """
    Met un torrent en pause.
    """

    qb_request(
        "POST",
        "/api/v2/torrents/stop",
        data={
            "hashes": torrent_hash,
        },
    )

    log.info(
        "[%s] Torrent mis en pause.",
        torrent_hash,
    )


def start_torrent(
    torrent_hash: str,
) -> None:
    """
    Relance un torrent.
    """

    qb_request(
        "POST",
        "/api/v2/torrents/start",
        data={
            "hashes": torrent_hash,
        },
    )

    log.info(
        "[%s] Torrent relancé.",
        torrent_hash,
    )


# ============================================================
# FICHIERS MKV
# ============================================================

def find_mkv_files(
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Retourne tous les fichiers MKV du torrent.

    Un torrent peut contenir :

        Film.mkv

    ou :

        Film/
            Film.mkv
            Film.nfo

    ou plusieurs MKV.
    """

    mkvs = []

    for file in files:

        name = str(
            file.get("name", "")
        )

        if name.lower().endswith(".mkv"):
            mkvs.append(file)

    return sorted(
        mkvs,
        key=lambda item: item.get(
            "size",
            0,
        ),
        reverse=True,
    )


def resolve_mkv_path(
    torrent: dict[str, Any],
    file_info: dict[str, Any],
) -> Path:
    """
    Transforme le chemin fourni par qBittorrent
    en chemin réellement accessible dans le conteneur.
    """

    content_path = Path(
        torrent.get(
            "content_path",
            "",
        )
    )

    save_path = Path(
        torrent.get(
            "save_path",
            "",
        )
    )

    file_name = Path(
        file_info["name"]
    )

    # --------------------------------------------------------
    # CAS 1
    #
    # content_path est directement le MKV
    #
    # /data/Téléchargements/Film.mkv
    # --------------------------------------------------------

    if content_path.suffix.lower() == ".mkv":

        return content_path

    # --------------------------------------------------------
    # CAS 2
    #
    # Torrent dans un dossier
    #
    # save_path/
    #   Film/
    #      Film.mkv
    # --------------------------------------------------------

    candidate = (
        save_path /
        file_name
    )

    if candidate.exists():

        return candidate

    # --------------------------------------------------------
    # CAS 3
    #
    # content_path/
    #   Film.mkv
    # --------------------------------------------------------

    candidate = (
        content_path /
        file_name.name
    )

    if candidate.exists():

        return candidate

    # --------------------------------------------------------
    # CAS 4
    #
    # recherche récursive
    # --------------------------------------------------------

    if content_path.is_dir():

        matches = list(
            content_path.rglob(
                file_name.name
            )
        )

        if matches:

            return matches[0]

    # --------------------------------------------------------
    # Dernier recours
    # --------------------------------------------------------

    return (
        save_path /
        file_name
    )


# ============================================================
# FFPROBE
# ============================================================

def run_ffprobe(
    file_path: Path,
) -> dict[str, Any]:
    """
    Analyse le conteneur MKV avec ffprobe.
    """

    if not file_path.exists():

        raise FileNotFoundError(
            f"Fichier introuvable : {file_path}"
        )

    if not file_path.is_file():

        raise RuntimeError(
            f"Le chemin n'est pas un fichier : "
            f"{file_path}"
        )

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "stream=index,codec_type:"
            "stream_disposition:"
            "stream_tags=language,title"
        ),
        "-of",
        "json",
        str(file_path),
    ]

    log.info(
        "[FFPROBE] Analyse : %s",
        file_path,
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:

        error = result.stderr.strip()

        raise RuntimeError(
            error or "ffprobe a échoué."
        )

    return json.loads(
        result.stdout or '{"streams":[]}'
    )


# ============================================================
# DÉTECTION FR FORCED
# ============================================================

def detect_french_forced(
    probe: dict[str, Any],
) -> dict[str, Any]:
    """
    Détecte les pistes de sous-titres françaises forcées.

    Conditions :

        langue française

        ET

        forced=1

        OU

        titre indiquant clairement FORCED.
    """

    subtitles = []

    forced_french = []

    for stream in probe.get(
        "streams",
        [],
    ):

        if stream.get(
            "codec_type"
        ) != "subtitle":

            continue

        tags = stream.get(
            "tags"
        ) or {}

        disposition = stream.get(
            "disposition"
        ) or {}

        language = str(
            tags.get(
                "language",
                "",
            )
        ).lower()

        title = str(
            tags.get(
                "title",
                "",
            )
        )

        title_lower = title.lower()

        french = language in {
            "fr",
            "fra",
            "fre",
        }

        forced_flag = (
            int(
                disposition.get(
                    "forced",
                    0,
                )
            )
            == 1
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
            "index": stream.get(
                "index"
            ),
            "language": language or None,
            "title": title or None,
            "french": french,
            "forced_flag": forced_flag,
            "forced_title": forced_title,
        }

        subtitles.append(
            subtitle
        )

        if french and (
            forced_flag
            or forced_title
        ):

            forced_french.append(
                subtitle
            )

    result = {
        "forced_french": bool(
            forced_french
        ),
        "subtitles": subtitles,
        "forced_tracks": forced_french,
    }

    log.info(
        "[FORCEDFR] FR Forced : %s",
        "OUI"
        if result["forced_french"]
        else "NON",
    )

    return result


# ============================================================
# ANALYSE D'UN TORRENT
# ============================================================

def analyze_torrent(
    torrent_hash: str,
) -> dict[str, Any]:
    """
    Analyse tous les MKV du torrent.
    """

    torrent = get_torrent(
        torrent_hash
    )

    files = get_torrent_files(
        torrent_hash
    )

    mkvs = find_mkv_files(
        files
    )

    if not mkvs:

        raise RuntimeError(
            "Aucun fichier MKV trouvé."
        )

    results = []

    for mkv in mkvs:

        path = resolve_mkv_path(
            torrent,
            mkv,
        )

        # Si le fichier n'existe pas encore
        # ou est encore inaccessible, on provoque
        # une erreur permettant au système de réessayer.
        if not path.exists():

            raise FileNotFoundError(
                f"MKV pas encore disponible : {path}"
            )

        if not path.is_file():

            raise RuntimeError(
                f"Le chemin du MKV est un dossier : "
                f"{path}"
            )

        # qBittorrent nous donne également
        # la progression du fichier.
        file_progress = float(
            mkv.get(
                "progress",
                0,
            )
        )

        if file_progress < MIN_FILE_PROGRESS:

            raise RuntimeError(
                "MKV encore trop peu téléchargé "
                f"({file_progress:.4%})."
            )

        probe = run_ffprobe(
            path
        )

        detection = detect_french_forced(
            probe
        )

        results.append(
            {
                "path": str(path),
                "size": mkv.get(
                    "size"
                ),
                "progress": file_progress,
                "detection": detection,
                "ffprobe": probe,
            }
        )

    forced_french = any(
        result[
            "detection"
        ][
            "forced_french"
        ]
        for result in results
    )

    return {
        "torrent": {
            "hash": torrent[
                "hash"
            ],
            "name": torrent[
                "name"
            ],
            "progress": torrent[
                "progress"
            ],
            "content_path": torrent[
                "content_path"
            ],
            "f_l_piece_prio": torrent.get(
                "f_l_piece_prio"
            ),
        },
        "forced_french": forced_french,
        "files": results,
    }


# ============================================================
# ANALYSE AVEC PRIORITÉ TEMPORAIRE
# ============================================================

def analyze_with_temporary_priority(
    torrent_hash: str,
) -> dict[str, Any]:
    """
    Active temporairement f_l_piece_prio.

    L'état initial est conservé.

    Exemple :

        false
          ↓
        true
          ↓
       analyse
          ↓
        false

    Si l'état initial est déjà true :

        true
          ↓
       analyse
          ↓
        true
    """

    torrent = get_torrent(
        torrent_hash
    )

    initial_priority = bool(
        torrent.get(
            "f_l_piece_prio",
            False,
        )
    )

    priority_changed = False

    try:

        if not initial_priority:

            log.info(
                "[%s] Activation "
                "f_l_piece_prio.",
                torrent_hash,
            )

            toggle_first_last_piece_priority(
                torrent_hash
            )

            priority_changed = True

        else:

            log.info(
                "[%s] f_l_piece_prio "
                "déjà actif.",
                torrent_hash,
            )

        result = analyze_torrent(
            torrent_hash
        )

        return result

    finally:

        # ----------------------------------------------------
        # IMPORTANT
        #
        # On restaure uniquement si ForcedFR
        # avait lui-même activé l'option.
        # ----------------------------------------------------

        if priority_changed:

            log.info(
                "[%s] Désactivation "
                "f_l_piece_prio.",
                torrent_hash,
            )

            try:

                toggle_first_last_piece_priority(
                    torrent_hash
                )

            except Exception:

                log.exception(
                    "[%s] Impossible de restaurer "
                    "f_l_piece_prio.",
                    torrent_hash,
                )


# ============================================================
# TRAITEMENT AUTOMATIQUE
# ============================================================

def process_new_torrent(
    torrent_hash: str,
) -> None:
    """
    Surveille un nouveau torrent jusqu'à ce que
    son MKV puisse être analysé.

    Si une piste FR Forced est trouvée :
        → on ne touche plus au torrent.

    Si aucune piste FR Forced :
        → pause du torrent.

    Pour le moment Discord n'est pas encore intégré.
    """

    if torrent_hash in processing_torrents:

        return

    processing_torrents.add(
        torrent_hash
    )

    log.info(
        "[%s] Nouveau torrent détecté.",
        torrent_hash,
    )

    started_at = time.time()

    try:

        while True:

            # ------------------------------------------------
            # Timeout
            # ------------------------------------------------

            elapsed = (
                time.time()
                - started_at
            )

            if elapsed >= ANALYSIS_TIMEOUT:

                log.warning(
                    "[%s] Timeout après %ss.",
                    torrent_hash,
                    ANALYSIS_TIMEOUT,
                )

                # On ne laisse pas le torrent
                # continuer indéfiniment sans analyse.
                try:
                    stop_torrent(
                        torrent_hash
                    )
                except Exception:
                    log.exception(
                        "[%s] Impossible de mettre "
                        "le torrent en pause.",
                        torrent_hash,
                    )

                return

            # ------------------------------------------------
            # Vérifier que le torrent existe toujours
            # ------------------------------------------------

            try:

                torrent = get_torrent(
                    torrent_hash
                )

            except Exception:

                log.exception(
                    "[%s] Impossible de récupérer "
                    "le torrent.",
                    torrent_hash,
                )

                time.sleep(
                    POLL_SECONDS
                )

                continue

            # ------------------------------------------------
            # Si le torrent est déjà terminé,
            # on peut analyser directement.
            # ------------------------------------------------

            progress = float(
                torrent.get(
                    "progress",
                    0,
                )
            )

            log.info(
                "[%s] Progression : %.2f%%",
                torrent_hash,
                progress * 100,
            )

            # ------------------------------------------------
            # Tentative d'analyse
            # ------------------------------------------------

            try:

                result = (
                    analyze_with_temporary_priority(
                        torrent_hash
                    )
                )

                forced = result[
                    "forced_french"
                ]

                if forced:

                    log.info(
                        "[%s] ✅ FR Forced détecté.",
                        torrent_hash,
                    )

                    return

                # ------------------------------------------------
                # Analyse réussie mais aucune piste FR Forced.
                # ------------------------------------------------

                log.warning(
                    "[%s] ❌ Aucune piste "
                    "FR Forced détectée.",
                    torrent_hash,
                )

                stop_torrent(
                    torrent_hash
                )

                # Discord sera ajouté ici.
                #
                # Plus tard :
                #
                # discord_notify(
                #     torrent,
                #     result
                # )

                return

            except FileNotFoundError as exc:

                log.info(
                    "[%s] MKV pas encore disponible : %s",
                    torrent_hash,
                    exc,
                )

            except RuntimeError as exc:

                log.info(
                    "[%s] Analyse impossible pour "
                    "le moment : %s",
                    torrent_hash,
                    exc,
                )

            except subprocess.TimeoutExpired:

                log.warning(
                    "[%s] ffprobe a dépassé "
                    "son timeout.",
                    torrent_hash,
                )

            except Exception:

                log.exception(
                    "[%s] Erreur pendant l'analyse.",
                    torrent_hash,
                )

            # ------------------------------------------------
            # Attendre avant nouvel essai
            # ------------------------------------------------

            time.sleep(
                POLL_SECONDS
            )

    finally:

        processing_torrents.discard(
            torrent_hash
        )

        log.info(
            "[%s] Fin du traitement.",
            torrent_hash,
        )


# ============================================================
# SURVEILLANCE qBittorrent
# ============================================================

async def monitor_qbittorrent() -> None:
    """
    Surveillance permanente de qBittorrent.

    Toutes les POLL_SECONDS secondes :

        1. récupère les torrents ;
        2. identifie ceux jamais vus ;
        3. lance leur analyse.
    """

    log.info(
        "Surveillance qBittorrent démarrée."
    )

    while True:

        try:

            torrents = get_torrents()

            current_hashes = set()

            for torrent in torrents:

                torrent_hash = torrent.get(
                    "hash"
                )

                if not torrent_hash:
                    continue

                current_hashes.add(
                    torrent_hash
                )

                # --------------------------------------------
                # Nouveau torrent
                # --------------------------------------------

                if torrent_hash not in seen_torrents:

                    seen_torrents.add(
                        torrent_hash
                    )

                    log.info(
                        "🆕 Nouveau torrent : %s",
                        torrent.get(
                            "name",
                            torrent_hash,
                        ),
                    )

                    # ----------------------------------------
                    # On lance le traitement dans un thread
                    # pour ne pas bloquer FastAPI.
                    # ----------------------------------------

                    asyncio.create_task(
                        asyncio.to_thread(
                            process_new_torrent,
                            torrent_hash,
                        )
                    )

            # ------------------------------------------------
            # IMPORTANT :
            #
            # On ne retire PAS les torrents de seen_torrents
            # immédiatement.
            #
            # Cela évite qu'un torrent supprimé puis
            # réapparu provoque des comportements imprévus
            # pendant cette V1.
            # ------------------------------------------------

        except Exception:

            log.exception(
                "Erreur dans la surveillance qBittorrent."
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup() -> None:

    log.info(
        "ForcedFR démarrage."
    )

    log.info(
        "qBittorrent : %s",
        QB_HOST,
    )

    if not QB_USERNAME or not QB_PASSWORD:

        log.warning(
            "QB_USERNAME ou QB_PASSWORD "
            "non configuré."
        )

        return

    try:

        qb_login()

    except Exception:

        log.exception(
            "Connexion initiale qBittorrent échouée."
        )

    # --------------------------------------------------------
    # Initialisation importante :
    #
    # Les torrents déjà présents au démarrage ne sont PAS
    # considérés comme nouveaux.
    #
    # ForcedFR s'intéresse aux torrents arrivant après
    # son démarrage.
    # --------------------------------------------------------

    try:

        torrents = get_torrents()

        for torrent in torrents:

            torrent_hash = torrent.get(
                "hash"
            )

            if torrent_hash:

                seen_torrents.add(
                    torrent_hash
                )

        log.info(
            "%d torrent(s) existant(s) ignoré(s) "
            "au démarrage.",
            len(seen_torrents),
        )

    except Exception:

        log.exception(
            "Impossible d'initialiser la liste "
            "des torrents existants."
        )

    asyncio.create_task(
        monitor_qbittorrent()
    )


# ============================================================
# API
# ============================================================

@app.get("/health")
def health() -> dict[str, Any]:

    return {
        "status": "ok",
        "version": "1.0.0",
        "qbittorrent": QB_HOST,
        "monitoring": True,
        "poll_seconds": POLL_SECONDS,
    }


@app.get("/torrents")
def torrents() -> Any:

    try:

        return get_torrents()

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Erreur qBittorrent : "
                f"{exc}"
            ),
        )


@app.get(
    "/torrent/{torrent_hash}/inspect"
)
def inspect(
    torrent_hash: str,
) -> dict[str, Any]:

    try:

        torrent = get_torrent(
            torrent_hash
        )

        files = get_torrent_files(
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
            detail=(
                "Erreur : "
                f"{exc}"
            ),
        )


@app.get(
    "/torrent/{torrent_hash}/analyze"
)
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
            detail=(
                "ffprobe a dépassé "
                "60 secondes."
            ),
        )

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Analyse impossible : "
                f"{exc}"
            ),
        )


@app.post(
    "/torrent/{torrent_hash}/pause"
)
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
            detail=(
                "Erreur qBittorrent : "
                f"{exc}"
            ),
        )


@app.post(
    "/torrent/{torrent_hash}/resume"
)
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
            detail=(
                "Erreur qBittorrent : "
                f"{exc}"
            ),
        )
