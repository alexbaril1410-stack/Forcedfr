# ForcedFR

ForcedFR est un service Docker destiné à surveiller les téléchargements de films et séries et à détecter rapidement la présence d'une piste de sous-titres français forcés.

L'objectif est simple :

> Éviter de télécharger plusieurs Go d'une release qui ne contient pas les sous-titres français forcés recherchés.

Le projet est conçu pour fonctionner avec :

- Radarr
- Sonarr
- qBittorrent
- Discord
- Docker / Docker Compose
- ZimaOS

## Fonctionnement prévu

```text
Radarr / Sonarr
       |
       v
  qBittorrent
       |
       v
    ForcedFR
       |
       +---- FR Forced trouvé ----> téléchargement normal
       |
       +---- FR Forced absent ----> pause
                                      |
                                      v
                                   Discord
                                  /       \
                                 /         \
                         Continuer       Annuler
                            |                |
                            v                v
                         Resume       Suppression
