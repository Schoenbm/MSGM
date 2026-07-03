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


# ── ERP (grands établissements recevant du public) ───────────────────────────

# Catégories ERP DPE tertiaire BDNB → fonction stratégique.
# Cat 1-2 = fort accueil public (> 1500 / > 700 personnes).
# Le type_erp affine : on le mappe à une fonction quand c'est possible.
_ERP_TYPE_TO_FONCTION: dict[str, str] = {
    "Salle": "gymnase",
    "Magasin": "centre_commercial",
    "Centre commercial": "centre_commercial",
    "Enseignement": "ecole",
    "Hôpital": "hopital",
    "Hôtel": "centre_commercial",
}

_ERP_CAT_COL = "dpe_ter_categorie_erp_dpe_tertiaire"
_ERP_TYPE_COL = "dpe_ter_type_erp_dpe_tertiaire"


def load_bdnb_erp(gpkg_path: "str | Path") -> "dict[str, str]":
    """Carte ID bâtiment BD TOPO → fonction, depuis les grands ERP BDNB (cat 1-2).

    Source optionnelle : dict vide si le gpkg est absent ou sans colonnes ERP.
    Destiné à être fusionné en **fallback** après OSM (OSM prime).
    """
    if gpkg_path is None or not Path(gpkg_path).exists():
        return {}
    gpkg_path = Path(gpkg_path)

    try:
        comp = gpd.read_file(
            gpkg_path, layer=_LAYER_COMPILE,
            columns=["batiment_groupe_id", _ERP_CAT_COL, _ERP_TYPE_COL],
            ignore_geometry=True,
        )
    except Exception:
        logger.debug("Colonnes ERP absentes du gpkg BDNB — pas d'enrichissement ERP")
        return {}

    if _ERP_CAT_COL not in comp.columns:
        return {}

    # Garder cat 1-2 (grands ERP, fort accueil public)
    cat = pd.to_numeric(comp[_ERP_CAT_COL], errors="coerce")
    big_erp = comp[cat.notna() & (cat <= 2)].copy()
    if big_erp.empty:
        return {}

    rel = gpd.read_file(
        gpkg_path, layer=_LAYER_REL_BDTOPO,
        columns=["bdtopo_bat_cleabs", "batiment_groupe_id"], ignore_geometry=True,
    )

    merged = rel.merge(big_erp, on="batiment_groupe_id", how="inner")
    result: dict[str, str] = {}
    for _, row in merged.iterrows():
        cleabs = row["bdtopo_bat_cleabs"]
        if pd.isna(cleabs) or cleabs in result:
            continue
        erp_type = row.get(_ERP_TYPE_COL, "")
        fonction = _ERP_TYPE_TO_FONCTION.get(str(erp_type).strip(), "centre_commercial")
        result[cleabs] = fonction

    logger.info("BDNB ERP (cat 1-2) : %d bâtiments stratégiques", len(result))
    return result


# Attributs de vulnérabilité (fichiers fonciers BDNB) consommés par le module de
# crise (réseau de neurones D1-D5). Meilleure couverture et libellés lisibles que
# MAT_MURS/MAT_TOITS BD TOPO (codes à 2 chiffres). Couverture mesurée sur la
# métropole : année ~58 %, matériaux ~64 % (dont ~23 % « INDETERMINE »). Les trous
# restants relèvent du prétraitement aval (imputation), pas de l'export.
_ATTR_COLS: dict[str, str] = {
    "ffo_bat_annee_construction": "annee_construction",
    "ffo_bat_mat_mur_txt": "mat_mur",
    "ffo_bat_mat_toit_txt": "mat_toit",
}


def load_bdnb_building_attrs(gpkg_path: "str | Path") -> pd.DataFrame:
    """Renvoie un DataFrame `ID → {annee_construction, mat_mur, mat_toit}` (BDNB ffo).

    Mêmes couches que `load_bdnb_building_usage` (`compile` + `rel` BD TOPO),
    d'autres colonnes. Source optionnelle : DataFrame vide (colonnes nommées) si le
    gpkg est absent. Les attributs d'un groupe sont propagés à chaque bâtiment
    BD TOPO du groupe.
    """
    cols = ["ID", *_ATTR_COLS.values()]
    if gpkg_path is None or not Path(gpkg_path).exists():
        logger.warning("BDNB absente (%s) — pas d'attributs matériaux/période", gpkg_path)
        return pd.DataFrame(columns=cols)
    gpkg_path = Path(gpkg_path)

    comp = gpd.read_file(
        gpkg_path, layer=_LAYER_COMPILE,
        columns=["batiment_groupe_id", *_ATTR_COLS], ignore_geometry=True,
    ).rename(columns=_ATTR_COLS)
    rel = gpd.read_file(
        gpkg_path, layer=_LAYER_REL_BDTOPO,
        columns=["bdtopo_bat_cleabs", "batiment_groupe_id"], ignore_geometry=True,
    )

    merged = rel.merge(comp, on="batiment_groupe_id", how="left")
    out = (
        merged.rename(columns={"bdtopo_bat_cleabs": "ID"})[cols]
        .dropna(subset=list(_ATTR_COLS.values()), how="all")
        .drop_duplicates(subset="ID")
        .reset_index(drop=True)
    )
    cov = {c: f"{100 * out[c].notna().mean():.0f}%" for c in _ATTR_COLS.values()}
    logger.info("BDNB attributs : %d bâtiments annotés, couverture %s", len(out), cov)
    return out
