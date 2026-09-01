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

app = FastAPI(title="ForcedFR", version="0.1.1")
session = requests.Session()


def qb_login() -> None:
    r = session.post(
        f"{QB_HOST}/api/v2/auth/login",
        data={"username": QB_USERNAME, "password": QB_PASSWORD},
        timeout=10,
    )
    r.raise_for_status()
    if r.text.strip() != "Ok.":
        raise RuntimeError(f"Échec connexion qBittorrent: {r.text}")


def qb_request(method: str, endpoint: str, params=None, data=None) -> Any:
    r = session.request(
        method, f"{QB_HOST}{endpoint}",
        params=params, data=data, timeout=15
    )
    if r.status_code == 403:
        qb_login()
        r = session.request(
            method, f"{QB_HOST}{endpoint}",
            params=params, data=data, timeout=15
        )
    r.raise_for_status()
    if not r.text:
        return None
    return r.json() if "application/json" in r.headers.get("content-type", "") else r.text


def torrent_context(h: str):
    torrents = qb_request("GET", "/api/v2/torrents/info", {"hashes": h})
    if not torrents:
        raise HTTPException(404, "Torrent introuvable.")
    files = qb_request("GET", "/api/v2/torrents/files", {"hash": h})
    return torrents[0], files


def find_mkv(files):
    mkvs = [f for f in files if str(f.get("name", "")).lower().endswith(".mkv")]
    if not mkvs:
        raise RuntimeError("Aucun fichier MKV trouvé.")
    return max(mkvs, key=lambda f: f.get("size", 0))


def run_ffprobe(path: str):
    if not Path(path).exists():
        raise FileNotFoundError(path)

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=index,codec_type:stream_tags=language,title",
        "-of", "json", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffprobe a échoué")
    return json.loads(r.stdout or '{"streams":[]}')


def detect_french_forced(probe):
    subtitles = []
    for s in probe.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        language = str(tags.get("language", "")).lower()
        title = str(tags.get("title", "")).lower()
        french = language in {"fr", "fra", "fre", "fra-fr", "fre-fr"}
        forced_title = "forced" in title or "forcé" in title
        subtitles.append({
            "index": s.get("index"),
            "language": language or None,
            "title": tags.get("title"),
            "french": french,
            "forced_by_title": forced_title,
        })
    return {
        "forced_french": any(x["french"] and x["forced_by_title"] for x in subtitles),
        "subtitles": subtitles,
        "confidence": "metadata-title-only",
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.1", "qBittorrent": QB_HOST}


@app.get("/torrents")
def torrents():
    try:
        qb_login()
        return qb_request("GET", "/api/v2/torrents/info")
    except Exception as e:
        raise HTTPException(502, f"Erreur qBittorrent : {e}")


@app.get("/torrent/{h}/inspect")
def inspect(h: str):
    try:
        qb_login()
        torrent, files = torrent_context(h)
        states = qb_request("GET", "/api/v2/torrents/pieceStates", {"hash": h})
        return {"torrent": torrent, "files": files, "piece_states": states}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Erreur : {e}")


@app.get("/torrent/{h}/analyze")
def analyze(h: str):
    try:
        qb_login()
        torrent, files = torrent_context(h)
        mkv = find_mkv(files)
        probe = run_ffprobe(mkv["name"])
        detection = detect_french_forced(probe)
        return {
            "torrent": {
                "hash": torrent["hash"],
                "name": torrent["name"],
                "progress": torrent["progress"],
                "content_path": torrent["content_path"],
            },
            "file": {"path": mkv["name"], "size": mkv.get("size")},
            "detection": detection,
            "ffprobe": probe,
        }
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(404, f"Fichier inaccessible depuis ForcedFR : {e}")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "ffprobe a dépassé 60 secondes.")
    except Exception as e:
        raise HTTPException(502, f"Analyse impossible : {e}")


@app.post("/torrent/{h}/pause")
def pause(h: str):
    try:
        qb_login()
        qb_request("POST", "/api/v2/torrents/stop", data={"hashes": h})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Erreur qBittorrent : {e}")


@app.post("/torrent/{h}/resume")
def resume(h: str):
    try:
        qb_login()
        qb_request("POST", "/api/v2/torrents/start", data={"hashes": h})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Erreur qBittorrent : {e}")
