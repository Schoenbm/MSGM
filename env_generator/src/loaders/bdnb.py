"""Loader BDNB : usage consolidé des bâtiments pour récupérer des lieux de travail.

La BD TOPO classe une grande part des bâtiments en « Indifférencié » (exclus des
lieux de travail faute d'usage fiable). La **BDNB** (Base Nationale du Bâtiment,
CSTB) fournit un usage consolidé `usage_principal_bdnb_open` qui permet d'en
récupérer une partie : *Tertiaire / Secondaire / Primaire* = bâtiments d'emploi.

On lit deux couches du géopackage BDNB :
  - `batiment_groupe_compile`        : `usage_principal_bdnb_open` par groupe
  - `rel_batiment_groupe_bdtopo_bat` : lien groupe ↔ `cleabs` BD TOPO (= ID bâtiment)
et on renvoie l'ensemble des ID BD TOPO dont l'usage BDNB est un usage d'emploi.

Fichier local volumineux (~2,5 Go), **hors dépôt**. Source **optionnelle** : si le
gpkg est absent, le pipeline continue sans (lieux de travail = BD TOPO seuls).
"""

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# Valeurs de `usage_principal_bdnb_open` considérées comme porteuses d'emploi.
# (Résidentiel individuel/collectif, Dépendance, NaN → écartés.)
BDNB_EMPLOYMENT_USAGES: frozenset[str] = frozenset({"Tertiaire", "Secondaire", "Primaire"})

_LAYER_COMPILE = "batiment_groupe_compile"
_LAYER_REL_BDTOPO = "rel_batiment_groupe_bdtopo_bat"
_USAGE_COL = "usage_principal_bdnb_open"


def _employment_ids(rel: pd.DataFrame, comp: pd.DataFrame, usages: "set[str]") -> "set[str]":
    """Jointure groupe ↔ cleabs filtrée sur les usages d'emploi. Pure (testable).

    Args:
        rel:  colonnes `bdtopo_bat_cleabs`, `batiment_groupe_id`.
        comp: colonnes `batiment_groupe_id`, `usage_principal_bdnb_open`.
        usages: usages BDNB retenus comme emploi.

    Returns:
        ensemble des `bdtopo_bat_cleabs` (= ID bâtiment BD TOPO) d'emploi.
    """
    emp_groups = set(comp.loc[comp[_USAGE_COL].isin(usages), "batiment_groupe_id"])
    sel = rel[rel["batiment_groupe_id"].isin(emp_groups)]
    return set(sel["bdtopo_bat_cleabs"].dropna())


def load_bdnb_employment_ids(
    gpkg_path: "str | Path",
    usages: "frozenset[str]" = BDNB_EMPLOYMENT_USAGES,
) -> "set[str]":
    """Renvoie les ID BD TOPO que la BDNB classe en bâtiment d'emploi.

    Source optionnelle : renvoie un ensemble vide si le gpkg est absent.
    """
    gpkg_path = Path(gpkg_path)
    if not gpkg_path.exists():
        logger.warning("BDNB absente (%s) — lieux de travail issus de la BD TOPO seule", gpkg_path)
        return set()

    comp = gpd.read_file(
        gpkg_path, layer=_LAYER_COMPILE,
        columns=["batiment_groupe_id", _USAGE_COL], ignore_geometry=True,
    )
    rel = gpd.read_file(
        gpkg_path, layer=_LAYER_REL_BDTOPO,
        columns=["bdtopo_bat_cleabs", "batiment_groupe_id"], ignore_geometry=True,
    )
    ids = _employment_ids(rel, comp, set(usages))
    logger.info("BDNB : %d bâtiments d'emploi identifiés (usages %s)", len(ids), sorted(usages))
    return ids
