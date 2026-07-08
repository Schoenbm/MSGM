"""Génération de la population d'agents individuels.

Descend de l'agrégat-par-bâtiment (sortie de `allocate_population`) à une liste
d'individus. Chaque agent possède :

    - un domicile        (home_id + point géométrique du bâtiment résidentiel)
    - un âge             (entier, tiré dans une tranche INSEE)
    - une CSP            (csp_* pour les 18+, "mineur" pour les moins de 18 ans)
    - une activité + une destination (dest_id) : travail / école / crèche / aucune

Deux chemins de génération :

`generate_household_agents` (chemin PRINCIPAL, chantier ménages briques 3-4) —
tirage de **ménages réels** (sample-based) : par bâtiment, `menages_alloues`
ménages sont tirés avec remise dans le pool RP détail (`loaders/rp_detail.py`)
repondéré par IPU sur les marges de l'IRIS (`matching/households.py`), et leurs
membres réels sont instanciés (âge/CSP/rôle observés). Les agents portent
`household_id` + `role` (lien parent→enfant exporté pour GAMA). Décisions
verrouillées D1-D6 / V1-V5 : cf. claude.md § « population en ménages ».

`generate_agents` (REPLI : mode Filosofi sans `menages_alloues`, ou pool RP
indisponible) — microsimulation par tirage d'individus indépendants (PAS un IPF) :
  1. Pour chaque bâtiment résidentiel, on tire `population_allouee` individus.
  2. L'âge est tiré dans les tranches `age_*` (multinomiale ∝ effectifs), puis un
     âge entier uniforme dans la tranche.
  3. La CSP des adultes (18+) est tirée dans les `csp_*` (multinomiale ∝ effectifs).
     Les mineurs reçoivent csp="mineur" et aucun lieu de travail.
  4. Les actifs occupés (CSP ∈ ACTIVE_CSP_COLS) reçoivent un lieu de travail.

ASSOMPTIONS À REVOIR (premier jet) :
- (repli seulement) Âge et CSP sont des marges INSEE *indépendantes* par bâtiment :
  on les tire séparément, on ne reconstruit pas la table jointe âge×CSP (ce que
  ferait un IPF / un échantillon comme gospl/Genstar). Les marges sont respectées
  en espérance, pas exactement (tirage aléatoire). Le chemin ménages n'a pas ce
  défaut : la table jointe âge×CSP×ménage est OBSERVÉE (ménages réels).
- Seuil adulte = 18 ans. Les 15-17 ans (comptés dans la pop CSP "15+" INSEE) sont
  donc traités comme mineurs sans CSP — léger écart documenté.
- Tranche 80+ bornée arbitrairement à [80, 99] (repli) ; les âges réels >99 du
  RP détail sont étiquetés `age_80p` en sortie (chemin ménages).
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd

from src.matching.schooling import assign_colleges_carte_scolaire
from src.matching.workplaces import (
    ACTIVE_CSP_COLS,
    DEFAULT_WORKPLACE_USAGES,
    assign_facilities,
    assign_workplaces_mobpro,
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

# Diagnostic runtime du calage IPU par IRIS (chemin ménages) : au-delà de cette
# part de la masse des marges INSEE mal placée après calage
# (Σ|fitted−target| / Σtarget sur les contraintes de cible > 0), un WARNING est
# loggé — l'utilisateur sait quels quartiers sont moins fiables.
IRIS_FIT_WARN_THRESHOLD = 0.05


def generate_agents(
    residential: gpd.GeoDataFrame,
    all_buildings: gpd.GeoDataFrame,
    education: "gpd.GeoDataFrame | None" = None,
    usages: "tuple[str, ...] | list[str]" = DEFAULT_WORKPLACE_USAGES,
    decay_m: float = 3000.0,
    education_decay_m: float = 1200.0,
    seed: int = 42,
    workplace_extra_ids: "set[str] | None" = None,
    commute_matrix: "pd.DataFrame | None" = None,
    carte_scolaire: "dict | None" = None,
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
        workplace_extra_ids: ID BD TOPO d'emploi récupérés via la BDNB (étendent
                     les lieux de travail au-delà des usages BD TOPO).
        commute_matrix: matrice MOBPRO P(c'|c) (cf. workplaces.build_commute_matrix).
                     Si fournie, le lieu de travail est tiré en 2 étapes calées
                     sur les flux réels (assign_workplaces_mobpro) ; sinon
                     gravité pure sur toute la région (chemin historique).
        carte_scolaire: sources de l'affectation collège par carte scolaire
                     (dict `secteurs`/`colleges`/`addresses`/`private_rate`,
                     cf. _assign_destinations). None → gravité pure (historique).

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

    home_communes = _home_communes(res) if commute_matrix is not None else None
    return _assign_destinations(agents, all_buildings, education, usages,
                                decay_m, education_decay_m, seed, workplace_extra_ids,
                                commute_matrix, home_communes, carte_scolaire)


def _assign_destinations(
    agents: gpd.GeoDataFrame,
    all_buildings: gpd.GeoDataFrame,
    education: "gpd.GeoDataFrame | None",
    usages: "tuple[str, ...] | list[str]",
    decay_m: float,
    education_decay_m: float,
    seed: int,
    workplace_extra_ids: "set[str] | None",
    commute_matrix: "pd.DataFrame | None" = None,
    home_communes: "pd.Series | None" = None,
    carte_scolaire: "dict | None" = None,
) -> gpd.GeoDataFrame:
    """Affecte les destinations par activité, chacune sur son propre jeu d'équipements.

    Partagé entre `generate_agents` et `generate_household_agents` (V5 : le tirage
    en ménages ne change rien à l'affectation des destinations). Enrichit `agents`
    des colonnes dest_id, dest_x, dest_y, dist_m.

    Travail : si `commute_matrix` (MOBPRO, P(c'|c)) et `home_communes` (ID bâtiment
    -> commune de résidence) sont fournis, affectation en 2 étapes calée sur les
    flux réels (assign_workplaces_mobpro) ; sinon gravité pure (repli global).

    Collège : si `carte_scolaire` est fourni et complet (clés `secteurs` — table
    des secteurs de recrutement —, `colleges` — établissements UAI public+privé
    de la zone —, `addresses` — adresses BAN des domiciles indexées par home_id —
    et `private_rate` optionnelle), l'affectation suit la carte scolaire
    officielle (assign_colleges_carte_scolaire) ; sinon gravité pure sur le
    sous-ensemble BPE (comportement historique). École/lycée/crèche inchangées.
    """
    crs = agents.crs
    agents["dest_id"] = pd.array([pd.NA] * len(agents), dtype="object")
    for col in ("dest_x", "dest_y", "dist_m"):
        agents[col] = np.nan

    workplaces = identify_workplaces(all_buildings, usages, extra_ids=workplace_extra_ids)
    use_mobpro = commute_matrix is not None and len(commute_matrix) > 0
    if use_mobpro and home_communes is None:
        logger.warning("Matrice MOBPRO fournie mais communes des domiciles inconnues "
                       "(code_iris/ID absents) — affectation travail par gravité pure")
        use_mobpro = False
    if use_mobpro and not workplaces.empty:
        # Commune d'un lieu de travail = code_iris[:5]. Sur buildings_all, le
        # code_iris vient du shapefile BD TOPO (lacunaire, parfois float) : les
        # bâtiments sans code restent tirables au repli global (D3) mais pas à
        # l'étape intra-commune.
        wp_iris = (workplaces["code_iris"] if "code_iris" in workplaces.columns
                   else pd.Series(pd.NA, index=workplaces.index))
        workplaces["commune"] = wp_iris.map(_commune_code)

    # Carte scolaire collège : active seulement si TOUTES les sources sont là
    # (secteurs + établissements UAI + adresses BAN) ; sinon comportement
    # historique (gravité pure sur le sous-ensemble BPE).
    use_carte = carte_scolaire is not None and all(
        carte_scolaire.get(k) is not None and len(carte_scolaire[k]) > 0
        for k in ("secteurs", "colleges", "addresses")
    )
    college_facilities = (carte_scolaire["colleges"] if use_carte
                          else _education_subset(education, ACT_COLLEGE, crs,
                                                 fallback=ACT_ECOLE))

    # Collège/lycée retombent sur le pool "école" si leur sous-ensemble BPE est
    # vide (sécurité ; en pratique BPE distingue les niveaux par TYPEQU).
    plans = [
        (ACT_TRAVAIL, workplaces, "ID", "lieu de travail", decay_m),
        (ACT_CRECHE, _education_subset(education, ACT_CRECHE, crs), "equip_id", "crèche", education_decay_m),
        (ACT_ECOLE, _education_subset(education, ACT_ECOLE, crs), "equip_id", "école", education_decay_m),
        (ACT_COLLEGE, college_facilities, "equip_id", "collège", education_decay_m),
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
        if activity == ACT_TRAVAIL and use_mobpro:
            workers = sub.copy()
            workers["commune"] = workers["home_id"].map(home_communes)
            assigned = assign_workplaces_mobpro(workers, facilities, commute_matrix,
                                                decay_m=decay, seed=seed)
        elif activity == ACT_COLLEGE and use_carte:
            assigned = assign_colleges_carte_scolaire(
                sub, carte_scolaire["addresses"], carte_scolaire["secteurs"],
                facilities, decay_m=decay,
                private_rate=carte_scolaire.get("private_rate", 0.20), seed=seed)
        else:
            assigned = assign_facilities(sub, facilities, decay_m=decay, seed=seed,
                                         id_col=id_col, label=label)
        for col in ("dest_id", "dest_x", "dest_y", "dist_m"):
            agents.loc[sub.index, col] = assigned[col].values

    return agents


def generate_household_agents(
    residential: gpd.GeoDataFrame,
    all_buildings: gpd.GeoDataFrame,
    members: pd.DataFrame,
    education: "gpd.GeoDataFrame | None" = None,
    usages: "tuple[str, ...] | list[str]" = DEFAULT_WORKPLACE_USAGES,
    decay_m: float = 3000.0,
    education_decay_m: float = 1200.0,
    seed: int = 42,
    workplace_extra_ids: "set[str] | None" = None,
    commute_matrix: "pd.DataFrame | None" = None,
    carte_scolaire: "dict | None" = None,
) -> gpd.GeoDataFrame:
    """Génère la population en tirant des MÉNAGES RÉELS (sample-based, briques 3-4).

    Par bâtiment résidentiel, tire exactement `menages_alloues` ménages (D1) **avec
    remise** dans le pool RP détail entier (V2), repondéré par IPU ménage×individu
    (V1 : un calage par IRIS) sur les marges `age_*`/`csp_*` de l'IRIS + la
    **composition des ménages** (`men_*` ← C22_MEN*, pince la distribution des
    tailles) + un **tilt final de taille moyenne** (population/ménages garanti même
    quand les cibles INSEE se contredisent — cf. households.weights_for), puis
    instancie leurs membres réels (âge, CSP, rôle LPRM observés → lien
    parent→enfant exporté, V5). La population par bâtiment devient stochastique
    (vraies tailles de ménages) mais reproductible à seed fixé ; le nombre de
    ménages, lui, est déterministe (D6).

    Replis (D5) : IRIS aux poids IPU dégénérés → tirage aux poids IPONDI bruts du
    pool départemental (structure familiale préservée) ; pool RP vide ou
    `menages_alloues` absent (mode Filosofi) → `generate_agents` (individus
    indépendants, household_id/role vides).

    ⚠️ Effet de bord voulu (D2) : `residential["population_allouee"]` est RECALCULÉ
    EN PLACE depuis les agents (`groupby(home_id).size()`) — une seule vérité de
    population, buildings.* et agents.* d'accord. Les colonnes `menages_alloues` et
    `age_*`/`csp_*` ne sont pas touchées (pour D3, cf. update_building_demographics).

    Args:
        residential: bâtiments résidentiels avec `menages_alloues`, `code_iris`,
                     `age_*`/`csp_*` (sortie de allocate_population --source iris),
                     Lambert-93, colonne `ID`. Modifié en place (D2, cf. ci-dessus).
        all_buildings: tous les bâtiments (lieux de travail), cf. generate_agents.
        members:     pool de membres de ménages réels (sortie de
                     loaders.rp_detail.load_rp_households).
        education:   équipements éducatifs BPE (cf. generate_agents).
        usages:      valeurs USAGE retenues comme lieux de travail.
        decay_m:     échelle de décroissance distance pour le travail (m).
        education_decay_m: idem pour école/crèche.
        seed:        graine du tirage (reproductibilité).
        workplace_extra_ids: ID BD TOPO d'emploi récupérés via la BDNB.
        commute_matrix: matrice MOBPRO P(c'|c) — travail affecté en 2 étapes
                     calées sur les flux réels si fournie (cf. generate_agents).
        carte_scolaire: sources de l'affectation collège par carte scolaire
                     (cf. generate_agents / _assign_destinations).

    Returns:
        GeoDataFrame, une ligne par agent. Colonnes de generate_agents +
        `household_id` (int, unique PAR TIRAGE : un ménage du pool tiré deux fois
        donne deux ids, V4) + `role` (referent/conjoint/enfant/…, LPRM).
    """
    # Import local : households.py importe AGE_BANDS/ALL_CSP_COLS de ce module —
    # un import module-level serait circulaire.
    from src.matching.households import (
        AGE_COLS,
        CSP_COLS,
        HH_TYPE_COLS,
        HouseholdReweighter,
    )

    if members is None or len(members) == 0:
        return _fallback_individual(residential, all_buildings, education, usages,
                                    decay_m, education_decay_m, seed,
                                    workplace_extra_ids, commute_matrix,
                                    carte_scolaire, "pool RP vide")
    if "menages_alloues" not in residential.columns:
        return _fallback_individual(residential, all_buildings, education, usages,
                                    decay_m, education_decay_m, seed,
                                    workplace_extra_ids, commute_matrix,
                                    carte_scolaire,
                                    "menages_alloues absent (mode Filosofi ?)")

    pool = _sanitize_pool(members)
    if pool.empty:
        return _fallback_individual(residential, all_buildings, education, usages,
                                    decay_m, education_decay_m, seed,
                                    workplace_extra_ids, commute_matrix,
                                    carte_scolaire, "pool RP vide après garde-fous")

    crs = residential.crs
    res = residential.loc[residential["menages_alloues"].fillna(0) > 0].copy()
    if res.empty:
        logger.warning("Aucun bâtiment avec menages_alloues > 0 — population vide")
        residential["population_allouee"] = 0  # D2 : la vérité vient des agents
        return _empty_household_agents(crs)
    res["_iris"] = res["code_iris"].map(_iris_code)

    # Marges IRIS = somme des colonnes bâtiment (l'allocateur conserve exactement
    # les totaux IRIS par plus fort reste → le groupby reconstitue la base-ic).
    age_cols = [c for c in AGE_COLS if c in res.columns]
    csp_cols = [c for c in CSP_COLS if c in res.columns]
    targets = res.groupby("_iris")[age_cols + csp_cols].sum()

    # Marges de COMPOSITION des ménages (C22_MEN* → men_*, valeurs de cellule
    # forwardées par spatial_join, constantes par IRIS → first). Elles pincent la
    # distribution des TAILLES de ménage dans l'IPU (Ye et al. 2009) ; sans elles,
    # seule la contrainte du nombre total s'applique (taille non pincée : +31 %
    # local mesuré). Somme nulle / colonnes absentes = indisponible → repli.
    hh_type_cols = [c for c in HH_TYPE_COLS if c in res.columns]
    hh_type_by_iris = res.groupby("_iris")[hh_type_cols].first() if hh_type_cols else None
    if hh_type_by_iris is None:
        logger.info("Marges de composition des ménages absentes — l'IPU ne contraint "
                    "que le NOMBRE de ménages (taille non pincée)")

    reweighter = HouseholdReweighter(pool)
    # Bornes des membres de chaque ménage dans le pool trié par hh_id (contigu).
    # factorize(sort=True) suit le même ordre que reweighter.hh_ids (sorted unique).
    codes, uniques = pd.factorize(pool["hh_id"], sort=True)
    if not np.array_equal(np.asarray(uniques), reweighter.hh_ids):
        raise RuntimeError("Pool de ménages désaligné avec le reweighter (ordre hh_id)")
    hh_sizes = np.bincount(codes)
    hh_starts = np.concatenate(([0], np.cumsum(hh_sizes)[:-1]))
    m_age = pool["age"].to_numpy(dtype=int)
    m_csp = pool["csp"].to_numpy(dtype=object)
    m_role = pool["role"].to_numpy(dtype=object)

    centroids = res.geometry.centroid
    res_x = centroids.x.to_numpy()
    res_y = centroids.y.to_numpy()
    res_ids = res["ID"].to_numpy()
    res_n = res["menages_alloues"].round().astype(int).to_numpy()
    res_iris = res["_iris"].to_numpy()

    rng = np.random.default_rng(seed)
    p_rows: list[np.ndarray] = []      # index des lignes membres dans le pool
    p_home: list[np.ndarray] = []
    p_hh: list[np.ndarray] = []
    p_x: list[np.ndarray] = []
    p_y: list[np.ndarray] = []
    hh_counter = 0
    n_iris_warned = 0
    worst_size = ("-", 0.0, 0.0, 0.0)  # (iris, écart relatif, attendu, cible)

    for iris_code in targets.index:  # ordre trié (groupby) → déterministe
        mask = res_iris == iris_code
        n_per_building = res_n[mask]
        total = int(n_per_building.sum())
        if total == 0:
            continue

        # Cibles de composition rescalées : Σ classes = nb de ménages tirés
        # (= P22_MEN). Le niveau C22 (exploitation complémentaire) peut différer
        # de P22_MEN (mesuré jusqu'à +38 % sur un petit IRIS) — on garde les
        # PROPORTIONS C22 au niveau P22.
        hh_t = None
        if hh_type_by_iris is not None:
            raw = hh_type_by_iris.loc[iris_code]
            raw_sum = float(raw.sum())
            if raw_sum > 0:
                hh_t = raw * (total / raw_sum)

        w = _iris_weights(reweighter, targets.loc[iris_code, age_cols],
                          targets.loc[iris_code, csp_cols], hh_t, total, iris_code)
        if w is None:
            n_iris_warned += 1
            w = np.where(np.isfinite(reweighter.init_weight), reweighter.init_weight, 0.0)
            if w.sum() <= 0:
                w = np.ones(len(reweighter.hh_ids))

        # Diagnostic taille : taille moyenne attendue du tirage (pondérée) vs
        # cible INSEE de l'IRIS (population / ménages). Le pire IRIS est loggé.
        exp_size = float((w * hh_sizes).sum() / w.sum())
        target_size = float(targets.loc[iris_code, age_cols].sum()) / total
        if target_size > 0:
            rel = abs(exp_size - target_size) / target_size
            if rel > worst_size[1]:
                worst_size = (iris_code, rel, exp_size, target_size)

        drawn = rng.choice(len(reweighter.hh_ids), size=total, p=w / w.sum())
        sizes = hh_sizes[drawn]
        n_members = int(sizes.sum())
        # Lignes membres du pool pour chaque ménage tiré (blocs contigus).
        offsets = np.arange(n_members) - np.repeat(np.cumsum(sizes) - sizes, sizes)
        p_rows.append(np.repeat(hh_starts[drawn], sizes) + offsets)
        p_home.append(np.repeat(np.repeat(res_ids[mask], n_per_building), sizes))
        p_x.append(np.repeat(np.repeat(res_x[mask], n_per_building), sizes))
        p_y.append(np.repeat(np.repeat(res_y[mask], n_per_building), sizes))
        # household_id unique PAR TIRAGE (V4) : compteur global, jamais réutilisé.
        p_hh.append(np.repeat(hh_counter + np.arange(total), sizes))
        hh_counter += total

    if not p_rows:
        residential["population_allouee"] = 0  # D2
        return _empty_household_agents(crs)

    rows = np.concatenate(p_rows)
    ages = m_age[rows]
    agents = gpd.GeoDataFrame(
        {
            "agent_id": np.arange(len(rows)),
            "home_id": np.concatenate(p_home),
            "household_id": np.concatenate(p_hh),
            "role": m_role[rows],
            "age": ages,
            "age_band": _bands_from_ages(ages),
            "csp": m_csp[rows],
        },
        geometry=gpd.points_from_xy(np.concatenate(p_x), np.concatenate(p_y)),
        crs=crs,
    )
    agents["activity"] = _classify_activity(agents)
    agents["is_worker"] = agents["activity"] == ACT_TRAVAIL

    logger.info(
        "Agents (ménages réels) : %d individus dans %d ménages (taille moyenne %.2f)"
        "  |  âge moyen %.1f  |  activités : %s",
        len(agents), hh_counter, len(agents) / hh_counter,
        agents["age"].mean(), agents["activity"].value_counts().to_dict(),
    )
    if n_iris_warned:
        logger.warning("%d IRIS tirés en repli départemental (poids IPONDI bruts, D5)",
                       n_iris_warned)
    if worst_size[0] != "-":
        logger.info(
            "Taille moyenne de ménage (attendue vs cible INSEE) — pire IRIS %s : "
            "%.3f vs %.3f (%+.1f %%)",
            worst_size[0], worst_size[2], worst_size[3],
            100.0 * (worst_size[2] - worst_size[3]) / worst_size[3],
        )

    home_communes = _home_communes(res) if commute_matrix is not None else None
    agents = _assign_destinations(agents, all_buildings, education, usages,
                                  decay_m, education_decay_m, seed, workplace_extra_ids,
                                  commute_matrix, home_communes, carte_scolaire)

    # D2 : une seule vérité de population — population_allouee recalculé EN PLACE
    # depuis les agents. casualties.py (lecteur de population_allouee) reste juste.
    old_total = (int(residential["population_allouee"].fillna(0).sum())
                 if "population_allouee" in residential.columns else 0)
    counts = agents.groupby("home_id").size()
    residential["population_allouee"] = residential["ID"].map(counts).fillna(0).astype(int)
    new_total = int(residential["population_allouee"].sum())
    # NB : pas de '→' (U+2192) dans les logs — console Windows cp1252.
    logger.info(
        "D2 : population_allouee recalculée depuis les agents : %d -> %d (%+.2f %%)",
        old_total, new_total,
        100.0 * (new_total - old_total) / old_total if old_total else 0.0,
    )
    return agents


def update_building_demographics(result: gpd.GeoDataFrame, agents: gpd.GeoDataFrame) -> None:
    """D3 : recale les colonnes `age_*`/`csp_*` par bâtiment sur les AGENTS générés.

    Les agents sont la vérité primaire ; la version allocateur (plus fort reste,
    marges indépendantes) est CONSERVÉE suffixée `_alloc` pour mesurer l'écart.
    Modifie `result` en place ; idempotent (les `_alloc` ne sont copiées qu'à la
    première application). À appeler après `generate_household_agents`, avant export.
    """
    age_cols = [c for c in AGE_BANDS if c in result.columns]
    csp_cols = [c for c in ALL_CSP_COLS if c in result.columns]
    if not age_cols and not csp_cols:
        return
    for col in age_cols + csp_cols:
        if f"{col}_alloc" not in result.columns:
            result[f"{col}_alloc"] = result[col]

    empty = pd.DataFrame()
    age_ct = pd.crosstab(agents["home_id"], agents["age_band"]) if len(agents) else empty
    # Le crosstab CSP contient une colonne "mineur" (hors csp_*) — ignorée d'office.
    csp_ct = pd.crosstab(agents["home_id"], agents["csp"]) if len(agents) else empty

    def _recount(ct: pd.DataFrame, col: str) -> pd.Series:
        if col in ct.columns:
            return result["ID"].map(ct[col]).fillna(0).astype(int)
        return pd.Series(0, index=result.index, dtype=int)

    for col in age_cols:
        result[col] = _recount(age_ct, col)
    for col in csp_cols:
        result[col] = _recount(csp_ct, col)

    for label, cols in (("âge", age_cols), ("CSP", csp_cols)):
        alloc_sum = float(sum(result[f"{c}_alloc"].sum() for c in cols)) if cols else 0.0
        if alloc_sum > 0:
            gap = sum(float((result[c] - result[f"{c}_alloc"]).abs().sum()) for c in cols)
            logger.info(
                "D3 : marges %s par bâtiment recalculées depuis les agents — écart "
                "vs allocateur : %.1f %% de la masse (colonnes *_alloc conservées)",
                label, 100.0 * gap / alloc_sum,
            )


def _iris_weights(reweighter, age_targets: pd.Series, csp_targets: pd.Series,
                  hh_type_targets: "pd.Series | None", n_households: int,
                  iris_code: str) -> "np.ndarray | None":
    """Poids IPU du pool pour un IRIS ; None si dégénérés (→ repli D5 chez l'appelant).

    Le calage inclut des contraintes de NIVEAU MÉNAGE (Ye et al. 2009) : la
    composition (`hh_type_targets`, C22_MEN* rescalées à Σ = menages_alloues =
    P22_MEN) pince le nombre ET la structure des tailles de ménage. Sans marges
    individus, le nombre implicite de ménages dérive (+9 à +20 % mesurés →
    population −15 %) ; avec le seul nombre total (`n_households`, repli quand la
    composition manque), la taille dérive encore localement (+31 % mesuré, IRIS
    381510102). Logge un WARNING (diagnostic runtime) quand le calage laisse plus
    de IRIS_FIT_WARN_THRESHOLD de la masse des marges (individus + composition)
    mal placée."""
    from src.matching.households import AGE_COLS, CSP_COLS, HH_TYPE_COLS

    # n_iter=300 : avec les contraintes ménage (jusqu'à 23 contraintes), 80
    # itérations laissent un biais résiduel (−0,29 % de population attendue mesuré
    # sur la zone test) ; 300 le ramènent à −0,08 %. Coût : ~ms par IRIS (types).
    # mean_size_target = population / ménages de l'IRIS : tilt final qui garantit
    # la taille moyenne (donc la population tirée) même quand âge×CSP×composition
    # sont mutuellement incohérents (cf. households.weights_for).
    total_persons = float(sum(float(age_targets.get(c, 0.0)) for c in AGE_COLS))
    mean_size = (total_persons / n_households) if (n_households and total_persons > 0) else None
    w = reweighter.weights_for(
        age_targets, csp_targets,
        hh_type_targets=hh_type_targets,
        n_households=None if hh_type_targets is not None else n_households,
        mean_size_target=mean_size,
        n_iter=300,
    )
    if not np.all(np.isfinite(w)) or float(w.sum()) <= 0.0:
        logger.warning("IRIS %s : IPU inutilisable (poids dégénérés) — tirage en "
                       "repli départemental, poids IPONDI bruts (D5)", iris_code)
        return None

    hh_t = hh_type_targets if hh_type_targets is not None else {}
    tvec = np.array([float(age_targets.get(c, 0.0)) for c in AGE_COLS]
                    + [float(csp_targets.get(c, 0.0)) for c in CSP_COLS]
                    + [float(hh_t.get(c, 0.0)) for c in HH_TYPE_COLS])
    keep = tvec > 0
    if keep.any():
        fitted = np.concatenate([w @ reweighter.A, w @ reweighter.type_matrix])
        misfit = float(np.abs(fitted[keep] - tvec[keep]).sum() / tvec[keep].sum())
        if misfit > IRIS_FIT_WARN_THRESHOLD:
            logger.warning(
                "IRIS %s : calage IPU imparfait — %.1f %% de la masse des marges mal "
                "placée (seuil %.0f %%) ; quartier moins fiable",
                iris_code, 100.0 * misfit, 100.0 * IRIS_FIT_WARN_THRESHOLD,
            )
    return w


def _sanitize_pool(members: pd.DataFrame) -> pd.DataFrame:
    """Garde-fous d'intégrité du pool avant tirage (invariants K1/K6/K9 des tests).

    Écarte les rares ménages du RP détail qui violeraient les invariants de la
    population générée (volumes ~0,1 ‰, loggés ; les marges IRIS — base-ic —
    ne bougent pas) :
      - membre d'âge inconnu (AGED manquant → -1 au chargement) ;
      - membre HORS TRANCHES d'âge (> 99 ans, ~150 centenaires sur le dép.) : ces
        membres ne contribuent à AUCUNE contrainte d'âge (piège verrouillé : ne
        pas les rabattre sur age_80p) → direction dégénérée que l'IPU exploite
        pour gonfler les tailles sans payer les marges. Mesuré (IRIS 381510103,
        avec contraintes de composition) : 361 personnes pondérées « invisibles »
        = taille moyenne attendue +9,2 % alors que TOUTES les contraintes sont
        exactes. Les centenaires disparaissent de la population générée (~0,01 %,
        assumé) ;
      - ménage sans adulte (mineur seul en logement, ou singleton de communauté
        type internat) : le tirer produirait un « enfant orphelin » ;
      - ménage ordinaire sans référent unique (29 cas mesurés sur l'Isère).
    Retourne le pool trié par hh_id (blocs membres contigus pour le tirage)."""
    m = members
    by_hh = m.groupby("hh_id", sort=False)
    ok_age = (by_hh["age"].min() >= 0) & (by_hh["age"].max() <= 99)
    has_adult = by_hh["age"].max() >= ADULT_MIN_AGE
    is_collective = by_hh["role"].first() == "hors_menage"  # singletons Zc_*
    one_ref = m["role"].eq("referent").groupby(m["hh_id"], sort=False).sum() == 1
    ok = ok_age & has_adult & (is_collective | one_ref)

    n_drop = int((~ok).sum())
    if n_drop:
        logger.info(
            "Pool ménages : %d ménage(s) écarté(s) avant tirage (âge inconnu ou "
            ">99 : %d, sans adulte %d, référent non unique %d)",
            n_drop, int((~ok_age).sum()), int((ok_age & ~has_adult).sum()),
            int((ok_age & has_adult & ~(is_collective | one_ref)).sum()),
        )
    kept = m.loc[m["hh_id"].map(ok)]
    return kept.sort_values("hh_id", kind="stable").reset_index(drop=True)


def _fallback_individual(residential, all_buildings, education, usages, decay_m,
                         education_decay_m, seed, workplace_extra_ids,
                         commute_matrix, carte_scolaire, reason: str) -> gpd.GeoDataFrame:
    """Dernier recours (D5/V3) : ancien tirage d'individus, schéma ménages conservé."""
    logger.warning("Tirage en ménages impossible (%s) — repli sur le tirage "
                   "d'individus indépendants (generate_agents)", reason)
    agents = generate_agents(residential, all_buildings, education=education,
                             usages=usages, decay_m=decay_m,
                             education_decay_m=education_decay_m, seed=seed,
                             workplace_extra_ids=workplace_extra_ids,
                             commute_matrix=commute_matrix,
                             carte_scolaire=carte_scolaire)
    agents["household_id"] = pd.array([pd.NA] * len(agents), dtype="object")
    agents["role"] = pd.array([pd.NA] * len(agents), dtype="object")
    return agents


def _iris_code(x) -> str:
    """Code IRIS 9 caractères depuis `code_iris` (float en sortie d'allocation)."""
    return str(int(float(x))).zfill(9)


def _commune_code(x) -> "str | None":
    """Commune (5 car.) depuis le `code_iris` d'un bâtiment — TOLÉRANT.

    Contrairement à `_iris_code` (allocation, code garanti), le code_iris de
    `buildings_all` vient du shapefile BD TOPO : lacunaire (NaN) et parfois float.
    None si inconnu (le bâtiment reste au repli global D3 côté MOBPRO).
    """
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    try:
        return _iris_code(x)[:5]
    except (TypeError, ValueError):
        s = str(x).strip()
        return s[:5] if len(s) >= 5 else None


def _home_communes(res: gpd.GeoDataFrame) -> "pd.Series | None":
    """Commune de résidence par bâtiment (ID -> code_iris[:5]).

    Côté « workers » de l'affectation MOBPRO (assign_workplaces_mobpro). None si
    code_iris/ID indisponibles — l'appelant retombe en gravité pure.
    """
    if "code_iris" not in res.columns or "ID" not in res.columns:
        return None
    return pd.Series(res["code_iris"].map(_commune_code).to_numpy(),
                     index=res["ID"].to_numpy())


def _bands_from_ages(ages: np.ndarray) -> np.ndarray:
    """Tranche INSEE de chaque âge (sortie agents). Les >99 (centenaires, hors
    tranches base-ic) sont étiquetés `age_80p` EN SORTIE seulement — l'IPU garde
    ses tranches strictes (piège « ne pas clipper », cf. households.py)."""
    bands = np.full(len(ages), "age_80p", dtype=object)
    for name, (lo, hi) in AGE_BANDS.items():
        bands[(ages >= lo) & (ages <= hi)] = name
    return bands


def _empty_household_agents(crs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        columns=["agent_id", "home_id", "household_id", "role", "age", "age_band",
                 "csp", "activity", "is_worker", "dest_id", "dest_x", "dest_y",
                 "dist_m", "geometry"],
        geometry="geometry", crs=crs,
    )


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
    """Filtre les équipements éducatifs par type et garantit une colonne capacity.

    La capacité d'accueil BPE n'est pas renseignée pour l'enseignement (vérifié :
    0/1463 équipements en Isère) : `bpe.py` retombe sur 1.0, chaque équipement pèse
    1 → l'affectation est dominée par la proximité (les enfants vont au plus proche,
    à l'aléa près). Le repli `capacity=1.0` ci-dessous ne sert qu'à une entrée hors
    BPE (sans colonne capacity). Si le type demandé est vide et qu'un `fallback` est
    fourni (collège/lycée → pool "école"), on bascule dessus.
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
        sub["capacity"] = 1.0  # entrée hors BPE sans capacité → proximité dominante
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
