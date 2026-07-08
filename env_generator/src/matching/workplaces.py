"""Affectation d'un bâtiment-lieu-de-travail aux agents actifs.

Chemin PRINCIPAL (chantier MOBPRO) — affectation en 2 étapes calée sur les flux
réels domicile-travail INSEE (`assign_workplaces_mobpro`, décision D1 option B) :
  1. tirer la COMMUNE de travail c' selon P(c'|c), la matrice MOBPRO clippée aux
     communes de la région et renormalisée par ligne (`build_commute_matrix`, D2a) ;
  2. dans c', tirer le BÂTIMENT par le modèle gravitaire ci-dessous, restreint
     aux lieux de travail de c'.
Replis (D3) : commune de résidence absente de la matrice, ou c' sans lieu de
travail identifié → gravité globale sur toute la région (`assign_facilities`).

Modèle gravitaire (inspiré du localisateur spatial de Genstar — `spll`,
`GravityFunction` : masse / distance^friction), décroissance exponentielle :

    P(travail j | domicile i) ∝ capacite_j × exp(-distance_ij / decay_m)

- capacite_j  : surface de plancher (emprise au sol × nb d'étages), proxy du
                nombre d'emplois du bâtiment j (cf. "masse" du modèle gravitaire).
- distance_ij : distance euclidienne domicile→travail en mètres (Lambert-93).
- decay_m     : échelle de décroissance (m). Plus elle est petite, plus on
                travaille près de chez soi.

Ce module ne génère PAS la population : il reçoit des agents actifs déjà créés
(cf. matching/agents.py) et leur attribue à chacun un lieu de travail.

ASSOMPTIONS À REVOIR (« on redesignera si besoin ») :
- Lieux de travail = bâtiments BD TOPO dont USAGE1/USAGE2 ∈ `usages` d'emploi,
  COMPLÉTÉS par les "Indifférencié" que la BDNB qualifie d'emploi (via `extra_ids`,
  ~+4 500 sur la métropole ; cf. `identify_workplaces` et `loaders/bdnb.py`). Les
  Indifférencié sans signal BDNB restent exclus (bruités, sans tag).
- Capacité = surface de plancher seule (pas de densité d'emploi par type d'usage).
- Tout actif occupé travaille DANS la région chargée (D2a : les flux MOBPRO
  sortants sont réaffectés aux destinations internes) ; pas de télétravail. Les
  actifs venant de l'EXTÉRIEUR travailler dans la zone = chantier futur.
- `decay_m` intra-commune non calibré (la répartition ENTRE communes, elle, est
  calée sur MOBPRO).
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd

from src.loaders.buildings import _compute_nb_etages  # même logique d'étages que le bâti
from src.loaders.mobpro import COL_FLUX, COL_RESIDENCE, COL_TRAVAIL

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


def build_commute_matrix(
    mobpro_df: pd.DataFrame,
    region_communes: "set[str] | list[str] | tuple[str, ...]",
) -> pd.DataFrame:
    """Matrice P(commune de travail | commune de résidence) depuis les flux MOBPRO.

    Clip région + renormalisation par ligne (décision D2a) : seuls les flux dont
    la RÉSIDENCE **et** le TRAVAIL sont dans `region_communes` sont conservés,
    puis chaque ligne est renormalisée à 1 — les actifs qui travailleraient hors
    zone sont réaffectés proportionnellement aux destinations internes (tout le
    monde reste dans la zone simulée ; les actifs venant de l'extérieur = chantier
    futur, hors périmètre).

    Une commune de résidence sans AUCUNE destination en région est ABSENTE de
    l'index : l'appelant bascule ses actifs en gravité globale (repli D3).

    Args:
        mobpro_df:       flux MOBPRO 2022 (colonnes CODGEO, DCLT,
                         NBFLUX_C22_ACTOCC15P — cf. loaders/mobpro.py, millésime
                         couplé au code).
        region_communes: codes commune (5 caractères) de la zone simulée.

    Returns:
        DataFrame : index = communes de résidence (en région), colonnes =
        communes de travail (en région), valeurs = P(c'|c) — lignes sommant à 1.
    """
    region = {str(c).strip() for c in region_communes}
    df = pd.DataFrame({
        "res": mobpro_df[COL_RESIDENCE].astype(str).str.strip(),
        "tra": mobpro_df[COL_TRAVAIL].astype(str).str.strip(),
        "flux": pd.to_numeric(mobpro_df[COL_FLUX], errors="coerce").fillna(0.0),
    })
    resident = df[df["res"].isin(region)]
    kept = resident[resident["tra"].isin(region)]
    if kept.empty:
        logger.warning("Matrice MOBPRO vide : aucun flux interne aux %d communes région",
                       len(region))
        return pd.DataFrame()

    flux_resident = float(resident["flux"].sum())
    flux_kept = float(kept["flux"].sum())
    if flux_resident > 0:
        logger.info(
            "Matrice MOBPRO : %.1f %% des flux des résidents conservés après clip "
            "région (%d communes) — le reste travaille hors zone, réaffecté aux "
            "destinations internes (D2a)",
            100.0 * flux_kept / flux_resident, len(region),
        )

    pivot = kept.groupby(["res", "tra"])["flux"].sum().unstack(fill_value=0.0)
    pivot = pivot.loc[pivot.sum(axis=1) > 0]
    matrix = pivot.div(pivot.sum(axis=1), axis=0)
    matrix.index.name = COL_RESIDENCE
    matrix.columns.name = COL_TRAVAIL
    return matrix


def assign_workplaces_mobpro(
    workers: gpd.GeoDataFrame,
    workplaces: gpd.GeoDataFrame,
    commute_matrix: pd.DataFrame,
    decay_m: float = 3000.0,
    seed: int = 42,
) -> gpd.GeoDataFrame:
    """Affecte un lieu de travail en 2 étapes calées sur MOBPRO (D1, option B).

    Étape 1 : pour chaque actif de commune `c`, tire sa commune de travail `c'`
    selon P(c'|c) (`commute_matrix`, cf. build_commute_matrix). Étape 2 : dans
    `c'`, tire le bâtiment par la gravité habituelle (capacité × exp(-dist/decay))
    restreinte aux lieux de travail de `c'`.

    Replis (D3, gravité globale sur TOUS les lieux de travail) : commune de
    résidence absente de la matrice (ou inconnue), ou commune tirée sans lieu de
    travail identifié. Déterministe à seed fixé.

    Args:
        workers:        actifs à affecter — `home_id`, `commune` (résidence,
                        5 caractères), géométrie = point du domicile (Lambert-93).
        workplaces:     lieux de travail — `ID`, `capacity`, `commune`, géométrie.
        commute_matrix: P(c'|c), index = communes de résidence, colonnes =
                        communes de travail (lignes normalisées).
        decay_m:        échelle de décroissance de la gravité intra-commune (m).
        seed:           graine du tirage (reproductibilité).

    Returns:
        Copie de `workers` enrichie de dest_id, dist_m, dest_x, dest_y.
    """
    result = workers.copy()
    result["dest_id"] = pd.array([pd.NA] * len(result), dtype="object")
    for col in ("dist_m", "dest_x", "dest_y"):
        result[col] = np.nan
    if result.empty:
        return result
    if workplaces is None or workplaces.empty:
        logger.warning("Aucun lieu de travail disponible — dest_id laissé vide "
                       "pour %d actifs", len(result))
        return result
    if workplaces.crs is not None and result.crs is not None and workplaces.crs != result.crs:
        workplaces = workplaces.to_crs(result.crs)

    rng = np.random.default_rng(seed)
    centroids = workplaces.geometry.centroid
    f_xy = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])
    cap = workplaces["capacity"].to_numpy(dtype=float)
    f_ids = workplaces["ID"].to_numpy()
    # Commune inconnue (code_iris BD TOPO lacunaire) → "" : le bâtiment reste
    # tirable au repli global (D3) mais jamais à l'étape intra-commune. NB :
    # astype(str) pandas PRÉSERVE les NaN (mélange str/float), d'où la boucle.
    wp_commune = np.asarray(
        ["" if pd.isna(c) else str(c) for c in workplaces["commune"]], dtype=object
    )
    wp_positions = {c: np.flatnonzero(wp_commune == c)
                    for c in np.unique(wp_commune) if c != ""}

    n = len(result)
    home_commune = np.asarray(
        ["" if pd.isna(c) else str(c) for c in result["commune"]], dtype=object
    )

    # Étape 1 (D1) : tirage de la commune de travail c' via P(c'|c). Communes de
    # résidence hors matrice → c' vide (repli D3 plus bas). Itération en ordre
    # trié → consommation du rng déterministe.
    matrix_cols = np.asarray(commute_matrix.columns, dtype=object)
    dest_commune = np.full(n, "", dtype=object)
    for c in sorted(set(home_commune)):
        if c == "" or c not in commute_matrix.index:
            continue
        p = commute_matrix.loc[c].to_numpy(dtype=float)
        total = p.sum()
        if total <= 0:
            continue
        pos = np.flatnonzero(home_commune == c)
        drawn = rng.choice(len(matrix_cols), size=len(pos), p=p / total)
        dest_commune[pos] = matrix_cols[drawn]

    # Étape 2 : gravité restreinte aux lieux de travail de c'. Repli D3 (clé "")
    # = gravité globale : commune hors matrice OU c' sans lieu de travail.
    fallback = np.asarray([c == "" or str(c) not in wp_positions for c in dest_commune])
    group_key = np.where(fallback, "", dest_commune.astype(str))

    hx_all = result.geometry.x.to_numpy()
    hy_all = result.geometry.y.to_numpy()
    icol = {c: result.columns.get_loc(c) for c in ("dest_id", "dist_m", "dest_x", "dest_y")}

    # Un vecteur de probabilité par (commune de travail, domicile) — les actifs
    # d'un même domicile allant dans la même commune partagent le tirage.
    groups = pd.DataFrame({"k": group_key, "h": result["home_id"].to_numpy()})
    for (key, _home), idx in groups.groupby(["k", "h"], sort=True).groups.items():
        positions = np.asarray(idx)  # RangeIndex de `groups` → positions iloc
        sel = wp_positions[key] if key else None  # None = pool global (D3)
        sub_xy = f_xy if sel is None else f_xy[sel]
        sub_cap = cap if sel is None else cap[sel]
        d = np.hypot(sub_xy[:, 0] - hx_all[positions[0]],
                     sub_xy[:, 1] - hy_all[positions[0]])
        weights = sub_cap * np.exp(-d / decay_m)
        total = weights.sum()
        if total <= 0 and sel is not None:
            # Poids tous nuls dans c' (capacités nulles) → dernier repli : global.
            sel = None
            d = np.hypot(f_xy[:, 0] - hx_all[positions[0]],
                         f_xy[:, 1] - hy_all[positions[0]])
            weights = cap * np.exp(-d / decay_m)
            total = weights.sum()
        if total <= 0:
            continue
        choice = rng.choice(len(weights), size=len(positions), p=weights / total)
        abs_choice = choice if sel is None else sel[choice]
        result.iloc[positions, icol["dest_id"]] = f_ids[abs_choice]
        result.iloc[positions, icol["dist_m"]] = np.round(d[choice], 1)
        result.iloc[positions, icol["dest_x"]] = np.round(f_xy[abs_choice, 0], 1)
        result.iloc[positions, icol["dest_y"]] = np.round(f_xy[abs_choice, 1], 1)

    assigned = result["dest_id"].notna()
    if assigned.any():
        logger.info(
            "Affectation lieu de travail (MOBPRO 2 étapes) : %d actifs, dont %d en "
            "repli gravité globale (commune hors matrice ou sans lieu de travail)  |  "
            "distance : médiane %.0f m, moyenne %.0f m, max %.0f m",
            int(assigned.sum()), int(fallback.sum()),
            result.loc[assigned, "dist_m"].median(),
            result.loc[assigned, "dist_m"].mean(),
            result.loc[assigned, "dist_m"].max(),
        )
        id_to_commune = dict(zip(f_ids, wp_commune))
        _log_flow_comparison(
            home_commune[assigned.to_numpy()],
            result.loc[assigned, "dest_id"].map(id_to_commune).to_numpy(),
            commute_matrix,
        )
    return result


def _log_flow_comparison(
    home_communes: np.ndarray,
    dest_communes: np.ndarray,
    commute_matrix: pd.DataFrame,
    n_top: int = 5,
) -> None:
    """Logge les plus gros flux c->c' générés face à la cible MOBPRO P(c'|c).

    Validation à l'œil du calage : les gros flux doivent dominer et les parts
    générées coller aux parts MOBPRO (aux replis D3 et au bruit de tirage près).
    """
    flows = pd.crosstab(pd.Series(home_communes, name="res"),
                        pd.Series(dest_communes, name="tra"))
    if flows.empty:
        return
    shares = flows.div(flows.sum(axis=1), axis=0)
    lines = []
    for (c, cp), count in flows.stack().sort_values(ascending=False).head(n_top).items():
        target = (float(commute_matrix.loc[c, cp])
                  if c in commute_matrix.index and cp in commute_matrix.columns
                  else float("nan"))
        lines.append(f"  {c} -> {cp} : {int(count)} actifs, part générée "
                     f"{float(shares.loc[c, cp]):.3f} vs MOBPRO {target:.3f}")
    # NB : pas de caractères hors cp1252 dans les logs (console Windows).
    logger.info("Flux domicile-travail générés vs MOBPRO (top %d) :\n%s",
                len(lines), "\n".join(lines))
