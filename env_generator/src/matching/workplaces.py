"""Affectation d'un bâtiment-lieu-de-travail aux agents actifs.

Inspiré du localisateur spatial de Genstar (`spll`, `GravityFunction` :
masse / distance^friction). On retient ici une décroissance exponentielle :

    P(travail j | domicile i) ∝ capacite_j × exp(-distance_ij / decay_m)

- capacite_j  : surface de plancher (emprise au sol × nb d'étages), proxy du
                nombre d'emplois du bâtiment j (cf. "masse" du modèle gravitaire).
- distance_ij : distance euclidienne domicile→travail en mètres (Lambert-93).
- decay_m     : échelle de décroissance (m). Plus elle est petite, plus on
                travaille près de chez soi.

Ce module ne génère PAS la population : il reçoit des agents actifs déjà créés
(cf. matching/agents.py) et leur attribue à chacun un lieu de travail.

ASSOMPTIONS À REVOIR (premier jet, "on redesignera si besoin") :
- Lieux de travail = bâtiments BD TOPO dont USAGE1/USAGE2 ∈ `usages` d'emploi,
  COMPLÉTÉS par les "Indifférencié" que la BDNB qualifie d'emploi (via `extra_ids`,
  ~+4 500 sur la métropole ; cf. `identify_workplaces` et `loaders/bdnb.py`). Les
  Indifférencié sans signal BDNB restent exclus (bruités, sans tag).
- Capacité = surface de plancher seule (pas de densité d'emploi par type d'usage).
- Tout actif occupé travaille DANS la région chargée : pas de fuite hors zone,
  pas de télétravail, pas de calage sur les flux domicile-travail INSEE (MOBPRO).
- Modèle gravitaire à une seule échelle de distance, non calibré.
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd

from src.loaders.buildings import _compute_nb_etages  # même logique d'étages que le bâti

logger = logging.getLogger(__name__)

# CSP "actifs ayant un emploi" (PCS 1-6) — couplées au schéma INSEE comme les
# colonnes P22_/C22_ ailleurs. Les inactifs/chômeurs n'ont pas de lieu de travail.
ACTIVE_CSP_COLS: tuple[str, ...] = (
    "csp_agriculteurs",
    "csp_artisans_commercants",
    "csp_cadres",
    "csp_prof_intermediaires",
    "csp_employes",
    "csp_ouvriers",
)

# Valeurs USAGE1/USAGE2 (BD TOPO) considérées comme porteuses d'emploi par défaut.
DEFAULT_WORKPLACE_USAGES: tuple[str, ...] = (
    "Commercial et services",
    "Industriel",
    "Agricole",
    "Religieux",
    "Sportif",
)


def identify_workplaces(
    all_buildings: gpd.GeoDataFrame,
    usages: "tuple[str, ...] | list[str]" = DEFAULT_WORKPLACE_USAGES,
    extra_ids: "set[str] | None" = None,
) -> gpd.GeoDataFrame:
    """Sélectionne les bâtiments-lieux-de-travail et calcule leur capacité.

    Un bâtiment est retenu si USAGE1 OU USAGE2 ∈ usages, OU si son `ID` figure
    dans `extra_ids` (bâtiments d'emploi récupérés via la BDNB parmi les
    « Indifférencié » BD TOPO — cf. loaders/bdnb.py). Capacité = surface de
    plancher (emprise au sol × nb d'étages), proxy du nombre d'emplois. Les
    bâtiments de capacité nulle (surface 0) sont écartés.

    Args:
        all_buildings: tous les bâtiments de la zone (issu de buildings_all),
                       en Lambert-93 (surface en m²).
        usages:        valeurs USAGE considérées comme emploi.
        extra_ids:     ID BD TOPO supplémentaires à inclure (source BDNB).

    Returns:
        GeoDataFrame des lieux de travail avec une colonne `capacity` (float).
    """
    usages = tuple(usages)

    usage1 = all_buildings.get("USAGE1", pd.Series(index=all_buildings.index, dtype=str))
    usage2 = all_buildings.get("USAGE2", pd.Series(index=all_buildings.index, dtype=str))
    mask = usage1.isin(usages) | usage2.isin(usages)
    if extra_ids and "ID" in all_buildings.columns:
        n_before = int(mask.sum())
        mask = mask | all_buildings["ID"].isin(extra_ids)
        logger.info("Lieux de travail : +%d récupérés via BDNB (usages BD TOPO : %d)",
                    int(mask.sum()) - n_before, n_before)

    wp = all_buildings.loc[mask].copy()
    if wp.empty:
        logger.warning("Aucun bâtiment-lieu-de-travail pour usages=%s", usages)
        return wp

    nb_etages = _compute_nb_etages(wp)
    wp["capacity"] = wp.geometry.area * nb_etages
    wp = wp[wp["capacity"] > 0].copy()

    logger.info(
        "Lieux de travail : %d bâtiments retenus (usages=%s), capacité totale %.0f m²",
        len(wp), usages, wp["capacity"].sum(),
    )
    return wp


def assign_facilities(
    agents: gpd.GeoDataFrame,
    facilities: gpd.GeoDataFrame,
    decay_m: float = 3000.0,
    seed: int = 42,
    id_col: str = "ID",
    label: str = "équipement",
) -> gpd.GeoDataFrame:
    """Affecte à chaque agent un équipement (travail, école, crèche…) par gravité.

    Modèle : `P(j|i) ∝ capacite_j × exp(-dist_ij / decay_m)`. Les agents d'un même
    domicile partagent le même vecteur de probabilité (calcul une fois par
    `home_id`, regroupement). Affectation indépendante par type d'équipement.

    Args:
        agents:     agents à affecter. Géométrie = point du domicile (Lambert-93).
                    Doit contenir `home_id`. Autres colonnes conservées.
        facilities: équipements candidats. Doivent contenir `capacity`, `id_col`
                    et une géométrie (le centroïde est utilisé).
        decay_m:    échelle de décroissance distance (m).
        seed:       graine du tirage (reproductibilité).
        id_col:     colonne identifiant des équipements (ex. "ID", "osm_id").
        label:      libellé pour les logs (ex. "lieu de travail", "école").

    Returns:
        Copie de `agents` enrichie de dest_id, dist_m, dest_x, dest_y
        (NA si aucun équipement disponible).
    """
    result = agents.copy()
    result["dest_id"] = pd.array([pd.NA] * len(result), dtype="object")
    for col in ("dist_m", "dest_x", "dest_y"):
        result[col] = np.nan

    if result.empty:
        return result
    if facilities is None or facilities.empty:
        logger.warning("Aucun %s disponible — dest_id laissé vide pour %d agents",
                       label, len(result))
        return result

    if facilities.crs is not None and result.crs is not None and facilities.crs != result.crs:
        facilities = facilities.to_crs(result.crs)

    centroids = facilities.geometry.centroid
    f_xy = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])
    cap = facilities["capacity"].to_numpy(dtype=float)
    f_ids = facilities[id_col].to_numpy()

    rng = np.random.default_rng(seed)
    pts = result.geometry
    hx_all = pts.x.to_numpy()
    hy_all = pts.y.to_numpy()

    for _, idx in result.groupby("home_id", sort=False).groups.items():
        positions = result.index.get_indexer(idx)
        hx = hx_all[positions[0]]
        hy = hy_all[positions[0]]

        d = np.hypot(f_xy[:, 0] - hx, f_xy[:, 1] - hy)
        weights = cap * np.exp(-d / decay_m)
        total = weights.sum()
        if total <= 0:
            continue
        probs = weights / total

        choice = rng.choice(len(facilities), size=len(positions), p=probs)
        result.iloc[positions, result.columns.get_loc("dest_id")] = f_ids[choice]
        result.iloc[positions, result.columns.get_loc("dist_m")] = np.round(d[choice], 1)
        result.iloc[positions, result.columns.get_loc("dest_x")] = np.round(f_xy[choice, 0], 1)
        result.iloc[positions, result.columns.get_loc("dest_y")] = np.round(f_xy[choice, 1], 1)

    assigned = result["dest_id"].notna()
    if assigned.any():
        logger.info(
            "Affectation %s : %d agents  |  distance domicile-destination : "
            "médiane %.0f m, moyenne %.0f m, max %.0f m",
            label, int(assigned.sum()),
            result.loc[assigned, "dist_m"].median(),
            result.loc[assigned, "dist_m"].mean(),
            result.loc[assigned, "dist_m"].max(),
        )
    return result
