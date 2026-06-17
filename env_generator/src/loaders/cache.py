"""Pipeline de cache unique pour tout accès à des données (download ou calcul).

Toute donnée onéreuse (téléchargement IGN/INSEE, requête OSM/Overpass, extraction
d'archive) suit la **même** logique :

    1. si le cache local existe ET passe le contrôle d'intégrité → on le réutilise ;
    2. sinon on (re)produit dans un emplacement temporaire (`.part`), on contrôle
       le résultat, puis on bascule **atomiquement** vers le chemin final.

Conséquence : un produit interrompu (réseau coupé, Ctrl-C, extraction partielle)
laisse au pire un `.part` (supprimé), jamais un cache corrompu pris pour valide.

La **seule** partie spécifique au type de données est le *validateur* d'intégrité
(`validate`), injecté par l'appelant : un zip ne se vérifie pas comme un GeoPackage.
Des validateurs prêts à l'emploi sont fournis ci-dessous.
"""
import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Un producteur écrit les données au chemin (fichier ou dossier) qu'on lui passe.
Producer = Callable[[Path], None]
# Un validateur dit si le chemin contient des données complètes / non corrompues.
# Il ne doit jamais lever : toute erreur = données invalides (→ False).
Validator = Callable[[Path], bool]


def _remove(path: Path) -> None:
    """Supprime un fichier OU un dossier, sans échouer s'il est absent."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def ensure_cached(
    dest: Path,
    *,
    produce: Producer,
    validate: Optional[Validator] = None,
    label: Optional[str] = None,
) -> Path:
    """Garantit la présence à ``dest`` d'un cache complet, et le renvoie.

    Args:
        dest:     chemin final du cache (fichier ou dossier).
        produce:  ``produce(tmp)`` écrit les données à ``tmp`` (appelé seulement si
                  le cache est absent ou invalide).
        validate: ``validate(path) -> bool`` ; intégrité/complétude des données.
                  Seule partie spécifique au type. ``None`` = aucun contrôle.
        label:    nom court pour les logs (défaut : ``dest.name``).
    """
    dest = Path(dest)
    label = label or dest.name

    if dest.exists():
        if validate is None or validate(dest):
            logger.info("Cache trouvé : %s", label)
            return dest
        logger.warning("Cache invalide/corrompu, régénération : %s", label)
        _remove(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # On insère ".part" AVANT l'extension finale pour la préserver (x.gpkg →
    # x.part.gpkg) : certains pilotes géo râlent sur une extension non conforme.
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    _remove(tmp)  # résidu d'un run précédent interrompu
    logger.debug("Production du cache %s (temporaire : %s)", label, tmp.name)
    try:
        produce(tmp)
        if validate is not None and not validate(tmp):
            raise IOError(f"Données produites invalides ou incomplètes : {label}")
    except BaseException:
        _remove(tmp)
        raise
    os.replace(tmp, dest)  # bascule atomique (même système de fichiers)
    return dest


# ── Validateurs prêts à l'emploi ────────────────────────────────────────────────

def valid_zip(path: Path) -> bool:
    """True si ``path`` est un ZIP complet (vérifie l'end-of-central-directory,
    ce qui détecte les téléchargements tronqués)."""
    import zipfile
    return zipfile.is_zipfile(path)


def valid_7z(path: Path) -> bool:
    """True si ``path`` est une archive 7z ouvrable."""
    try:
        import py7zr
        with py7zr.SevenZipFile(path, mode="r"):
            return True
    except Exception:
        return False


def valid_geofile(path: Path) -> bool:
    """True si ``path`` est un fichier géo lisible (GeoPackage, GeoJSON, …).

    Lit une seule entité : valide la structure sans charger tout le fichier.
    Un fichier tronqué/corrompu par une écriture interrompue lèvera → False.
    """
    try:
        import geopandas as gpd
        gpd.read_file(path, rows=1)
        return True
    except Exception:
        return False


def valid_dir_with(*, glob: str, predicate: Callable[[Path], bool] | None = None) -> Validator:
    """Construit un validateur de dossier : True si au moins un fichier matchant
    ``glob`` (récursif) existe et, optionnellement, satisfait ``predicate``."""
    def _validate(path: Path) -> bool:
        if not path.is_dir():
            return False
        for p in path.rglob(glob):
            if predicate is None or predicate(p):
                return True
        return False
    return _validate
