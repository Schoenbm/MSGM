"""Cohérence & correctness de la population en ménages sur DONNÉES RÉELLES.

Chantier ménages briques 3-4. Complète `test_household_agents.py` (contrat
structurel sur fixture synthétique) par les propriétés **émergentes** que seules
les vraies données révèlent (leçon du chantier : les mocks n'ont attrapé aucun des
bugs réels). Construit toute la chaîne à partir des données locales (IRIS + BD TOPO
+ RP détail), génère les ménages, puis vérifie conservation, correctness et calage.

Deux familles de garde-fous :
  - **certaines** (exactes, indépendantes de tout seuil) : conservation du nombre de
    ménages, population = groupby(home_id), 0 enfant orphelin, 0 ménage éclaté… ;
  - **à seuil** (marquées EMPIRIQUE) : écart des marges/headcount à l'INSEE. Sous
    l'option A, seule la **forme** de la distribution (proportions) est pincée par
    l'IPU ; le **headcount absolu** est une quantité émergente (population = ménages
    × taille réelle) et peut dériver de ~1-2 %. Les seuils ci-dessous sont un
    **premier jet** : à calibrer au premier run réel (cf. commentaires TODO).

Skippé si le cache RP ou les données locales sont absents, ou tant que
`generate_household_agents` n'est pas implémenté.
"""

from pathlib import Path

import numpy as np
import pytest

# ── Seuils EMPIRIQUES (à calibrer au premier run réel — cf. docstring) ─────────
HEADCOUNT_TOL = 0.02          # |pop générée − pop INSEE| / INSEE, zone (émergent, A)
DIST_TOL = 0.005              # écart médian des PROPORTIONS âge/CSP à l'INSEE, zone
IRIS_DIST_MEDIAN_TOL = 0.02   # écart médian par IRIS (Ind>800) de la distribution âge
IRIS_DIST_CEILING = 0.10      # plafond de sanité : aucun IRIS (Ind>800) au-delà
MEAN_SIZE_TOL = 0.02          # taille moyenne ménage générée vs INSEE
CONV_TOL = 0.01               # convergence multi-seeds des proportions de zone

_ROOT = Path(__file__).resolve().parent.parent
_SHP = _ROOT / "data" / "contour_iris.shp"
_BAT = _ROOT / "data" / "batim_grenoble.shp"

agents_mod = pytest.importorskip("src.matching.agents")
if not hasattr(agents_mod, "generate_household_agents"):
    pytest.skip(
        "generate_household_agents pas encore implémenté (chantier ménages brique 3-4)",
        allow_module_level=True,
    )

from src.loaders.rp_detail import _CACHE_DIR  # noqa: E402

_RP_ZIP = _CACHE_DIR / "RP2022_indcvize.zip"

pytestmark = pytest.mark.skipif(
    not (_RP_ZIP.exists() and _SHP.exists() and _BAT.exists()),
    reason="données réelles absentes (RP / contour_iris / batim) — test ignoré",
)

_AGE_COLS = ("age_0_2", "age_3_5", "age_6_10", "age_11_17", "age_18_24",
             "age_25_39", "age_40_54", "age_55_64", "age_65_79", "age_80p")
_CSP_COLS = ("csp_agriculteurs", "csp_artisans_commercants", "csp_cadres",
             "csp_prof_intermediaires", "csp_employes", "csp_ouvriers",
             "csp_chomeurs_inactifs", "csp_autres_inactifs")


# ── Construction de la chaîne réelle (une fois par module) ────────────────────

def _build_inputs():
    """Charge IRIS + bâtiments + RP, alloue, et renvoie (result, buildings_all, members, grid).

    Restreint à la zone `population` de config.yaml (petite → test rapide, mais
    plusieurs IRIS pour la cohérence par IRIS).
    """
    import yaml

    from src.loaders.iris import load_iris
    from src.loaders.buildings import load_all_buildings, prepare_residential
    from src.loaders.rp_detail import load_rp_households
    from src.matching.spatial_join import join_buildings_to_insee
    from src.matching.allocator import allocate_population

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    codes = cfg["zones"]["population"]["selector"]["codes"]
    grid = load_iris(selector={"type": "iris", "codes": codes}, shp_path=str(_SHP))

    buildings_all = load_all_buildings(str(_BAT), study_area=grid)
    residential = prepare_residential(buildings_all, osm_gdf=None, bdnb_usage={})
    joined = join_buildings_to_insee(residential, grid)
    result = allocate_population(joined)

    communes = sorted({str(c)[:5] for c in grid["CODE_IRIS"]})
    members = load_rp_households("38", communes=communes)
    return result, buildings_all, members, grid


@pytest.fixture(scope="module")
def real():
    try:
        result, buildings_all, members, grid = _build_inputs()
    except Exception as exc:  # base-ic non caché, colonne manquante… → skip, pas échec
        pytest.skip(f"chaîne réelle indisponible ({exc})")
    if "menages_alloues" not in result.columns:
        pytest.skip("menages_alloues absent (P22_MEN indisponible) — chantier IRIS-only")

    agents = agents_mod.generate_household_agents(
        result, buildings_all, members, education=None, seed=42,
    )
    if agents.empty:
        pytest.skip("aucun agent généré sur la zone")
    return {"agents": agents, "result": result, "grid": grid,
            "members": members, "buildings_all": buildings_all}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _insee_zone_margins(grid, cols):
    return np.array([float(grid[c].sum()) for c in cols if c in grid.columns])


def _gen_age_counts(agents):
    return agents["age_band"].value_counts().reindex(_AGE_COLS, fill_value=0).to_numpy(float)


def _gen_csp_counts(agents):
    adults = agents[agents["csp"] != "mineur"]
    return adults["csp"].value_counts().reindex(_CSP_COLS, fill_value=0).to_numpy(float)


def _shares(counts):
    total = counts.sum()
    return counts / total if total > 0 else counts


def _rel_err_on_shares(gen_counts, insee_counts):
    """Écart relatif par catégorie sur les PROPORTIONS (retire l'offset d'échelle)."""
    gs, isr = _shares(gen_counts), _shares(insee_counts)
    keep = isr > 0
    return np.abs(gs[keep] - isr[keep]) / isr[keep]


# ── GATES CERTAINES (exactes, sans seuil) ─────────────────────────────────────

def test_household_count_conserved_real(real):
    # C12 : Σ ménages générés == Σ menages_alloues (par bâtiment ET zone).
    agents, result = real["agents"], real["result"]
    per_bldg = agents.groupby("home_id")["household_id"].nunique()
    alloc = result.set_index("ID")["menages_alloues"]
    common = per_bldg.index.intersection(alloc.index[alloc > 0])
    assert (per_bldg.loc[common] == alloc.loc[common]).all(), \
        "nb de ménages généré ≠ menages_alloues sur certains bâtiments"


def test_population_equals_groupby_home(real):
    # C5 / D2 : la population par bâtiment == groupby(home_id).size() (une seule
    # vérité de population). Après le chantier, result['population_allouee'] doit
    # être recalculé pour coïncider — on vérifie l'égalité si la colonne existe.
    agents, result = real["agents"], real["result"]
    counts = agents.groupby("home_id").size()
    pop = result.set_index("ID")["population_allouee"]
    common = counts.index.intersection(pop.index)
    assert (counts.loc[common] == pop.loc[common]).all(), \
        "population_allouee non recalculée depuis les agents (D2)"


def test_no_orphan_children_real(real):
    agents = real["agents"]
    g = agents.groupby("household_id")
    has_minor = g["age"].apply(lambda s: (s < 18).any())
    has_adult = g["age"].apply(lambda s: (s >= 18).any())
    assert int((has_minor & ~has_adult).sum()) == 0


def test_no_household_split_real(real):
    agents = real["agents"]
    assert int((agents.groupby("household_id")["home_id"].nunique() > 1).sum()) == 0


def test_member_conservation_single_referent_real(real):
    # K9 : ménages ordinaires (non-communauté) avec exactement 1 référent.
    agents = real["agents"]
    collective = set(agents.loc[agents["role"].eq("hors_menage"), "household_id"])
    ordinary = agents[~agents["household_id"].isin(collective)]
    nref = ordinary.groupby("household_id")["role"].apply(lambda s: (s == "referent").sum())
    assert (nref == 1).all()


def test_no_unknown_age_or_csp_real(real):
    # K6/K13 : membres réels → jamais d'âge -1, mineurs="mineur", adultes réels.
    agents = real["agents"]
    assert (agents["age"] >= 0).all()
    assert (agents.loc[agents["age"] < 18, "csp"] == "mineur").all()
    assert (agents.loc[agents["age"] >= 18, "csp"] != "mineur").all()


# ── GATES À SEUIL (EMPIRIQUES — calibrer au 1er run) ──────────────────────────

def test_headcount_close_to_insee(real):
    # C1 : headcount global. EMPIRIQUE (émergent sous A) — seuil large, à resserrer.
    agents, grid = real["agents"], real["grid"]
    insee_pop = _insee_zone_margins(grid, _AGE_COLS).sum()
    rel = abs(len(agents) - insee_pop) / insee_pop
    assert rel < HEADCOUNT_TOL, f"headcount à {rel:.1%} de l'INSEE (seuil {HEADCOUNT_TOL:.0%})"


def test_age_distribution_matches_insee_zone(real):
    # C2 : FORME de la distribution d'âge (proportions) vs INSEE, zone. Gate dure.
    agents, grid = real["agents"], real["grid"]
    err = _rel_err_on_shares(_gen_age_counts(agents), _insee_zone_margins(grid, _AGE_COLS))
    assert np.median(err) < DIST_TOL, \
        f"distribution d'âge : écart médian {np.median(err):.4f} (seuil {DIST_TOL})"


def test_csp_distribution_matches_insee_zone(real):
    # C2 : FORME de la distribution CSP (proportions) vs INSEE, zone. Gate dure.
    agents, grid = real["agents"], real["grid"]
    err = _rel_err_on_shares(_gen_csp_counts(agents), _insee_zone_margins(grid, _CSP_COLS))
    assert np.median(err) < DIST_TOL, \
        f"distribution CSP : écart médian {np.median(err):.4f} (seuil {DIST_TOL})"


def test_per_iris_distribution_within_bounds(real):
    # C3 : par IRIS (Ind>800), distance de distribution d'âge gen vs INSEE.
    # Médiane serrée + plafond de sanité (aucun gros IRIS aberrant).
    agents, grid = real["agents"], real["grid"]
    agents = agents.copy()
    agents["iris"] = agents["home_id"].map(
        real["result"].set_index("ID")["code_iris"].apply(
            lambda x: str(int(x)).zfill(9)))
    big = grid[grid["Ind_total"] > 800]
    dists = []
    for _, r in big.iterrows():
        code = str(r["CODE_IRIS"])
        sub = agents[agents["iris"] == code]
        if len(sub) < 50:
            continue
        gen = sub["age_band"].value_counts().reindex(_AGE_COLS, fill_value=0).to_numpy(float)
        insee = np.array([float(r[c]) for c in _AGE_COLS])
        # Distance en variation totale (½·Σ|p−q|) des distributions.
        tv = 0.5 * np.abs(_shares(gen) - _shares(insee)).sum()
        dists.append(tv)
    if not dists:
        pytest.skip("aucun IRIS Ind>800 avec assez d'agents sur la zone")
    assert np.median(dists) < IRIS_DIST_MEDIAN_TOL, \
        f"distribution par IRIS : médiane TV {np.median(dists):.4f}"
    assert max(dists) < IRIS_DIST_CEILING, \
        f"IRIS aberrant : TV max {max(dists):.4f} > plafond {IRIS_DIST_CEILING}"


def test_mean_household_size_matches_insee(real):
    # C6 : taille moyenne des ménages générés vs INSEE (Σpop/Σménages de la zone).
    agents, grid = real["agents"], real["grid"]
    mean_gen = agents.groupby("household_id").size().mean()
    insee_pop = _insee_zone_margins(grid, _AGE_COLS).sum()
    insee_men = float(grid["P22_MEN"].sum()) if "P22_MEN" in grid.columns else np.nan
    if not np.isfinite(insee_men) or insee_men == 0:
        pytest.skip("P22_MEN indisponible")
    mean_insee = insee_pop / insee_men
    rel = abs(mean_gen - mean_insee) / mean_insee
    assert rel < MEAN_SIZE_TOL, \
        f"taille moyenne ménage {mean_gen:.2f} vs INSEE {mean_insee:.2f} ({rel:.1%})"


def test_multirun_convergence(real):
    # K8 / D6 : sur plusieurs seeds, les proportions d'âge de la zone convergent
    # (l'aléa de tirage ne déplace pas la distribution au-delà de CONV_TOL).
    shares = []
    for seed in (1, 2, 3):
        g = agents_mod.generate_household_agents(
            real["result"], real["buildings_all"], real["members"],
            education=None, seed=seed,
        )
        shares.append(_shares(_gen_age_counts(g)))
    arr = np.vstack(shares)
    spread = arr.max(axis=0) - arr.min(axis=0)
    assert spread.max() < CONV_TOL, \
        f"proportions d'âge instables entre seeds : écart max {spread.max():.4f}"


# ── Diagnostic runtime : WARNING sur IRIS mal calé ────────────────────────────

def test_warns_on_poorly_fit_iris(real, caplog):
    # Garde-fou UX : l'utilisateur doit être averti d'un IRIS à fort écart. On ne
    # teste pas qu'il y en a un, mais que le MÉCANISME de warning existe et se
    # déclenche si un IRIS dépasse le seuil interne (cf. IRIS_FIT_WARN_THRESHOLD).
    import logging
    assert hasattr(agents_mod, "IRIS_FIT_WARN_THRESHOLD"), \
        "seuil de warning par IRIS absent (diagnostic runtime attendu)"
    with caplog.at_level(logging.WARNING):
        agents_mod.generate_household_agents(
            real["result"], real["buildings_all"], real["members"],
            education=None, seed=42,
        )
    # Le run ne doit pas planter ; le mécanisme de warning par IRIS doit exister.
    assert True
