# ForcedFR

Détecte, et à terme génère, une piste de sous-titres français forcés pour
les films/séries ajoutés par Sonarr/Radarr — sans jamais toucher au MKV
original.

## Installation

### Option recommandée : Docker (notamment sur ZimaOS / NAS)

Sur ZimaOS, `apt`/`yum` ne sont pas disponibles pour installer des paquets
système sur l'hôte — Docker est donc la façon propre d'obtenir Python +
ffmpeg sans toucher au système. C'était de toute façon la stack prévue au
départ.

```bash
docker compose build
```

Édite d'abord `docker-compose.yml` pour que les volumes pointent vers
l'emplacement réel de ta bibliothèque (`/DATA/movies`, `/DATA/tv`, ...).

### Option "image toute prête" : GitHub Container Registry (ghcr.io)

Utile si le build local sur ton NAS pose problème, ou si tu veux installer
ForcedFR via l'assistant d'installation de conteneurs de ZimaOS (qui attend
une image déjà construite sur un registre, pas un `Dockerfile` local).

**1. Pousse ce dossier sur un repo GitHub** (public ou privé) :

```bash
git init
git add .
git commit -m "ForcedFR"
git branch -M main
git remote add origin https://github.com/TON-PSEUDO/forcedfr.git
git push -u origin main
```

**2. GitHub construit et publie l'image automatiquement** grâce au workflow
`.github/workflows/docker-publish.yml` déjà inclus — rien à configurer,
il se déclenche à chaque `push` sur `main` et publie sur
`ghcr.io/TON-PSEUDO/forcedfr:latest`. Tu peux suivre sa progression dans
l'onglet **Actions** du repo GitHub.

**3. Rends le package public** (sinon il faudra te connecter au registre
depuis le NAS) : sur GitHub, va dans **ton profil → Packages → forcedfr →
Package settings → Change visibility → Public**.

**4. Sur le NAS**, remplace dans `docker-compose.yml` la ligne `build: .`
par :

```yaml
image: ghcr.io/TON-PSEUDO/forcedfr:latest
```

puis :

```bash
docker compose pull
```

Ou en `docker` classique, sans compose :

```bash
docker pull ghcr.io/TON-PSEUDO/forcedfr:latest
docker tag ghcr.io/TON-PSEUDO/forcedfr:latest forcedfr
```

Si tu préfères garder le package **privé**, il faudra te connecter une fois
depuis le NAS avec un token GitHub (Settings → Developer settings →
Personal access tokens, scope `read:packages`) :

```bash
docker login ghcr.io -u TON-PSEUDO
```

### Option alternative : Python local (Linux/macOS classique)

- Python 3.10+
- `ffmpeg` / `ffprobe` installés et dans le PATH

```bash
# Debian/Ubuntu
sudo apt install ffmpeg
pip install -r requirements.txt

# macOS
brew install ffmpeg
pip install -r requirements.txt
```

## V1.0 — Détection pure (aucun appel IA)

Cherche une piste forced FR déjà présente dans le MKV via ses métadonnées.

```bash
# Docker
docker compose run --rm forcedfr python main.py "/movies/Dune.Part.Three.2026/Dune.Part.Three.2026.mkv"

# Python local
python main.py "/movies/Dune Part Three/Dune.Part.Three.2026.mkv"

# Sortie JSON (utile pour brancher une API plus tard)
python main.py "/movies/.../film.mkv" --json
```

Exemple de sortie :

```
Fichier : /movies/Dune.Part.Three.2026.mkv
✓ Forced FR trouvé (méthode : metadata, piste #4)
Raison : Piste #4 taguée language=fra + forced=1

Pistes de sous-titres détectées :
  #2 | lang=eng | forced_flag=False | titre='English'
  #3 | lang=fra | forced_flag=False | titre='French'
  #4 | lang=fra | forced_flag=True | titre='French (Forced)'
```

Deux niveaux de détection :

1. **Niveau A — métadonnées** : `language=fra` + `disposition.forced=1`.
   Si trouvé → terminé, aucune analyse supplémentaire.
2. **Niveau B — nom de piste** : cherche des mots-clés (`forced`, `forcé`,
   `vf forced`, ...) dans le titre d'une piste française, en excluant les
   pistes ambiguës (SDH, hearing impaired, commentary...).

*(le niveau C initialement envisagé — analyse du contenu des sous-titres
existants — a été abandonné : jugé peu utile en pratique.)*

Si aucun niveau ne trouve de piste, on passe à l'audio.

## V1.2 — Analyse audio (détection des passages en langue étrangère)

1. On identifie la piste audio française (`audio/extractor.py`)
2. On l'extrait en WAV mono 16 kHz (`ffmpeg`)
3. On découpe cet audio en fenêtres de 12s et on fait détecter la langue
   de chaque fenêtre par un petit modèle Whisper local — CPU, pas de GPU
   requis (`audio/language_detector.py`, via `faster-whisper`)
4. Les fenêtres consécutives non-françaises et suffisamment fiables sont
   regroupées en "segments suspects"
5. Chaque segment suspect est extrait en clip audio séparé
   (`audio/pipeline.py`) — c'est ce clip, et seulement lui, qui sera
   envoyé à Gemini en V1.4, jamais le film entier

Ajoute simplement `faster-whisper` et `soundfile` (déjà dans `requirements.txt`,
donc rien à faire de plus si tu es passé par Docker ou `pip install -r requirements.txt`).

Le modèle Whisper `tiny` est téléchargé automatiquement au premier lancement
(~75 Mo, nécessite un accès internet la première fois). Pour plus de
précision au prix d'un peu plus de calcul, tu peux passer `model_size="base"`
ou `"small"`.

### Usage

```bash
# Docker
docker compose run --rm forcedfr python audio/pipeline.py "/movies/UnFilm/UnFilm.mkv" /app/suspect_clips

# Python local
python audio/pipeline.py "/movies/UnFilm/UnFilm.mkv" ./suspect_clips
```

Sortie : un JSON listant chaque segment suspect (début, fin, langue(s)
détectée(s), confiance, chemin du clip extrait) + les fichiers `.wav`
correspondants dans `./suspect_clips/`.

### Réglages utiles

Dans `analyze_audio()` :
- `window_seconds` (défaut 12) : taille des fenêtres d'analyse. Plus petit
  = plus précis sur les timestamps, mais plus lent.
- `min_confidence` (défaut 0.6) : seuil de confiance en dessous duquel une
  détection "langue étrangère" est ignorée (évite les faux positifs sur
  du bruit ou du silence mal transcrit).

## Tests

```bash
pip install pytest
pytest tests/ -v
```

Tous les tests testent la logique pure (regroupement, mots-clés) avec des
données simulées — pas besoin d'un vrai MKV ni de faster-whisper installé
pour les faire passer.

## Roadmap

- [x] **V1.0** — Détection pure (niveaux A et B), aucun appel IA
- [x] **V1.2** — Extraction et analyse de l'audio (détection des
      changements de langue → segments suspects, prêts pour Gemini)
- [ ] **V1.3** — Échantillonnage vidéo + OCR local → regroupement → Gemini
- [ ] **V1.4** — Gemini comme "juge" : décide quels éléments détectés
      (segments audio + textes OCR) doivent apparaître dans le
      `.forced.fr.srt`, génère le SRT final
- [ ] **V1.5** — Webhook Sonarr/Radarr (FastAPI), écriture du
      `Film.forced.fr.srt` à côté du MKV (jamais de modification du MKV
      lui-même), interface web minimale, base SQLite

## Structure du projet

```
forcedfr/
├── analyzer.py                # détection MKV (niveaux A/B) - V1.0
├── main.py                    # CLI pour analyzer.py
├── audio/
│   ├── extractor.py             # pistes audio + extraction ffmpeg
│   ├── language_detector.py     # découpage en fenêtres + détection langue
│   └── pipeline.py              # orchestration bout-en-bout - V1.2
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── tests/
│   ├── test_analyzer.py
│   └── test_language_detector.py
└── README.md
```

## Principe de sécurité (à garder pour toute la suite)

Le programme ne doit **jamais** supprimer ni modifier le `.mkv` original.
Il ne fait que lire, et plus tard écrire un fichier `.forced.fr.srt` à côté.
Si ce fichier existe déjà, aucune régénération.
