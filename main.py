"""ForcedFR V1.0 - Point d'entrée CLI.

Usage :
    python main.py /chemin/vers/film.mkv
    python main.py /chemin/vers/film.mkv --json
"""

from __future__ import annotations

import json
import sys

from analyzer import analyze_file


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python main.py <chemin_vers_fichier.mkv> [--json]")
        sys.exit(1)

    path = args[0]
    as_json = "--json" in args

    result = analyze_file(path)

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return

    print(f"\nFichier : {result.path}")
    if result.forced_found:
        print(f"✓ Forced FR trouvé (méthode : {result.method}, piste #{result.matched_track_index})")
    else:
        print("✗ Aucun Forced FR trouvé")
    print(f"Raison : {result.reason}\n")

    if result.all_subtitle_tracks:
        print("Pistes de sous-titres détectées :")
        for t in result.all_subtitle_tracks:
            print(f"  #{t.index} | lang={t.language} | forced_flag={t.forced_flag} | titre='{t.title}'")
    else:
        print("Aucune piste de sous-titres dans ce fichier.")

    if result.content_analyses:
        print("\nAnalyse de contenu (niveau C) :")
        for a in result.content_analyses:
            print(
                f"  #{a.track_index} | {a.classification} | "
                f"{a.cue_count} lignes | couverture={a.coverage_ratio:.0%} | "
                f"langue détectée={a.detected_language}"
            )
            print(f"     -> {a.reason}")


if __name__ == "__main__":
    main()
