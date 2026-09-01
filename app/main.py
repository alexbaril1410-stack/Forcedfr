import os
from typing import Any

import requests
from fastapi import FastAPI, HTTPException

QB_HOST = os.getenv("QB_HOST", "").rstrip("/")
QB_USERNAME = os.getenv("QB_USERNAME", "")
QB_PASSWORD = os.getenv("QB_PASSWORD", "")

app = FastAPI(
    title="ForcedFR",
    description="Détection précoce des pistes de sous-titres français forcés.",
    version="0.1.0",
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

    if response.text.strip() != "Ok.":
        raise RuntimeError(
            f"Échec de connexion à qBittorrent: {response.text}"
        )


def qb_request(
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """Effectue une requête vers l'API qBittorrent."""

    url = f"{QB_HOST}{endpoint}"

    response = session.request(
        method,
        url,
        params=params,
        data=data,
        timeout=15,
    )

    # Session expirée : nouvelle authentification puis nouvelle tentative.
    if response.status_code == 403:
        qb_login()

        response = session.request(
            method,
            url,
            params=params,
            data=data,
            timeout=15,
        )

    response.raise_for_status()

    if not response.text:
        return None

    content_type = response.headers.get("content-type", "")

    if "application/json" in content_type:
        return response.json()

    return response.text


@app.on_event("startup")
def startup() -> None:
    """Teste la connexion à qBittorrent au démarrage."""

    if not QB_USERNAME or not QB_PASSWORD:
        print(
            "WARNING: QB_USERNAME ou QB_PASSWORD n'est pas configuré."
        )
        return

    try:
        qb_login()
        print(f"Connexion qBittorrent réussie : {QB_HOST}")
    except Exception as exc:
        print(f"WARNING: impossible de se connecter à qBittorrent : {exc}")


@app.get("/health")
def health() -> dict[str, Any]:
    """Vérifie que ForcedFR fonctionne."""

    return {
        "status": "ok",
        "version": "0.1.0",
        "qbittorrent": QB_HOST,
    }


@app.get("/torrents")
def get_torrents() -> Any:
    """Retourne les torrents actuellement présents dans qBittorrent."""

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


@app.get("/torrent/{torrent_hash}")
def get_torrent(torrent_hash: str) -> Any:
    """Retourne les informations d'un torrent."""

    try:
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

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.get("/torrent/{torrent_hash}/files")
def get_torrent_files(torrent_hash: str) -> Any:
    """Retourne les fichiers contenus dans un torrent."""

    try:
        return qb_request(
            "GET",
            "/api/v2/torrents/files",
            params={
                "hash": torrent_hash,
            },
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.get("/torrent/{torrent_hash}/pieces")
def get_piece_states(torrent_hash: str) -> Any:
    """Retourne l'état de chaque pièce du torrent."""

    try:
        return qb_request(
            "GET",
            "/api/v2/torrents/pieceStates",
            params={
                "hash": torrent_hash,
            },
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.get("/torrent/{torrent_hash}/inspect")
def inspect_torrent(torrent_hash: str) -> dict[str, Any]:
    """
    Retourne toutes les informations nécessaires à notre phase de test.

    Aucun changement n'est effectué sur le torrent.
    """

    try:
        torrent = qb_request(
            "GET",
            "/api/v2/torrents/info",
            params={
                "hashes": torrent_hash,
            },
        )

        if not torrent:
            raise HTTPException(
                status_code=404,
                detail="Torrent introuvable.",
            )

        files = qb_request(
            "GET",
            "/api/v2/torrents/files",
            params={
                "hash": torrent_hash,
            },
        )

        piece_states = qb_request(
            "GET",
            "/api/v2/torrents/pieceStates",
            params={
                "hash": torrent_hash,
            },
        )

        return {
            "torrent": torrent[0],
            "files": files,
            "piece_states": piece_states,
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.post("/torrent/{torrent_hash}/pause")
def pause_torrent(torrent_hash: str) -> dict[str, bool]:
    """
    Met un torrent en pause.

    Endpoint de test : il sera utilisé plus tard par le moteur de décision.
    """

    try:
        qb_request(
            "POST",
            "/api/v2/torrents/stop",
            data={
                "hashes": torrent_hash,
            },
        )

        return {"ok": True}

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )


@app.post("/torrent/{torrent_hash}/resume")
def resume_torrent(torrent_hash: str) -> dict[str, bool]:
    """Reprend un torrent."""

    try:
        qb_request(
            "POST",
            "/api/v2/torrents/start",
            data={
                "hashes": torrent_hash,
            },
        )

        return {"ok": True}

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur qBittorrent : {exc}",
        )
