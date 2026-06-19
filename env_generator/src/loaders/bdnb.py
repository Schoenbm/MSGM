"""Loader BDNB : usage consolidé des bâtiments pour qualifier les « Indifférencié ».

La BD TOPO classe une grande part des bâtiments en « Indifférencié » (ni
résidentiel ni usage d'emploi identifié). La **BDNB** (Base Nationale du Bâtiment,
CSTB) fournit un usage consolidé `usage_principal_bdnb_open` qui permet d'en
qualifier une partie :

  - *Tertiaire / Secondaire / Primaire* → ``travail`` (lieu de travail)
  - *Résidentiel individuel / collectif* → ``residentiel`` (domicile)
  - *Dépendance*                         → ``annexe`` (ni l'un ni l'autre)

On lit deux couches du géopackage BDNB :
  - `batiment_groupe_compile`        : `usage_principal_bdnb_open` par groupe
  - `rel_batiment_groupe_bdtopo_bat` : lien groupe ↔ `cleabs` BD TOPO (= ID bâtiment)
et on renvoie une **carte ID bâtiment → catégorie** consommée par :
  - le filtre résidentiel (`loaders/buildings.py`) — exclut des logements les
    Indifférencié non-résidentiels, garde ceux dits résidentiels ;
  - l'identification des lieux de travail (`matching/workplaces.py`) — ajoute les
    bâtiments ``travail``.

Fichier local volumineux (~2,5 Go), **hors dépôt**. Source **optionnelle** : si le
gpkg est absent, la carte est vide et le pipeline continue (BD TOPO + OSM seuls).
"""

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# usage_principal_bdnb_open → catégorie consolidée. Les usages absents de cette
# table (ou NaN) restent non qualifiés (bâtiment laissé tel quel).
BDNB_USAGE_TO_CATEGORY: dict[str, str] = {
    "Tertiaire": "travail",
    "Secondaire": "travail",
    "Primaire": "travail",
    "Résidentiel individuel": "residentiel",
    "Résidentiel collectif": "residentiel",
    "Dépendance": "annexe",
}

_LAYER_COMPILE = "batiment_groupe_compile"
_LAYER_REL_BDTOPO = "rel_batiment_groupe_bdtopo_bat"
_USAGE_COL = "usage_principal_bdnb_open"


def _usage_map(rel: pd.DataFrame, comp: pd.DataFrame) -> "dict[str, str]":
    """Carte `bdtopo_bat_cleabs` → catégorie (`travail`/`residentiel`/`annexe`).

    Pure (testable). Les groupes sans usage mappable sont ignorés.

    Args:
        rel:  colonnes `bdtopo_bat_cleabs`, `batiment_groupe_id`.
        comp: colonnes `batiment_groupe_id`, `usage_principal_bdnb_open`.
    """
    cat = comp[_USAGE_COL].map(BDNB_USAGE_TO_CATEGORY)
    group_to_cat = dict(zip(comp["batiment_groupe_id"][cat.notna()], cat[cat.notna()]))
    out: dict[str, str] = {}
    for cleabs, gid in zip(rel["bdtopo_bat_cleabs"], rel["batiment_groupe_id"]):
        c = group_to_cat.get(gid)
        if c is not None and pd.notna(cleabs):
            out[cleabs] = c
    return out


def load_bdnb_building_usage(gpkg_path: "str | Path") -> "dict[str, str]":
    """Renvoie la carte ID bâtiment BD TOPO → catégorie d'usage BDNB.

    Source optionnelle : carte vide si le gpkg est absent.
    """
    gpkg_path = Path(gpkg_path)
    if not gpkg_path.exists():
        logger.warning("BDNB absente (%s) — qualification des bâtiments via BD TOPO + OSM seuls", gpkg_path)
        return {}

    comp = gpd.read_file(
        gpkg_path, layer=_LAYER_COMPILE,
        columns=["batiment_groupe_id", _USAGE_COL], ignore_geometry=True,
    )
    rel = gpd.read_file(
        gpkg_path, layer=_LAYER_REL_BDTOPO,
        columns=["bdtopo_bat_cleabs", "batiment_groupe_id"], ignore_geometry=True,
    )
    usage = _usage_map(rel, comp)
    counts: dict[str, int] = {}
    for c in usage.values():
        counts[c] = counts.get(c, 0) + 1
    logger.info("BDNB : %d bâtiments qualifiés %s", len(usage), counts)
    return usage


def employment_ids(usage: "dict[str, str]") -> "set[str]":
    """ID bâtiments d'emploi (catégorie ``travail``) depuis la carte d'usage."""
    return {bid for bid, cat in usage.items() if cat == "travail"}
