"""Génération de la population d'agents individuels.

Descend de l'agrégat-par-bâtiment (sortie de `allocate_population`) à une liste
d'individus. Chaque agent possède :

    - un domicile        (home_id + point géométrique du bâtiment résidentiel)
    - un âge             (entier, tiré dans une tranche INSEE)
    - une CSP            (csp_* pour les 18+, "mineur" pour les moins de 18 ans)
    - une activité + une destination (dest_id) : travail / école / crèche / aucune

Méthode (microsimulation par tirage, PAS un IPF à la Genstar) :
  1. Pour chaque bâtiment résidentiel, on tire `population_allouee` individus.
  2. L'âge est tiré dans les tranches `age_*` (multinomiale ∝ effectifs), puis un
     âge entier uniforme dans la tranche.
  3. La CSP des adultes (18+) est tirée dans les `csp_*` (multinomiale ∝ effectifs).
     Les mineurs reçoivent csp="mineur" et aucun lieu de travail.
  4. Les actifs occupés (CSP ∈ ACTIVE_CSP_COLS) reçoivent un lieu de travail.

ASSOMPTIONS À REVOIR (premier jet) :
- Âge et CSP sont des marges INSEE *indépendantes* par bâtiment : on les tire
  séparément, on ne reconstruit pas la table jointe âge×CSP (ce que ferait un IPF
  / un échantillon comme gospl/Genstar). Les marges sont respectées en espérance,
  pas exactement (tirage aléatoire).
- Seuil adulte = 18 ans. Les 15-17 ans (comptés dans la pop CSP "15+" INSEE) sont
  donc traités comme mineurs sans CSP — léger écart documenté.
- Tranche 80+ bornée arbitrairement à [80, 99].
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd

from src.matching.workplaces import (
    ACTIVE_CSP_COLS,
    DEFAULT_WORKPLACE_USAGES,
    assign_facilities,
    identify_workplaces,
)

logger = logging.getLogger(__name__)

# Tranches d'âge INSEE (RP 2022) → bornes [min, max] inclusives pour le tirage entier.
AGE_BANDS: dict[str, tuple[int, int]] = {
    "age_0_2": (0, 2),
    "age_3_5": (3, 5),
    "age_6_10": (6, 10),
    "age_11_17": (11, 17),
    "age_18_24": (18, 24),
    "age_25_39": (25, 39),
    "age_40_54": (40, 54),
    "age_55_64": (55, 64),
    "age_65_79": (65, 79),
    "age_80p": (80, 99),
}

# Toutes les CSP allouées (actifs occupés + inactifs/chômeurs).
ALL_CSP_COLS: tuple[str, ...] = ACTIVE_CSP_COLS + ("csp_chomeurs_inactifs", "csp_autres_inactifs")

ADULT_MIN_AGE = 18
MINOR_CSP = "mineur"
RETIREMENT_AGE = 62       # > 62 ans → à la retraite, pas de travail

# Bornes d'âge des niveaux scolaires (système français). Répartition supposée
# uniforme : l'âge entier est tiré uniformément dans la tranche INSEE, donc une
# tranche à cheval (11-17) se scinde mécaniquement (collège 11-14, lycée 15-17).
CRECHE_MAX_AGE = 2        # 0-2  → crèche
ECOLE_MAX_AGE = 10        # 3-10 → école (maternelle + élémentaire)
COLLEGE_MAX_AGE = 14      # 11-14 → collège
LYCEE_MAX_AGE = 17        # 15-17 → lycée

# Activités (destination de jour de l'agent en situation normale).
ACT_CRECHE = "creche"
ACT_ECOLE = "ecole"
ACT_COLLEGE = "college"
ACT_LYCEE = "lycee"
ACT_TRAVAIL = "travail"
ACT_AUCUNE = "aucune"     # inactifs, chômeurs, retraités → au domicile


def generate_agents(
    residential: gpd.GeoDataFrame,
    all_buildings: gpd.GeoDataFrame,
    education: "gpd.GeoDataFrame | None" = None,
    usages: "tuple[str, ...] | list[str]" = DEFAULT_WORKPLACE_USAGES,
    decay_m: float = 3000.0,
    education_decay_m: float = 1200.0,
    seed: int = 42,
) -> gpd.GeoDataFrame:
    """Génère un GeoDataFrame d'agents individuels (domicile, âge, CSP, activité).

    Chaque agent reçoit une **activité** et, si elle s'y prête, une **destination**
    (lieu de travail / crèche / école / collège / lycée) affectée par gravité :
      - 0-2 ans            → crèche
      - 3-10 ans           → école
      - 11-14 ans          → collège
      - 15-17 ans          → lycée
      - 18-62 ans actif    → travail
      - sinon (inactif, chômeur, retraité > 62 ans) → aucune (reste au domicile)

    Args:
        residential: bâtiments résidentiels avec `population_allouee`, `age_*` et
                     `csp_*` (sortie de allocate_population), Lambert-93, colonne `ID`.
        all_buildings: tous les bâtiments (buildings_all) — lieux de travail.
        education:   équipements éducatifs (colonnes `equip_id`, `kind`, géométrie,
                     `capacity` optionnelle ; cf. loaders.bpe.load_bpe_education).
                     Si None, les enfants reçoivent une activité mais aucune destination.
        usages:      valeurs USAGE retenues comme lieux de travail.
        decay_m:     échelle de décroissance distance pour le travail (m).
        education_decay_m: idem pour école/crèche (plus court : on scolarise au
                     plus proche).
        seed:        graine du tirage (reproductibilité).

    Returns:
        GeoDataFrame, une ligne par agent. Colonnes : agent_id, home_id, age,
        age_band, csp, activity, is_worker, dest_id, dest_x, dest_y, dist_m,
        geometry (point du domicile, Lambert-93).
    """
    crs = residential.crs
    rng = np.random.default_rng(seed)

    age_cols = [c for c in AGE_BANDS if c in residential.columns]
    csp_cols = [c for c in ALL_CSP_COLS if c in residential.columns]
    if not age_cols:
        logger.warning("Aucune colonne age_* — agents sans âge")
    if not csp_cols:
        logger.warning("Aucune colonne csp_* — agents sans CSP")

    res = residential[residential.get("population_allouee", 0) > 0].copy()
    if res.empty:
        logger.warning("Aucun bâtiment peuplé — population d'agents vide")
        return _empty_agents(crs)

    # Distributions globales de repli : un petit bâtiment peuplé peut avoir 0 dans
    # CHAQUE tranche age_*/csp_* (plus fort reste appliqué marge par marge), alors
    # que son total survit. On retombe alors sur la distribution du jeu entier
    # plutôt que de produire des "inconnu".
    age_fallback = _normalize([res[c].sum() for c in age_cols]) if age_cols else None
    csp_fallback = _normalize([res[c].sum() for c in csp_cols]) if csp_cols else None

    centroids = res.geometry.centroid
    cx = centroids.x.to_numpy()
    cy = centroids.y.to_numpy()
    ids = res["ID"].to_numpy()

    home_ids: list = []
    ages: list = []
    bands: list = []
    csps: list = []
    hxs: list = []
    hys: list = []

    for pos in range(len(res)):
        row = res.iloc[pos]
        pop = int(round(row["population_allouee"]))
        if pop <= 0:
            continue

        agent_ages, agent_bands = _draw_ages(row, age_cols, pop, rng, age_fallback)
        agent_csps = _draw_csps(row, csp_cols, agent_ages, rng, csp_fallback)

        home_ids.extend([ids[pos]] * pop)
        ages.extend(agent_ages)
        bands.extend(agent_bands)
        csps.extend(agent_csps)
        hxs.extend([cx[pos]] * pop)
        hys.extend([cy[pos]] * pop)

    if not home_ids:
        return _empty_agents(crs)

    agents = gpd.GeoDataFrame(
        {
            "agent_id": np.arange(len(home_ids)),
            "home_id": home_ids,
            "age": np.asarray(ages, dtype=int),
            "age_band": bands,
            "csp": csps,
        },
        geometry=gpd.points_from_xy(hxs, hys),
        crs=crs,
    )
    agents["activity"] = _classify_activity(agents)
    agents["is_worker"] = agents["activity"] == ACT_TRAVAIL

    logger.info(
        "Agents générés : %d  |  âge moyen %.1f  |  activités : %s",
        len(agents), agents["age"].mean(), agents["activity"].value_counts().to_dict(),
    )

    # Destinations par activité, chacune sur son propre jeu d'équipements.
    agents["dest_id"] = pd.array([pd.NA] * len(agents), dtype="object")
    for col in ("dest_x", "dest_y", "dist_m"):
        agents[col] = np.nan

    workplaces = identify_workplaces(all_buildings, usages)
    # Faute de niveau fiable dans OSM, collège/lycée retombent sur le pool "école"
    # générique quand aucun établissement n'est explicitement nommé/typé.
    plans = [
        (ACT_TRAVAIL, workplaces, "ID", "lieu de travail", decay_m),
        (ACT_CRECHE, _education_subset(education, ACT_CRECHE, crs), "equip_id", "crèche", education_decay_m),
        (ACT_ECOLE, _education_subset(education, ACT_ECOLE, crs), "equip_id", "école", education_decay_m),
        (ACT_COLLEGE, _education_subset(education, ACT_COLLEGE, crs, fallback=ACT_ECOLE), "equip_id", "collège", education_decay_m),
        (ACT_LYCEE, _education_subset(education, ACT_LYCEE, crs, fallback=ACT_ECOLE), "equip_id", "lycée", education_decay_m),
    ]
    for activity, facilities, id_col, label, decay in plans:
        sub = agents[agents["activity"] == activity]
        if sub.empty:
            continue
        if facilities is None or facilities.empty:
            logger.warning("Aucun %s — %d agents '%s' sans destination",
                           label, len(sub), activity)
            continue
        assigned = assign_facilities(sub, facilities, decay_m=decay, seed=seed,
                                     id_col=id_col, label=label)
        for col in ("dest_id", "dest_x", "dest_y", "dist_m"):
            agents.loc[sub.index, col] = assigned[col].values

    return agents


def _classify_activity(agents: gpd.GeoDataFrame) -> "np.ndarray":
    """Détermine l'activité de jour de chaque agent (crèche/école/travail/aucune)."""
    age = agents["age"].to_numpy()
    is_active_csp = agents["csp"].isin(ACTIVE_CSP_COLS).to_numpy()

    activity = np.full(len(agents), ACT_AUCUNE, dtype=object)
    activity[(age >= 0) & (age <= CRECHE_MAX_AGE)] = ACT_CRECHE
    activity[(age > CRECHE_MAX_AGE) & (age <= ECOLE_MAX_AGE)] = ACT_ECOLE
    activity[(age > ECOLE_MAX_AGE) & (age <= COLLEGE_MAX_AGE)] = ACT_COLLEGE
    activity[(age > COLLEGE_MAX_AGE) & (age <= LYCEE_MAX_AGE)] = ACT_LYCEE
    # Travail : actif occupé, 18-62 ans (les > 62 ans sont à la retraite).
    activity[is_active_csp & (age >= ADULT_MIN_AGE) & (age <= RETIREMENT_AGE)] = ACT_TRAVAIL
    return activity


def _education_subset(
    education: "gpd.GeoDataFrame | None", kind: str, crs,
    fallback: "str | None" = None,
) -> "gpd.GeoDataFrame | None":
    """Filtre les équipements éducatifs par type et leur ajoute une capacité unité.

    Faute de donnée de capacité OSM, chaque équipement pèse 1 : l'affectation est
    alors dominée par la proximité (les enfants vont au plus proche, à l'aléa près).
    Si le type demandé est vide et qu'un `fallback` est fourni (ex. collège/lycée
    non distingués dans OSM → pool "école" générique), on bascule dessus.
    """
    if education is None or education.empty or "kind" not in education.columns:
        return None
    sub = education[education["kind"] == kind].copy()
    if sub.empty and fallback is not None:
        sub = education[education["kind"] == fallback].copy()
    if sub.empty:
        return None
    if sub.crs is not None and crs is not None and sub.crs != crs:
        sub = sub.to_crs(crs)
    if "capacity" not in sub.columns:
        sub["capacity"] = 1.0  # capacité inconnue → proximité dominante
    return sub


def _normalize(values) -> "np.ndarray | None":
    """Vecteur de probabilité à partir d'effectifs ; None si somme nulle."""
    arr = np.array([max(0.0, float(v)) for v in values])
    total = arr.sum()
    return arr / total if total > 0 else None


def _draw_ages(row, age_cols, pop, rng, fallback=None) -> tuple[list, list]:
    """Tire `pop` âges (entiers) et leur tranche, selon les effectifs age_* du bâtiment.

    Repli sur la distribution `fallback` si le bâtiment n'a aucun effectif d'âge."""
    if not age_cols:
        return [-1] * pop, ["inconnu"] * pop
    probs = _normalize([row[c] for c in age_cols])
    if probs is None:
        probs = fallback
    if probs is None:
        return [-1] * pop, ["inconnu"] * pop
    draw = rng.multinomial(pop, probs)
    ages: list = []
    bands: list = []
    for col, n in zip(age_cols, draw):
        if n == 0:
            continue
        lo, hi = AGE_BANDS[col]
        ages.extend(rng.integers(lo, hi + 1, size=int(n)).tolist())
        bands.extend([col] * int(n))
    return ages, bands


def _draw_csps(row, csp_cols, agent_ages, rng, fallback=None) -> list:
    """Affecte une CSP aux adultes (18+) selon les effectifs csp_* ; mineur sinon.

    Repli sur la distribution `fallback` si le bâtiment n'a aucun effectif CSP.
    Les âges inconnus (-1) sont traités comme adultes (faute de mieux)."""
    ages_arr = np.asarray(agent_ages)
    is_adult = (ages_arr >= ADULT_MIN_AGE) | (ages_arr < 0)
    n_adults = int(is_adult.sum())

    result = np.array([MINOR_CSP] * len(agent_ages), dtype=object)
    if n_adults == 0 or not csp_cols:
        return result.tolist()

    probs = _normalize([row[c] for c in csp_cols])
    if probs is None:
        probs = fallback
    if probs is None:
        result[is_adult] = "inconnu"
        return result.tolist()

    draw = rng.multinomial(n_adults, probs)
    labels = np.repeat(csp_cols, draw)
    rng.shuffle(labels)
    result[is_adult] = labels
    return result.tolist()


def _empty_agents(crs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        columns=["agent_id", "home_id", "age", "age_band", "csp", "activity",
                 "is_worker", "dest_id", "dest_x", "dest_y", "dist_m", "geometry"],
        geometry="geometry", crs=crs,
    )
