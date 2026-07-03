"""Tests de CONTRAT (structurels) de la génération de population en ménages.

Chantier ménages briques 3-4 : `matching/agents.generate_household_agents` descend
au grain individuel en **tirant des ménages réels** (pool RP repondéré par IPU),
au lieu de tirer des individus indépendants (`generate_agents`, ancien chemin).

Ces tests fixent le **contrat** que l'implémentation doit respecter (décisions
D1-D6 / V1-V5 verrouillées, cf. `claude.md` § « population en ménages »). Ils
tournent sur une **fixture synthétique** (rapide, hors réseau, sans cache) : ils
vérifient les invariants de structure, pas la qualité de calage sur le vrai fichier
(ça, c'est `test_households_realdata.py`).

Tant que `generate_household_agents` n'existe pas, le module **skippe** (il ne
casse pas la suite). Dès que la fonction est écrite, ces tests s'activent et
guident l'implémentation.
"""

import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

# Skip propre tant que le chantier n'est pas implémenté (test-first).
agents_mod = pytest.importorskip("src.matching.agents")
if not hasattr(agents_mod, "generate_household_agents"):
    pytest.skip(
        "generate_household_agents pas encore implémenté (chantier ménages brique 3-4)",
        allow_module_level=True,
    )

from src.matching.agents import generate_household_agents  # noqa: E402

CRS = "EPSG:2154"

# Colonnes attendues en sortie = schéma agents actuel + household_id + role (V4).
EXPECTED_COLS = {
    "agent_id", "home_id", "age", "age_band", "csp", "activity", "is_worker",
    "dest_id", "dest_x", "dest_y", "dist_m", "household_id", "role", "geometry",
}

ACTIVE_CSP = {"csp_agriculteurs", "csp_artisans_commercants", "csp_cadres",
              "csp_prof_intermediaires", "csp_employes", "csp_ouvriers"}


# ── Fixture synthétique ───────────────────────────────────────────────────────

def _square(cx: float, cy: float, r: float = 8.0) -> Polygon:
    """Petit carré centré (empreinte de bâtiment)."""
    return Polygon([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)])


def _members_pool() -> pd.DataFrame:
    """Pool de ménages réels miniature (schéma de load_rp_households).

    - hh A : couple (référent + conjoint) + 2 enfants (8 et 3 ans) — IRIS 101
    - hh B : retraité seul (70 ans)                                 — IRIS 101
    - hh C : ouvrier seul (30 ans)                                  — IRIS 102
    - Zc_1 : singleton de communauté (EHPAD, 85 ans)                — IRIS 101
    """
    rows = [
        # hh_id, iris, age, sexe, role, csp, is_minor, weight
        ("A", "381850101", 40, "1", "referent", "csp_cadres", False, 10.0),
        ("A", "381850101", 38, "2", "conjoint", "csp_employes", False, 10.0),
        ("A", "381850101", 8, "1", "enfant", "mineur", True, 10.0),
        ("A", "381850101", 3, "2", "enfant", "mineur", True, 10.0),
        ("B", "381850101", 70, "2", "referent", "csp_chomeurs_inactifs", False, 12.0),
        ("C", "381850102", 30, "1", "referent", "csp_ouvriers", False, 15.0),
        ("Zc_1", "381850101", 85, "2", "hors_menage", "csp_chomeurs_inactifs", False, 5.0),
    ]
    return pd.DataFrame(
        rows,
        columns=["hh_id", "iris", "age", "sexe", "role", "csp", "is_minor", "weight"],
    )


_CSP_COLS = ("csp_agriculteurs", "csp_artisans_commercants", "csp_cadres",
             "csp_prof_intermediaires", "csp_employes", "csp_ouvriers",
             "csp_chomeurs_inactifs", "csp_autres_inactifs")
_AGE_COLS = ("age_0_2", "age_3_5", "age_6_10", "age_11_17", "age_18_24",
             "age_25_39", "age_40_54", "age_55_64", "age_65_79", "age_80p")


def _residential():
    """Deux bâtiments résidentiels avec menages_alloues + marges âge/CSP par IRIS.

    Les colonnes age_*/csp_* portent les cibles (dérivées par la fonction en
    marges IRIS via groupby code_iris). Valeurs cohérentes avec le pool.
    """
    import geopandas as gpd

    base = {c: 0 for c in _AGE_COLS + _CSP_COLS}

    # IRIS 101 : ménages A(4) + B(1) + Zc(1) = 6 pers. Marges plausibles.
    b1 = dict(base)
    b1.update({"age_3_5": 1, "age_6_10": 1, "age_25_39": 1, "age_40_54": 1,
               "age_65_79": 1, "age_80p": 1,
               "csp_cadres": 1, "csp_employes": 1, "csp_chomeurs_inactifs": 2})
    # IRIS 102 : ménage C(1).
    b2 = dict(base)
    b2.update({"age_25_39": 1, "csp_ouvriers": 1})

    r1 = {"ID": "b1", "code_iris": 381850101.0, "menages_alloues": 3,
          "population_allouee": 6, "geometry": _square(0, 0), **b1}
    r2 = {"ID": "b2", "code_iris": 381850102.0, "menages_alloues": 2,
          "population_allouee": 2, "geometry": _square(500, 0), **b2}
    return gpd.GeoDataFrame([r1, r2], geometry="geometry", crs=CRS)


def _all_buildings():
    """Quelques lieux de travail (USAGE1) proches, pour l'affectation travail."""
    import geopandas as gpd

    rows = [
        {"ID": "w1", "USAGE1": "Commercial et services", "USAGE2": None,
         "geometry": _square(50, 50)},
        {"ID": "w2", "USAGE1": "Industriel", "USAGE2": None,
         "geometry": _square(520, 40)},
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _education():
    """Équipements BPE (un par niveau) proches des domiciles."""
    import geopandas as gpd

    rows = [
        {"equip_id": "e_creche", "kind": "creche", "capacity": 1.0, "geometry": Point(10, 10)},
        {"equip_id": "e_ecole", "kind": "ecole", "capacity": 1.0, "geometry": Point(20, 20)},
        {"equip_id": "e_college", "kind": "college", "capacity": 1.0, "geometry": Point(30, 30)},
        {"equip_id": "e_lycee", "kind": "lycee", "capacity": 1.0, "geometry": Point(40, 40)},
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


@pytest.fixture(scope="module")
def gen():
    """Génère la population de ménages une fois (seed fixe) pour le module."""
    return generate_household_agents(
        _residential(), _all_buildings(), _members_pool(),
        education=_education(), seed=42,
    )


# ── Schéma ────────────────────────────────────────────────────────────────────

def test_schema_has_household_columns(gen):
    # V4 : schéma agents actuel + household_id + role.
    assert EXPECTED_COLS.issubset(set(gen.columns)), (
        f"colonnes manquantes : {EXPECTED_COLS - set(gen.columns)}")


def test_crs_is_lambert93(gen):
    # K14 : CRS non négociable.
    assert gen.crs is not None and gen.crs.to_epsg() == 2154


# ── K10 / D1 / C12 : conservation du nombre de ménages ────────────────────────

def test_household_count_per_building_matches_menages_alloues(gen):
    # D1 : le nombre de ménages tirés par bâtiment == menages_alloues (exact).
    per_bldg = gen.groupby("home_id")["household_id"].nunique()
    assert per_bldg.get("b1") == 3
    assert per_bldg.get("b2") == 2


def test_total_household_count_conserved(gen):
    # C12 : Σ ménages générés == Σ menages_alloues (zone).
    assert gen["household_id"].nunique() == 5  # 3 + 2


# ── K2 / K11 : intégrité des ménages ──────────────────────────────────────────

def test_no_household_spans_two_buildings(gen):
    # K2 : chaque household_id est dans un seul bâtiment.
    spans = gen.groupby("household_id")["home_id"].nunique()
    assert int((spans > 1).sum()) == 0


def test_household_ids_are_globally_unique_per_draw(gen):
    # K11 : un même ménage du pool tiré dans 2 bâtiments donne 2 household_id
    # distincts. Chaque household_id est donc rattaché à un unique home_id.
    mapping = gen.groupby("household_id")["home_id"].nunique()
    assert (mapping == 1).all()


# ── K1 / K9 : lien parent→enfant + conservation des membres ───────────────────

def test_no_orphan_children(gen):
    # K1 : tout enfant/mineur partage son household_id avec au moins un adulte.
    g = gen.groupby("household_id")
    has_minor = g["age"].apply(lambda s: (s < 18).any())
    has_adult = g["age"].apply(lambda s: (s >= 18).any())
    assert int((has_minor & ~has_adult).sum()) == 0, "des enfants sans adulte dans leur ménage"


def test_children_have_parental_figure_role(gen):
    # K1 (renforcé) : un ménage avec un role=enfant a un referent ou conjoint.
    g = gen.groupby("household_id")
    has_child = g["role"].apply(lambda s: (s == "enfant").any())
    has_parent = g["role"].apply(lambda s: s.isin(["referent", "conjoint"]).any())
    assert int((has_child & ~has_parent).sum()) == 0


def test_ordinary_households_keep_single_referent(gen):
    # K9 : les membres réels ne sont pas perdus au tirage — un ménage ordinaire
    # garde son référent unique. Les singletons de communauté (role=hors_menage)
    # n'ont pas de référent et sont exclus du contrôle.
    collective_hh = set(gen.loc[gen["role"].eq("hors_menage"), "household_id"])
    ordinary = gen[~gen["household_id"].isin(collective_hh)]
    nref = ordinary.groupby("household_id")["role"].apply(lambda s: (s == "referent").sum())
    assert (nref == 1).all(), "membre perdu : ménage ordinaire sans référent unique"


# ── K13 : cohérence CSP mineurs / adultes ─────────────────────────────────────

def test_minors_have_mineur_csp(gen):
    minors = gen[gen["age"] < 18]
    assert (minors["csp"] == "mineur").all()


def test_adults_have_real_csp(gen):
    adults = gen[gen["age"] >= 18]
    assert (adults["csp"] != "mineur").all()


# ── K3 / K4 / K5 : activité adaptée à l'âge et à la destination ───────────────

def test_activity_matches_age_band(gen):
    # K3 : la tranche d'âge scolaire détermine l'activité.
    def expected(age, csp):
        if age <= 2:
            return "creche"
        if age <= 10:
            return "ecole"
        if age <= 14:
            return "college"
        if age <= 17:
            return "lycee"
        if 18 <= age <= 62 and csp in ACTIVE_CSP:
            return "travail"
        return "aucune"

    for _, a in gen.iterrows():
        exp = expected(int(a["age"]), a["csp"])
        assert a["activity"] == exp, (
            f"âge {a['age']} csp {a['csp']} → activité {a['activity']} (attendu {exp})")


def test_children_go_to_matching_education(gen):
    # K3 : un enfant a une destination du BON niveau (8 ans → école, pas lycée).
    edu = _education().set_index("equip_id")["kind"].to_dict()
    for _, a in gen.iterrows():
        if a["activity"] in ("creche", "ecole", "college", "lycee") and pd.notna(a["dest_id"]):
            assert edu.get(a["dest_id"]) == a["activity"], (
                f"enfant ({a['activity']}) envoyé vers {edu.get(a['dest_id'])}")


def test_retirees_have_no_work(gen):
    # K5 : les >62 ans ne travaillent pas.
    assert (gen[gen["age"] > 62]["activity"] != "travail").all()


def test_workers_are_working_age_active(gen):
    # K4 : activity=travail ⟹ 18-62 ans, CSP active.
    workers = gen[gen["activity"] == "travail"]
    assert workers["age"].between(18, 62).all()
    assert workers["csp"].isin(ACTIVE_CSP).all()


# ── K14 : géométrie ───────────────────────────────────────────────────────────

def test_home_point_inside_building_footprint(gen):
    foot = _residential().set_index("ID").geometry.to_dict()
    for _, a in gen.iterrows():
        poly = foot[a["home_id"]]
        assert poly.contains(a.geometry) or poly.touches(a.geometry)


# ── K7 / K10 / D6 : déterminisme (nb ménages) vs stochasticité (population) ────

def test_same_seed_is_reproducible():
    # K7 : même seed ⟹ sortie identique.
    kwargs = dict(education=_education(), seed=7)
    a = generate_household_agents(_residential(), _all_buildings(), _members_pool(), **kwargs)
    b = generate_household_agents(_residential(), _all_buildings(), _members_pool(), **kwargs)
    pd.testing.assert_frame_equal(
        a.drop(columns="geometry").reset_index(drop=True),
        b.drop(columns="geometry").reset_index(drop=True),
    )


def test_household_count_seed_independent(gen):
    # D1 + D6 : le NB de ménages/bâtiment ne dépend PAS du seed (== menages_alloues).
    # (La POPULATION/bâtiment peut, elle, varier avec le seed — vérifié sur données
    # réelles, K8 ; sur ce pool minuscule les tailles peuvent coïncider.)
    counts = []
    for seed in (1, 2, 3, 4, 5):
        g = generate_household_agents(_residential(), _all_buildings(), _members_pool(),
                                      education=_education(), seed=seed)
        counts.append(tuple(g.groupby("home_id")["household_id"].nunique().sort_index()))
    assert len(set(counts)) == 1, "le nombre de ménages/bâtiment dépend du seed (interdit)"
