import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

try:
    import discord
except ImportError:
    discord = None


# ============================================================
# CONFIGURATION
# ============================================================

QB_HOST = os.getenv(
    "QB_HOST",
    "http://192.168.1.42:8080",
).rstrip("/")

QB_USERNAME = os.getenv("QB_USERNAME", "")
QB_PASSWORD = os.getenv("QB_PASSWORD", "")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()

RADARR_URL = os.getenv("RADARR_URL", "http://192.168.1.42:7878").rstrip("/")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "").strip()

SONARR_URL = os.getenv("SONARR_URL", "http://192.168.1.42:8989").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "").strip()

# Vérification très fréquente des nouveaux torrents
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "1"))
ANALYSIS_POLL_SECONDS = 0.5

# Temps maximum consacré à la recherche d'un MKV analysable
ANALYSIS_TIMEOUT = int(
    os.getenv("ANALYSIS_TIMEOUT", "900")
)

LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO",
).upper()

# Recherche du contexte Radarr/Sonarr avant notification Discord
# 1 tentative toutes les 5 secondes pendant 1 minute maximum.
RELEASE_CONTEXT_RETRY_SECONDS = 5
RELEASE_CONTEXT_TIMEOUT = 60

# Stabilisation de qBittorrent au démarrage : évite de considérer comme nouveaux
# les torrents restaurés progressivement après un redémarrage du serveur.
STARTUP_STABILITY_CHECK_SECONDS = 5
STARTUP_STABLE_CHECKS_REQUIRED = 3


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
    description="Détection automatique des pistes françaises forcées.",
    version="2.0.0",
)


# ============================================================
# SESSION qBITTORRENT
# ============================================================

qb_session = requests.Session()


# ============================================================
# ÉTAT DE LA SURVEILLANCE
# ============================================================

previous_torrents: set[str] = set()

processing_torrents: set[str] = set()

MAIN_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
discord_bot: Any = None

# Évite qu'une même notification Discord soit traitée plusieurs fois.
resolved_discord_actions: set[str] = set()


# ============================================================
# AUTHENTIFICATION qBITTORRENT
# ============================================================

def qb_login() -> None:
    response = qb_session.post(
        f"{QB_HOST}/api/v2/auth/login",
        data={
            "username": QB_USERNAME,
            "password": QB_PASSWORD,
        },
        timeout=10,
    )

    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Authentification qBittorrent échouée : "
            f"HTTP {response.status_code} "
            f"{response.text}"
        )

    log.info(
        "Connexion qBittorrent réussie : %s",
        QB_HOST,
    )


# ============================================================
# REQUÊTES qBITTORRENT
# ============================================================

def qb_request(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:

    response = qb_session.request(
        method,
        f"{QB_HOST}{endpoint}",
        params=params,
        data=data,
        timeout=15,
    )

    # Session expirée
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
# INFORMATIONS TORRENTS
# ============================================================

def get_torrents() -> list[dict[str, Any]]:

    return qb_request(
        "GET",
        "/api/v2/torrents/info",
    )


def get_torrent(
    torrent_hash: str,
) -> dict[str, Any]:

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


def get_torrent_files(
    torrent_hash: str,
) -> list[dict[str, Any]]:

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

    return qb_request(
        "GET",
        "/api/v2/torrents/pieceStates",
        params={
            "hash": torrent_hash,
        },
    )


# ============================================================
# ACTIONS qBITTORRENT
# ============================================================

def toggle_first_last_piece_priority(
    torrent_hash: str,
) -> None:

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
# DISCORD / RADARR / SONARR
# ============================================================

def arr_item_url_lookup(
    base_url: str,
    api_key: str,
    source: str,
    item_id: Any,
) -> str:
    """
    Construit l'URL de la page web Radarr/Sonarr.

    Les IDs présents dans l'historique sont des IDs internes Arr.
    L'interface web utilise des IDs externes :
    - Radarr : tmdbId
    - Sonarr : tvdbId

    En cas d'échec de la requête complémentaire, on conserve
    l'URL basée sur l'ID interne afin de ne jamais bloquer
    la notification Discord.
    """

    if item_id is None:
        return base_url

    fallback = (
        f"{base_url}/movie/{item_id}"
        if source == "Radarr"
        else f"{base_url}/series/{item_id}"
    )

    try:
        endpoint = (
            f"{base_url}/api/v3/movie/{item_id}"
            if source == "Radarr"
            else f"{base_url}/api/v3/series/{item_id}"
        )

        response = requests.get(
            endpoint,
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        response.raise_for_status()

        item = response.json()

        if source == "Radarr":
            external_id = item.get("tmdbId")
            if external_id:
                return f"{base_url}/movie/{external_id}"

        else:
            external_id = item.get("tvdbId")
            if external_id and str(external_id) != "0":
                return f"{base_url}/series/{external_id}"

        log.warning(
            "[%s] Identifiant externe introuvable pour %s ID %s. "
            "Utilisation de l'URL de secours.",
            source,
            source,
            item_id,
        )

    except Exception as exc:
        log.warning(
            "Impossible de récupérer l'identifiant externe %s "
            "(ID interne %s) : %s. Utilisation de l'URL de secours.",
            source,
            item_id,
            exc,
        )

    return fallback


def arr_history_lookup(
    base_url: str,
    api_key: str,
    torrent_hash: str,
    source: str,
) -> dict[str, Any] | None:

    if not base_url or not api_key:
        return None

    try:

        response = requests.get(
            f"{base_url}/api/v3/history",
            headers={"X-Api-Key": api_key},
            params={"pageSize": 1000},
            timeout=10,
        )

        response.raise_for_status()

        records = response.json().get("records", [])

        matches = [
            item
            for item in records
            if str(item.get("downloadId", "")).lower()
            == torrent_hash.lower()
        ]

        if not matches:
            return None

        grabbed = next(
            (
                item
                for item in matches
                if item.get("eventType") == "grabbed"
            ),
            matches[0],
        )

        data = grabbed.get("data", {}) or {}

        item_id = (
            grabbed.get("movieId")
            if source == "Radarr"
            else grabbed.get("seriesId")
        )

        arr_item_url = arr_item_url_lookup(
            base_url,
            api_key,
            source,
            item_id,
        )

        return {
            "source": source,
            "title": grabbed.get("sourceTitle"),
            "indexer": data.get("indexer"),
            "tracker_url": (
                data.get("nzbInfoUrl")
                or data.get("infoUrl")
            ),
            "arr_item_url": arr_item_url,
            "item_id": item_id,
            "event_type": grabbed.get("eventType"),
        }

    except Exception as exc:

        log.warning(
            "[%s] Impossible de récupérer l'historique %s : %s",
            torrent_hash,
            source,
            exc,
        )

        return None


def get_release_context(
    torrent_hash: str,
) -> dict[str, Any]:

    radarr = arr_history_lookup(
        RADARR_URL,
        RADARR_API_KEY,
        torrent_hash,
        "Radarr",
    )

    if radarr:
        return radarr

    sonarr = arr_history_lookup(
        SONARR_URL,
        SONARR_API_KEY,
        torrent_hash,
        "Sonarr",
    )

    if sonarr:
        return sonarr

    return {
        "source": None,
        "title": None,
        "indexer": None,
        "tracker_url": None,
        "arr_item_url": None,
        "item_id": None,
        "event_type": None,
    }


def wait_for_release_context(
    torrent_hash: str,
) -> dict[str, Any] | None:
    """
    Attend que Radarr ou Sonarr expose l'événement "grabbed"
    contenant l'URL de la page du torrent.

    Une tentative toutes les 5 secondes pendant 1 minute.
    Aucune notification Discord n'est envoyée sans tracker_url.
    """

    started_at = time.time()
    attempt = 0

    while True:
        attempt += 1

        release = get_release_context(
            torrent_hash
        )

        if release.get("tracker_url"):
            log.info(
                "[%s] URL du torrent récupérée via %s.",
                torrent_hash,
                release.get("source") or "Arr",
            )
            return release

        elapsed = time.time() - started_at

        if elapsed >= RELEASE_CONTEXT_TIMEOUT:
            log.warning(
                "[%s] URL du torrent introuvable après %ss "
                "(%d tentative(s)). Notification Discord annulée.",
                torrent_hash,
                RELEASE_CONTEXT_TIMEOUT,
                attempt,
            )
            return None

        remaining = max(
            0,
            RELEASE_CONTEXT_TIMEOUT - int(elapsed),
        )

        log.info(
            "[%s] URL du torrent pas encore disponible "
            "(tentative %d, nouvelle tentative dans %ss, "
            "%ss restantes).",
            torrent_hash,
            attempt,
            RELEASE_CONTEXT_RETRY_SECONDS,
            remaining,
        )

        time.sleep(
            RELEASE_CONTEXT_RETRY_SECONDS
        )


def build_qbittorrent_url(
    torrent_hash: str,
) -> str:

    return f"{QB_HOST}/#torrent={torrent_hash}"


async def _send_discord_bot_message(
    *,
    embeds: list[dict[str, Any]],
    torrent_hash: str,
    release: dict[str, Any],
) -> None:
    if discord_bot is None or not discord_bot.is_ready():
        raise RuntimeError("Bot Discord non prêt.")

    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID non configuré.")

    channel = discord_bot.get_channel(int(DISCORD_CHANNEL_ID))
    if channel is None:
        channel = await discord_bot.fetch_channel(int(DISCORD_CHANNEL_ID))

    embed_objects = [discord.Embed.from_dict(embed) for embed in embeds]
    view = ForcedFRView(torrent_hash, release)
    await channel.send(embeds=embed_objects, view=view)


def send_discord_message(
    *,
    embeds: list[dict[str, Any]],
    torrent_hash: str | None = None,
    release: dict[str, Any] | None = None,
) -> None:
    """Envoie la notification via le bot pour permettre les boutons interactifs."""
    if discord_bot is not None and MAIN_EVENT_LOOP is not None and torrent_hash and release:
        try:
            future = asyncio.run_coroutine_threadsafe(
                _send_discord_bot_message(
                    embeds=embeds,
                    torrent_hash=torrent_hash,
                    release=release,
                ),
                MAIN_EVENT_LOOP,
            )
            future.result(timeout=15)
            log.info("Notification Discord envoyée via le bot.")
            return
        except Exception:
            log.exception("Impossible d'envoyer la notification via le bot Discord.")
            return

    log.warning("Bot Discord indisponible : notification interactive non envoyée.")


class ForcedFRView(discord.ui.View if discord else object):
    def __init__(self, torrent_hash: str, release: dict[str, Any]) -> None:
        if discord is None:
            return
        super().__init__(timeout=None)

        tracker_url = release.get("tracker_url")
        arr_item_url = release.get("arr_item_url")
        source = release.get("source")

        if tracker_url:
            self.add_item(discord.ui.Button(
                label="🌐 Voir le torrent",
                style=discord.ButtonStyle.link,
                url=tracker_url,
            ))

        self.add_item(discord.ui.Button(
            label="🖥️ Ouvrir qBittorrent",
            style=discord.ButtonStyle.link,
            url=build_qbittorrent_url(torrent_hash),
        ))

        if arr_item_url:
            label = "🎬 Ouvrir Radarr" if source == "Radarr" else "📺 Ouvrir Sonarr"
            self.add_item(discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.link,
                url=arr_item_url,
            ))

        self.add_item(discord.ui.Button(
            label="▶️ Continuer le téléchargement",
            style=discord.ButtonStyle.success,
            custom_id=f"forcedfr:resume:{torrent_hash}",
        ))

        self.add_item(discord.ui.Button(
            label="⏸️ Laisser en pause",
            style=discord.ButtonStyle.secondary,
            custom_id=f"forcedfr:pause:{torrent_hash}",
        ))


def build_disabled_decision_view(message: Any) -> Any:
    """
    Reconstruit les boutons après une décision.

    Les liens restent actifs. Seuls les boutons interactifs
    Continuer / Laisser en pause sont désactivés.
    """
    if discord is None:
        return None

    view = discord.ui.View(timeout=None)

    for row in getattr(message, "components", []):
        for component in getattr(row, "children", []):
            custom_id = getattr(component, "custom_id", None)
            url = getattr(component, "url", None)

            disabled = bool(
                custom_id
                and str(custom_id).startswith("forcedfr:")
            )

            view.add_item(
                discord.ui.Button(
                    label=getattr(component, "label", None),
                    style=getattr(
                        component,
                        "style",
                        discord.ButtonStyle.secondary,
                    ),
                    custom_id=custom_id,
                    url=url,
                    emoji=getattr(component, "emoji", None),
                    disabled=disabled,
                )
            )

    return view


def build_discord_bot() -> Any:
    if discord is None:
        return None

    intents = discord.Intents.none()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready() -> None:
        log.info("Bot Discord connecté : %s", bot.user)
        if DISCORD_CHANNEL_ID:
            log.info("Salon Discord configuré : %s", DISCORD_CHANNEL_ID)

    @bot.event
    async def on_interaction(interaction: Any) -> None:
        try:
            data = interaction.data or {}
            custom_id = str(data.get("custom_id", ""))

            if not custom_id.startswith("forcedfr:"):
                return

            parts = custom_id.split(":", 2)
            if len(parts) != 3:
                return

            _, action, torrent_hash = parts

            await interaction.response.defer(ephemeral=True)

            if torrent_hash in resolved_discord_actions:
                await interaction.followup.send(
                    "ℹ️ Une décision a déjà été prise pour ce torrent.",
                    ephemeral=True,
                )
                return

            # Vérifie que le torrent existe toujours avant toute action.
            await asyncio.to_thread(get_torrent, torrent_hash)

            if action == "resume":
                await asyncio.to_thread(start_torrent, torrent_hash)
                response_message = (
                    "▶️ Le téléchargement a été repris dans qBittorrent."
                )
                log.info(
                    "[%s] Reprise demandée depuis Discord par %s.",
                    torrent_hash,
                    interaction.user,
                )

            elif action == "pause":
                await asyncio.to_thread(stop_torrent, torrent_hash)
                response_message = (
                    "⏸️ Le téléchargement reste en pause dans qBittorrent."
                )
                log.info(
                    "[%s] Maintien en pause demandé depuis Discord par %s.",
                    torrent_hash,
                    interaction.user,
                )

            else:
                await interaction.followup.send(
                    "⚠️ Action Discord inconnue.",
                    ephemeral=True,
                )
                return

            resolved_discord_actions.add(torrent_hash)

            # Désactive les boutons de décision tout en conservant
            # les liens vers torrent / qBittorrent / Radarr-Sonarr.
            if interaction.message is not None:
                await interaction.message.edit(
                    view=build_disabled_decision_view(
                        interaction.message
                    )
                )

            await interaction.followup.send(
                response_message,
                ephemeral=True,
            )

            log.info(
                "[%s] Décision Discord enregistrée : %s.",
                torrent_hash,
                action,
            )

        except HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "⚠️ Ce torrent n'existe plus dans qBittorrent.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "⚠️ Ce torrent n'existe plus dans qBittorrent.",
                    ephemeral=True,
                )

        except requests.RequestException:
            log.exception(
                "Erreur qBittorrent lors d'une interaction Discord."
            )
            await interaction.followup.send(
                "⚠️ Impossible de communiquer avec qBittorrent.",
                ephemeral=True,
            )

        except Exception as exc:
            log.exception(
                "Erreur lors d'une interaction Discord : %s",
                exc,
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "⚠️ Impossible d'exécuter cette action.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "⚠️ Impossible d'exécuter cette action.",
                        ephemeral=True,
                    )
            except Exception:
                pass

    return bot


def build_release_fields(
    torrent: dict[str, Any],
    release: dict[str, Any],
) -> list[dict[str, Any]]:

    fields = [
        {
            "name": "Torrent",
            "value": (
                f"`{torrent.get('name', 'Nom inconnu')}`"
            )[:1024],
            "inline": False,
        },
        {
            "name": "Progression",
            "value": (
                f"{float(torrent.get('progress', 0)) * 100:.2f}%"
            ),
            "inline": True,
        },
    ]

    if release.get("source"):

        fields.append(
            {
                "name": "Source",
                "value": release["source"],
                "inline": True,
            }
        )

    if release.get("indexer"):

        fields.append(
            {
                "name": "Indexeur",
                "value": str(
                    release["indexer"]
                )[:1024],
                "inline": True,
            }
        )

    return fields


def notify_no_french_forced(
    torrent: dict[str, Any],
) -> None:

    torrent_hash = str(
        torrent.get("hash", "")
    )

    release = wait_for_release_context(
        torrent_hash
    )

    # Aucun message Discord sans URL de la page du torrent.
    if release is None:
        log.warning(
            "[%s] Notification Discord non envoyée : "
            "URL du torrent indisponible.",
            torrent_hash,
        )
        return

    fields = build_release_fields(
        torrent,
        release,
    )

    fields.extend(
        [
            {
                "name": "Action effectuée",
                "value": (
                    "⏸️ Le téléchargement a été mis en pause automatiquement."
                ),
                "inline": False,
            },
            {
                "name": "Que faire ?",
                "value": (
                    "Vérifie le torrent puis décide dans qBittorrent "
                    "si tu souhaites reprendre ou supprimer le téléchargement."
                ),
                "inline": False,
            },
        ]
    )

    send_discord_message(
        embeds=[
            {
                "title": "🚨 Aucune piste FR Forced détectée",
                "description": (
                    "ForcedFR a pu analyser le fichier, mais aucune piste "
                    "de sous-titres français forcés n'a été trouvée."
                ),
                "color": 15158332,
                "fields": fields,
                "footer": {
                    "text": (
                        "ForcedFR • Vérification manuelle recommandée"
                    ),
                },
            }
        ],
        torrent_hash=torrent_hash,
        release=release,
    )


def notify_analysis_error(
    torrent: dict[str, Any],
    error: str,
) -> None:

    torrent_hash = str(
        torrent.get("hash", "")
    )

    release = wait_for_release_context(
        torrent_hash
    )

    # Aucun message Discord sans URL de la page du torrent.
    if release is None:
        log.warning(
            "[%s] Notification Discord non envoyée : "
            "URL du torrent indisponible.",
            torrent_hash,
        )
        return

    fields = build_release_fields(
        torrent,
        release,
    )

    error_text = str(
        error
    ).strip() or "Erreur inconnue"

    fields.extend(
        [
            {
                "name": "Erreur d'analyse",
                "value": f"```{error_text[:1000]}```",
                "inline": False,
            },
            {
                "name": "Action effectuée",
                "value": (
                    "▶️ Le téléchargement continue automatiquement."
                ),
                "inline": False,
            },
            {
                "name": "Que faire ?",
                "value": (
                    "ForcedFR n'a pas pu déterminer le résultat de manière fiable. "
                    "Une vérification manuelle est recommandée."
                ),
                "inline": False,
            },
        ]
    )

    send_discord_message(
        embeds=[
            {
                "title": "⚠️ Erreur pendant l'analyse ForcedFR",
                "description": (
                    "Le torrent n'a pas été bloqué afin d'éviter "
                    "une interruption injustifiée du téléchargement."
                ),
                "color": 16776960,
                "fields": fields,
                "footer": {
                    "text": (
                        "ForcedFR • Téléchargement laissé en cours"
                    ),
                },
            }
        ],
        torrent_hash=torrent_hash,
        release=release,
    )


# ============================================================
# RECHERCHE DES MKV
# ============================================================

def find_mkv_files(
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    mkvs = []

    for file in files:

        name = str(
            file.get(
                "name",
                "",
            )
        )

        if name.lower().endswith(".mkv"):
            mkvs.append(file)

    # Le plus gros MKV en premier
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
    # content_path pointe directement vers le MKV
    # --------------------------------------------------------

    if content_path.suffix.lower() == ".mkv":
        return content_path

    # --------------------------------------------------------
    # CAS 2
    # save_path + chemin relatif du fichier
    # --------------------------------------------------------

    candidate = save_path / file_name

    if candidate.exists():
        return candidate

    # --------------------------------------------------------
    # CAS 3
    # content_path + nom du MKV
    # --------------------------------------------------------

    candidate = content_path / file_name.name

    if candidate.exists():
        return candidate

    # --------------------------------------------------------
    # CAS 4
    # Recherche récursive dans le dossier
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

    return save_path / file_name


# ============================================================
# ERREUR TEMPORAIRE FFPROBE
# ============================================================

class IncompleteMKVError(RuntimeError):
    """Le MKV est encore incomplet et doit être réessayé."""


# ============================================================
# FFPROBE
# ============================================================

def run_ffprobe(
    file_path: Path,
) -> dict[str, Any]:

    if not file_path.exists():
        raise FileNotFoundError(
            f"Fichier introuvable : {file_path}"
        )

    if not file_path.is_file():
        raise RuntimeError(
            f"Le chemin n'est pas un fichier : {file_path}"
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

        # Erreurs normales pendant le téléchargement :
        # le fichier existe déjà mais son en-tête MKV n'est
        # pas encore entièrement disponible.
        temporary_markers = (
            "EBML header parsing failed",
            "Invalid data found when processing input",
            "invalid as first byte of an EBML number",
            "End of file",
            "Input/output error",
        )

        if any(
            marker.lower() in error.lower()
            for marker in temporary_markers
        ):
            raise IncompleteMKVError(
                error or "MKV encore incomplet."
            )

        # Toute autre erreur est considérée comme une
        # vraie erreur d'analyse.
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

    return {
        "forced_french": bool(
            forced_french
        ),
        "subtitles": subtitles,
        "forced_tracks": forced_french,
    }


# ============================================================
# ANALYSE D'UN TORRENT
# ============================================================

def analyze_torrent(
    torrent_hash: str,
) -> dict[str, Any]:

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

        # Le fichier peut ne pas encore exister.
        if not path.exists():
            raise FileNotFoundError(
                f"MKV pas encore disponible : {path}"
            )

        if not path.is_file():
            raise RuntimeError(
                f"Le chemin du MKV est un dossier : {path}"
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
                "size": mkv.get("size"),
                "progress": mkv.get(
                    "progress",
                    0,
                ),
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
# ANALYSE AVEC PRIORITÉ TEMPORAIRE
# ============================================================

def analyze_with_temporary_priority(
    torrent_hash: str,
) -> dict[str, Any]:

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

        # ----------------------------------------------------
        # Activation IMMÉDIATE
        # ----------------------------------------------------

        if not initial_priority:

            log.info(
                "[%s] Activation immédiate "
                "f_l_piece_prio.",
                torrent_hash,
            )

            toggle_first_last_piece_priority(
                torrent_hash
            )

            priority_changed = True

        # ----------------------------------------------------
        # Analyse
        # ----------------------------------------------------

        return analyze_torrent(
            torrent_hash
        )

    finally:

        # ----------------------------------------------------
        # Toujours désactiver après analyse
        # ----------------------------------------------------

        if priority_changed:

            log.info(
                "[%s] Désactivation f_l_piece_prio.",
                torrent_hash,
            )

            try:

                toggle_first_last_piece_priority(
                    torrent_hash
                )

            except Exception:

                log.exception(
                    "[%s] Impossible de désactiver "
                    "f_l_piece_prio.",
                    torrent_hash,
                )


# ============================================================
# TRAITEMENT NOUVEAU TORRENT
# ============================================================

def process_new_torrent(
    torrent_hash: str,
) -> None:

    if torrent_hash in processing_torrents:
        return

    processing_torrents.add(
        torrent_hash
    )

    log.info(
        "========================================"
    )

    log.info(
        "[%s] 🆕 Nouveau torrent détecté.",
        torrent_hash,
    )

    started_at = time.time()

    # --------------------------------------------------------
    # ACTIVER LA PRIORITÉ IMMÉDIATEMENT
    # --------------------------------------------------------

    priority_enabled = False

    try:

        torrent = get_torrent(
            torrent_hash
        )

        if not torrent.get(
            "f_l_piece_prio",
            False,
        ):

            toggle_first_last_piece_priority(
                torrent_hash
            )

            priority_enabled = True

            log.info(
                "[%s] ⚡ Priorité premières/dernières "
                "pièces activée.",
                torrent_hash,
            )

        # ----------------------------------------------------
        # Boucle d'analyse
        # ----------------------------------------------------

        while True:

            elapsed = (
                time.time()
                - started_at
            )

            if elapsed >= ANALYSIS_TIMEOUT:

                log.error(
                    "[%s] ⚠️ ERREUR RÉELLE : timeout d'analyse "
                    "après %ss. Le téléchargement continue.",
                    torrent_hash,
                    ANALYSIS_TIMEOUT,
                )

                notify_analysis_error(
                    torrent,
                    f"Timeout après {ANALYSIS_TIMEOUT} secondes.",
                )

                # IMPORTANT : ne pas mettre le torrent en pause.
                return

            try:

                torrent = get_torrent(
                    torrent_hash
                )

                log.info(
                    "[%s] %s — %.2f%%",
                    torrent_hash,
                    torrent.get(
                        "name",
                        "",
                    ),
                    float(
                        torrent.get(
                            "progress",
                            0,
                        )
                    ) * 100,
                )

                # ------------------------------------------------
                # Tentative immédiate de ffprobe
                # ------------------------------------------------

                result = analyze_torrent(
                    torrent_hash
                )

                if result[
                    "forced_french"
                ]:

                    log.info(
                        "[%s] ✅ FR Forced détecté.",
                        torrent_hash,
                    )

                    return

                # ------------------------------------------------
                # MKV lisible mais pas de FR Forced
                # ------------------------------------------------

                log.warning(
                    "[%s] ❌ Aucune piste "
                    "FR Forced détectée.",
                    torrent_hash,
                )

                stop_torrent(
                    torrent_hash
                )

                notify_no_french_forced(
                    torrent
                )

                return

            except FileNotFoundError:

                log.info(
                    "[%s] MKV pas encore disponible. "
                    "Nouvelle tentative.",
                    torrent_hash,
                )

            except IncompleteMKVError as exc:

                log.info(
                    "[%s] MKV encore incomplet : %s",
                    torrent_hash,
                    exc,
                )

            except subprocess.TimeoutExpired as exc:

                log.error(
                    "[%s] ⚠️ ERREUR RÉELLE : ffprobe timeout. "
                    "Le téléchargement continue.",
                    torrent_hash,
                )

                notify_analysis_error(
                    torrent,
                    "ffprobe a dépassé son délai de 60 secondes.",
                )

                # Le torrent est volontairement laissé en cours.
                return

            except RuntimeError as exc:

                log.error(
                    "[%s] ⚠️ ERREUR RÉELLE D'ANALYSE : %s",
                    torrent_hash,
                    exc,
                )

                notify_analysis_error(
                    torrent,
                    str(exc),
                )

                # Le torrent est volontairement laissé en cours.
                return

            except Exception as exc:

                log.exception(
                    "[%s] ⚠️ ERREUR RÉELLE INATTENDUE : %s "
                    "Le téléchargement continue.",
                    torrent_hash,
                    exc,
                )

                notify_analysis_error(
                    torrent,
                    str(exc),
                )

                # Le torrent est volontairement laissé en cours.
                return

            time.sleep(ANALYSIS_POLL_SECONDS)

    finally:

        # --------------------------------------------------------
        # TOUJOURS désactiver la priorité
        # --------------------------------------------------------

        if priority_enabled:

            try:

                # Vérification avant toggle pour éviter
                # de changer un état déjà modifié ailleurs.

                torrent = get_torrent(
                    torrent_hash
                )

                if torrent.get(
                    "f_l_piece_prio",
                    False,
                ):

                    toggle_first_last_piece_priority(
                        torrent_hash
                    )

                    log.info(
                        "[%s] 📴 f_l_piece_prio désactivé.",
                        torrent_hash,
                    )

            except Exception:

                log.exception(
                    "[%s] Impossible de désactiver "
                    "f_l_piece_prio.",
                    torrent_hash,
                )

        processing_torrents.discard(
            torrent_hash
        )

        log.info(
            "[%s] Fin du traitement.",
            torrent_hash,
        )

        log.info(
            "========================================"
        )


# ============================================================
# SURVEILLANCE qBITTORRENT
# ============================================================

async def monitor_qbittorrent() -> None:

    global previous_torrents

    # --------------------------------------------------------
    # Stabilisation de qBittorrent au démarrage
    # --------------------------------------------------------
    # Après un redémarrage du serveur, qBittorrent peut restaurer
    # ses torrents progressivement. On attend donc que la liste
    # soit inchangée pendant 3 vérifications espacées de 5 secondes
    # avant d'enregistrer la référence initiale.

    log.info(
        "Attente de stabilisation de qBittorrent..."
    )

    stable_snapshot: set[str] | None = None
    stable_checks = 0

    while stable_checks < STARTUP_STABLE_CHECKS_REQUIRED:

        try:

            torrents = get_torrents()

            current_snapshot = {
                torrent["hash"]
                for torrent in torrents
                if torrent.get("hash")
            }

            if stable_snapshot is None:

                stable_snapshot = current_snapshot
                stable_checks = 0

                log.info(
                    "Liste détectée : %d torrent(s). "
                    "Vérification de stabilité en cours...",
                    len(current_snapshot),
                )

            elif current_snapshot == stable_snapshot:

                stable_checks += 1

                log.info(
                    "Liste stable (%d/%d) : %d torrent(s).",
                    stable_checks,
                    STARTUP_STABLE_CHECKS_REQUIRED,
                    len(current_snapshot),
                )

            else:

                log.info(
                    "Liste modifiée (%d → %d torrents). "
                    "Nouvelle période de stabilisation.",
                    len(stable_snapshot),
                    len(current_snapshot),
                )

                stable_snapshot = current_snapshot
                stable_checks = 0

        except Exception:

            log.warning(
                "qBittorrent pas encore prêt. "
                "Nouvelle tentative dans %ss.",
                STARTUP_STABILITY_CHECK_SECONDS,
            )

            stable_snapshot = None
            stable_checks = 0

        if stable_checks < STARTUP_STABLE_CHECKS_REQUIRED:
            await asyncio.sleep(
                STARTUP_STABILITY_CHECK_SECONDS
            )

    previous_torrents = stable_snapshot or set()

    log.info(
        "qBittorrent stabilisé. %d torrent(s) présents au démarrage "
        "et ignorés.",
        len(previous_torrents),
    )

    log.info(
        "Surveillance qBittorrent démarrée "
        "(intervalle : %ss).",
        POLL_SECONDS,
    )

    # --------------------------------------------------------
    # Surveillance permanente
    # --------------------------------------------------------

    while True:

        try:

            torrents = get_torrents()

            current_torrents = {
                torrent["hash"]
                for torrent in torrents
                if torrent.get("hash")
            }

            new_torrents = (
                current_torrents
                - previous_torrents
            )

            if new_torrents:

                log.info(
                    "🔎 %d nouveau(x) torrent(s) détecté(s).",
                    len(new_torrents),
                )

                for torrent_hash in new_torrents:

                    asyncio.create_task(
                        asyncio.to_thread(
                            process_new_torrent,
                            torrent_hash,
                        )
                    )

            previous_torrents = (
                current_torrents
            )

        except Exception:

            log.exception(
                "Erreur dans la surveillance "
                "qBittorrent."
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup() -> None:

    global MAIN_EVENT_LOOP, discord_bot
    MAIN_EVENT_LOOP = asyncio.get_running_loop()

    log.info(
        "========================================"
    )

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

    else:

        try:

            qb_login()

        except Exception:

            log.exception(
                "Connexion initiale qBittorrent échouée."
            )

    if not DISCORD_BOT_TOKEN:
        log.warning("DISCORD_BOT_TOKEN non configuré : les boutons interactifs Discord sont indisponibles.")
    elif not DISCORD_CHANNEL_ID:
        log.warning("DISCORD_CHANNEL_ID non configuré : les notifications Discord sont indisponibles.")
    elif discord is None:
        log.error("Le module discord.py est absent. Ajoute discord.py aux dépendances du conteneur.")
    else:
        discord_bot = build_discord_bot()
        asyncio.create_task(discord_bot.start(DISCORD_BOT_TOKEN))

    asyncio.create_task(
        monitor_qbittorrent()
    )

    log.info(
        "========================================"
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown() -> None:
    global discord_bot
    if discord_bot is not None:
        try:
            await discord_bot.close()
        except Exception:
            log.exception("Erreur lors de l'arrêt du bot Discord.")


# ============================================================
# API
# ============================================================

@app.get("/health")
def health() -> dict[str, Any]:

    return {
        "status": "ok",
        "version": "2.0.0",
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
            detail=f"Erreur qBittorrent : {exc}",
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
            detail=f"Erreur : {exc}",
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
            detail="ffprobe a dépassé 60 secondes.",
        )

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=f"Analyse impossible : {exc}",
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
            detail=f"Erreur qBittorrent : {exc}",
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
            detail=f"Erreur qBittorrent : {exc}",
        )

# ============================================================
# V2.0 — INTERFACE WEB ET SCAN DE BIBLIOTHÈQUE
# ============================================================

LIBRARY_FILMS_PATH = Path(
    os.getenv("LIBRARY_FILMS_PATH", "/data/Films")
)

LIBRARY_SERIES_PATH = Path(
    os.getenv("LIBRARY_SERIES_PATH", "/data/Séries")
)

SERVICE_STARTED_AT = time.time()

scan_lock = threading.Lock()
scan_state: dict[str, Any] = {
    "running": False,
    "requested_scope": None,
    "started_at": None,
    "finished_at": None,
    "current_file": None,
    "total_files": 0,
    "processed_files": 0,
    "files_with_forced_fr": 0,
    "files_without_forced_fr": 0,
    "errors": 0,
    "results": [],
    "last_error": None,
}


def _scan_reset(scope: str) -> None:
    scan_state.update(
        {
            "running": True,
            "requested_scope": scope,
            "started_at": time.time(),
            "finished_at": None,
            "current_file": None,
            "total_files": 0,
            "processed_files": 0,
            "files_with_forced_fr": 0,
            "files_without_forced_fr": 0,
            "errors": 0,
            "results": [],
            "last_error": None,
        }
    )


def _scan_roots(scope: str) -> list[Path]:
    if scope == "films":
        return [LIBRARY_FILMS_PATH]
    if scope == "series":
        return [LIBRARY_SERIES_PATH]
    if scope == "all":
        return [LIBRARY_FILMS_PATH, LIBRARY_SERIES_PATH]
    raise ValueError("Scope de scan invalide.")


def _library_kind(file_path: Path) -> str:
    try:
        if file_path.is_relative_to(LIBRARY_FILMS_PATH):
            return "film"
        if file_path.is_relative_to(LIBRARY_SERIES_PATH):
            return "serie"
    except AttributeError:
        path_text = str(file_path)
        if path_text.startswith(str(LIBRARY_FILMS_PATH)):
            return "film"
        if path_text.startswith(str(LIBRARY_SERIES_PATH)):
            return "serie"
    return "inconnu"


def _relative_library_path(file_path: Path) -> str:
    for root in (LIBRARY_FILMS_PATH, LIBRARY_SERIES_PATH):
        try:
            return str(file_path.relative_to(root))
        except ValueError:
            pass
    return str(file_path)


def run_library_scan(scope: str) -> None:
    if not scan_lock.acquire(blocking=False):
        log.warning("Un scan de bibliothèque est déjà en cours.")
        return

    try:
        _scan_reset(scope)
        roots = _scan_roots(scope)

        files: list[Path] = []

        for root in roots:
            if not root.exists():
                log.warning("Bibliothèque introuvable : %s", root)
                continue

            files.extend(
                sorted(
                    path
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".mkv"
                )
            )

        scan_state["total_files"] = len(files)

        log.info(
            "[SCAN] Démarrage du scan '%s' : %d MKV détecté(s).",
            scope,
            len(files),
        )

        for file_path in files:
            scan_state["current_file"] = str(file_path)

            result: dict[str, Any] = {
                "path": str(file_path),
                "relative_path": _relative_library_path(file_path),
                "type": _library_kind(file_path),
                "forced_french": False,
                "status": "ok",
                "error": None,
                "forced_tracks": [],
                "subtitles": [],
            }

            try:
                probe = run_ffprobe(file_path)
                detection = detect_french_forced(probe)

                result["forced_french"] = bool(
                    detection.get("forced_french")
                )
                result["forced_tracks"] = detection.get(
                    "forced_tracks", []
                )
                result["subtitles"] = detection.get(
                    "subtitles", []
                )

                if result["forced_french"]:
                    scan_state["files_with_forced_fr"] += 1
                else:
                    scan_state["files_without_forced_fr"] += 1
                    scan_state["results"].append(result)

            except Exception as exc:
                result["status"] = "error"
                result["error"] = str(exc)
                scan_state["errors"] += 1
                scan_state["results"].append(result)

                log.warning(
                    "[SCAN] Erreur sur %s : %s",
                    file_path,
                    exc,
                )

            finally:
                scan_state["processed_files"] += 1

        log.info(
            "[SCAN] Terminé : %d analysé(s), %d sans FR Forced, %d erreur(s).",
            scan_state["processed_files"],
            scan_state["files_without_forced_fr"],
            scan_state["errors"],
        )

    except Exception as exc:
        scan_state["last_error"] = str(exc)
        log.exception("[SCAN] Erreur générale.")

    finally:
        scan_state["running"] = False
        scan_state["current_file"] = None
        scan_state["finished_at"] = time.time()
        scan_lock.release()


def start_library_scan(scope: str) -> dict[str, Any]:
    if scan_state.get("running"):
        raise HTTPException(
            status_code=409,
            detail="Un scan de bibliothèque est déjà en cours.",
        )

    thread = threading.Thread(
        target=run_library_scan,
        args=(scope,),
        daemon=True,
        name=f"forcedfr-scan-{scope}",
    )
    thread.start()

    return {
        "ok": True,
        "message": f"Scan '{scope}' démarré.",
        "scope": scope,
    }


def _discord_status() -> str:
    if discord_bot is None:
        return "disabled"
    try:
        return "connected" if discord_bot.is_ready() else "connecting"
    except Exception:
        return "unknown"


def _qbittorrent_status() -> tuple[str, int | None]:
    try:
        count = len(get_torrents())
        return "connected", count
    except Exception:
        return "error", None


@app.get("/", response_class=HTMLResponse)
def web_dashboard() -> str:
    return """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ForcedFR v2.0</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,Arial,sans-serif}
*{box-sizing:border-box} body{margin:0;background:#0d1117;color:#e6edf3}
main{max-width:1180px;margin:auto;padding:28px}
h1{margin:0;font-size:2rem}.sub{color:#8b949e;margin:6px 0 26px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.card{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:18px}
.label{color:#8b949e;font-size:.85rem}.value{font-size:1.25rem;font-weight:700;margin-top:8px}
.ok{color:#3fb950}.warn{color:#d29922}.bad{color:#f85149}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin:24px 0}
button{border:0;border-radius:9px;padding:12px 16px;font-weight:700;cursor:pointer;background:#238636;color:white}
button.secondary{background:#30363d}button:hover{filter:brightness(1.12)}
section{margin-top:24px}.progress{height:12px;background:#21262d;border-radius:999px;overflow:hidden}
.progress>div{height:100%;background:#238636;width:0%;transition:.3s}
table{width:100%;border-collapse:collapse;background:#161b22;border-radius:14px;overflow:hidden}
th,td{text-align:left;padding:12px;border-bottom:1px solid #30363d;font-size:.9rem}
.path{word-break:break-all}.small{font-size:.82rem;color:#8b949e}
</style>
</head>
<body>
<main>
<h1>ForcedFR <span class="small">v2.0.0</span></h1>
<p class="sub">Surveillance des torrents et contrôle de la bibliothèque média.</p>

<div class="grid">
 <div class="card"><div class="label">ForcedFR</div><div class="value ok" id="service">Chargement…</div></div>
 <div class="card"><div class="label">qBittorrent</div><div class="value" id="qb">Chargement…</div></div>
 <div class="card"><div class="label">Discord</div><div class="value" id="discord">Chargement…</div></div>
 <div class="card"><div class="label">Torrents en cours d'analyse</div><div class="value" id="processing">0</div></div>
</div>

<section>
<h2>Scanner la bibliothèque</h2>
<div class="actions">
<button onclick="scan('films')">🎬 Scanner les films</button>
<button onclick="scan('series')">📺 Scanner les séries</button>
<button class="secondary" onclick="scan('all')">🔍 Scanner toute la bibliothèque</button>
</div>
<div class="card">
 <div class="label" id="scanLabel">Aucun scan en cours.</div>
 <div class="progress"><div id="bar"></div></div>
 <div class="small" id="scanStats" style="margin-top:10px"></div>
</div>
</section>

<section>
<h2>Fichiers nécessitant une vérification</h2>
<p class="small">Les fichiers sans piste française forcée et les erreurs d'analyse apparaissent ici.</p>
<table>
<thead><tr><th>Type</th><th>Fichier</th><th>État</th></tr></thead>
<tbody id="results"><tr><td colspan="3">Aucun résultat pour le moment.</td></tr></tbody>
</table>
</section>
</main>

<script>
async function api(url,opt={}){const r=await fetch(url,opt);const d=await r.json();if(!r.ok)throw new Error(d.detail||'Erreur API');return d}
async function scan(scope){try{await api('/scan/'+scope,{method:'POST'});refresh()}catch(e){alert(e.message)}}
function esc(v){return String(v||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
async function refresh(){
 try{
  const s=await api('/status');
  document.getElementById('service').textContent=s.status==='ok'?'● En ligne':'● Erreur';
  document.getElementById('qb').textContent=s.qbittorrent.status==='connected'?'● Connecté ('+(s.qbittorrent.torrents||0)+')':'● Indisponible';
  document.getElementById('discord').textContent=s.discord.status;
  document.getElementById('processing').textContent=s.monitoring.processing_torrents;
  const x=await api('/scan/status');
  const pct=x.total_files?Math.round((x.processed_files/x.total_files)*100):0;
  document.getElementById('bar').style.width=pct+'%';
  document.getElementById('scanLabel').textContent=x.running?('Scan en cours : '+pct+'%'+(x.current_file?' — '+x.current_file:'' )):(x.finished_at?'Dernier scan terminé.':'Aucun scan en cours.');
  document.getElementById('scanStats').textContent='Analysés : '+x.processed_files+'/'+x.total_files+' • Avec FR Forced : '+x.files_with_forced_fr+' • Sans FR Forced : '+x.files_without_forced_fr+' • Erreurs : '+x.errors;
  const r=await api('/scan/results');
  const rows=r.results||[];
  document.getElementById('results').innerHTML=rows.length?rows.map(a=>'<tr><td>'+esc(a.type)+'</td><td class="path">'+esc(a.relative_path)+'</td><td class="'+(a.status==='error'?'bad':'warn')+'">'+(a.status==='error'?'Erreur : '+esc(a.error):'Pas de FR Forced')+'</td></tr>').join(''):'<tr><td colspan="3">Aucun fichier problématique détecté.</td></tr>';
 }catch(e){console.error(e)}
}
refresh();setInterval(refresh,3000);
</script>
</body></html>"""


@app.get("/status")
def status() -> dict[str, Any]:
    qb_status, qb_count = _qbittorrent_status()

    return {
        "status": "ok",
        "version": "2.0.0",
        "uptime_seconds": int(time.time() - SERVICE_STARTED_AT),
        "qbittorrent": {
            "status": qb_status,
            "host": QB_HOST,
            "torrents": qb_count,
        },
        "discord": {
            "status": _discord_status(),
            "channel_id": DISCORD_CHANNEL_ID or None,
        },
        "monitoring": {
            "enabled": True,
            "poll_seconds": POLL_SECONDS,
            "known_torrents": len(previous_torrents),
            "processing_torrents": len(processing_torrents),
        },
        "scan": {
            "running": scan_state.get("running", False),
            "scope": scan_state.get("requested_scope"),
            "processed_files": scan_state.get("processed_files", 0),
            "total_files": scan_state.get("total_files", 0),
        },
        "libraries": {
            "films": str(LIBRARY_FILMS_PATH),
            "series": str(LIBRARY_SERIES_PATH),
        },
    }


@app.get("/scan/status")
def scan_status() -> dict[str, Any]:
    return dict(scan_state)


@app.get("/scan/results")
def scan_results() -> dict[str, Any]:
    return {
        "running": scan_state.get("running", False),
        "results": list(scan_state.get("results", [])),
    }


@app.post("/scan/films")
def scan_films() -> dict[str, Any]:
    return start_library_scan("films")


@app.post("/scan/series")
def scan_series() -> dict[str, Any]:
    return start_library_scan("series")


@app.post("/scan/all")
def scan_all() -> dict[str, Any]:
    return start_library_scan("all")
