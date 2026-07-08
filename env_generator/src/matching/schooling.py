"""Affectation collège calée sur la carte scolaire officielle (chantier 2026-07).

Remplace la gravité pure pour le niveau COLLÈGE uniquement : en France,
l'affectation en collège public suit une carte scolaire (secteur de recrutement
par adresse — la RUE, pas l'IRIS), pas la proximité. Lycées et écoles primaires
restent en gravité pure (aucune carte scolaire ouverte pour ces niveaux en
Isère/académie de Grenoble — dataset national collèges publics uniquement).

Logique (décisions verrouillées, cf. METHODE.md) :
  - statut public/privé tiré PAR MÉNAGE (Bernoulli, `private_rate=0.20`, taux
    national DEPP — pas de taux communal disponible) : les collégiens d'un même
    ménage partagent le statut (des frères et sœurs vont dans le même secteur,
    pas chacun tiré séparément). Repli par agent si `household_id` absent
    (chemin individus indépendants) ;
  - PRIVÉ  → gravité restreinte aux collèges `secteur == "Privé"` (repli gravité
    globale si aucun collège privé dans la zone) ;
  - PUBLIC → résolution DÉTERMINISTE par la carte scolaire (commune + rue +
    numéro + parité) via l'adresse BAN du bâtiment domicile ; tout échec de
    résolution (pas d'adresse à moins de `max_dist_m`, rue inconnue, numéro
    hors plage / parité incompatible, `code_rne` hors zone d'étude) → repli
    gravité restreinte aux collèges `secteur == "Public"` (ou globale si vide).

Même contrat de sortie que `workplaces.assign_facilities` (dest_id, dest_x,
dest_y, dist_m), même esprit 2 étapes que `assign_workplaces_mobpro`.
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd

from src.matching.workplaces import assign_facilities

logger = logging.getLogger(__name__)

# Distance max de rattachement bâtiment résidentiel → adresse BAN (constante).
ADDRESS_MAX_DIST_M = 50.0


def attach_building_addresses(
    buildings: gpd.GeoDataFrame,
    ban_addresses: gpd.GeoDataFrame,
    max_dist_m: float = ADDRESS_MAX_DIST_M,
) -> pd.DataFrame:
    """Rattache chaque bâtiment à l'adresse BAN la plus proche (≤ max_dist_m).

    Jointure spatiale plus-proche-voisin ; un bâtiment sans adresse à moins de
    `max_dist_m` garde des colonnes NaN (il retombera en gravité au moment de
    l'affectation collège).

    Args:
        buildings:     bâtiments résidentiels (`ID`, géométrie, Lambert-93).
        ban_addresses: points adresse BAN (`code_insee`, `nom_voie_norm`,
                       `numero`, géométrie — cf. loaders.ban).
        max_dist_m:    rayon de rattachement (m).

    Returns:
        DataFrame : ID, code_insee, nom_voie_norm, numero (NaN si aucune
        adresse dans le rayon).
    """
    left = buildings[["ID", "geometry"]].copy()
    addr_cols = ["code_insee", "nom_voie_norm", "numero"]
    if ban_addresses is None or ban_addresses.empty:
        logger.warning("Aucune adresse BAN — aucun bâtiment rattaché")
        out = pd.DataFrame(left.drop(columns="geometry"))
        for c in addr_cols:
            out[c] = np.nan
        return out

    right = ban_addresses[addr_cols + ["geometry"]]
    if right.crs is not None and left.crs is not None and right.crs != left.crs:
        right = right.to_crs(left.crs)

    joined = gpd.sjoin_nearest(left, right, how="left", max_distance=max_dist_m)
    # Ties (deux adresses exactement à la même distance) → doublons : on en
    # garde une seule par bâtiment.
    joined = joined[~joined.index.duplicated(keep="first")]
    out = pd.DataFrame(joined[["ID"] + addr_cols])

    n_matched = int(out["nom_voie_norm"].notna().sum())
    logger.info("Adresses BAN rattachées : %d/%d bâtiments (rayon %.0f m)",
                n_matched, len(out), max_dist_m)
    return out


def resolve_college_sector(
    code_insee,
    nom_voie_norm,
    numero,
    secteurs: pd.DataFrame,
) -> "str | None":
    """Résout le collège public de secteur d'une adresse (déterministe).

    Args:
        code_insee:    commune de l'adresse (5 caractères).
        nom_voie_norm: nom de voie normalisé (cf. text_utils.normalize_voie).
        numero:        numéro dans la voie.
        secteurs:      table des secteurs (cf. loaders.carte_scolaire).

    Returns:
        `code_rne` (UAI) du collège de secteur, ou None si la donnée ne permet
        pas de trancher : commune absente de la carte, rue inconnue, numéro
        hors plage ou parité incompatible.
    """
    if code_insee is None or pd.isna(code_insee):
        return None
    sub = secteurs.loc[secteurs["code_insee"] == str(code_insee)]
    if sub.empty:
        return None

    # Secteur unique : toute la commune va au même collège, la rue est ignorée.
    unique = sub.loc[sub["secteur_unique"] == "O"]
    if not unique.empty:
        return str(unique.iloc[0]["code_rne"])

    if nom_voie_norm is None or pd.isna(nom_voie_norm) or numero is None or pd.isna(numero):
        return None
    n = int(numero)
    cand = sub.loc[sub["nom_voie_norm"] == str(nom_voie_norm)]
    for _, row in cand.iterrows():
        lo, hi = row["n_de_voie_debut"], row["n_de_voie_fin"]
        if pd.notna(lo) and n < lo:
            continue
        if pd.notna(hi) and n > hi:
            continue
        parite = row["parite"]
        if parite == "P" and n % 2 != 0:
            continue
        if parite == "I" and n % 2 != 1:
            continue
        return str(row["code_rne"])
    return None


def assign_colleges_carte_scolaire(
    workers: gpd.GeoDataFrame,
    addresses: pd.DataFrame,
    secteurs: pd.DataFrame,
    colleges: gpd.GeoDataFrame,
    decay_m: float = 1200.0,
    private_rate: float = 0.20,
    seed: int = 42,
) -> gpd.GeoDataFrame:
    """Affecte un collège à chaque agent collégien, carte scolaire en tête.

    Deux étapes (même esprit que `assign_workplaces_mobpro`) : tirage
    public/privé PAR MÉNAGE (Bernoulli `private_rate`, RNG seedé ; les
    collégiens d'un même `household_id` partagent le statut — repli par agent
    si la colonne est absente), puis affectation déterministe par la carte
    scolaire pour les publics (repli gravité) et gravité restreinte au privé
    pour les autres — cf. module.

    Args:
        workers:      agents collégiens (`home_id`, géométrie = point domicile).
        addresses:    adresses des domiciles, INDEXÉ par `home_id` (colonnes
                      `code_insee`, `nom_voie_norm`, `numero` — sortie
                      compatible attach_building_addresses, réindexée par
                      l'appelant).
        secteurs:     table carte scolaire (cf. loaders.carte_scolaire).
        colleges:     collèges de la zone (`equip_id`, `code_rne`, `secteur`,
                      `capacity`, géométrie — filtrés à la zone par l'appelant).
        decay_m:      échelle de la gravité de repli (m).
        private_rate: probabilité qu'un agent soit scolarisé dans le privé.
        seed:         graine (reproductibilité).

    Returns:
        Copie de `workers` enrichie de dest_id, dest_x, dest_y, dist_m (même
        contrat que assign_facilities, même ordre/index).
    """
    result = workers.copy()
    result["dest_id"] = pd.array([pd.NA] * len(result), dtype="object")
    for col in ("dist_m", "dest_x", "dest_y"):
        result[col] = np.nan
    if result.empty:
        return result
    if colleges is None or colleges.empty:
        logger.warning("Aucun collège disponible — dest_id laissé vide pour %d agents",
                       len(result))
        return result
    if colleges.crs is not None and result.crs is not None and colleges.crs != result.crs:
        colleges = colleges.to_crs(result.crs)

    rng = np.random.default_rng(seed)
    # Tirage public/privé PAR MÉNAGE : un seul Bernoulli par household_id, partagé
    # par ses collégiens (fratrie dans le même secteur). Repli par agent quand la
    # colonne manque (chemin individus indépendants) ou porte des valeurs nulles.
    hh = result["household_id"] if "household_id" in result.columns else None
    if hh is not None and hh.notna().all():
        codes, uniques = pd.factorize(hh, sort=True)
        is_private = (rng.random(len(uniques)) < float(private_rate))[codes]
    else:
        is_private = rng.random(len(result)) < float(private_rate)

    priv_pool = colleges.loc[colleges["secteur"] == "Privé"]
    pub_pool = colleges.loc[colleges["secteur"] == "Public"]

    # Position (iloc) de chaque code_rne dans `colleges` : cible de l'affectation
    # directe. Sans colonne code_rne, la résolution ne peut jamais aboutir.
    if "code_rne" in colleges.columns:
        rne_pos = {}
        for pos, rne in enumerate(colleges["code_rne"].tolist()):
            rne_pos.setdefault(str(rne), pos)
    else:
        rne_pos = {}

    centroids = colleges.geometry.centroid
    col_x = centroids.x.to_numpy()
    col_y = centroids.y.to_numpy()
    col_ids = colleges["equip_id"].to_numpy()

    # Résolution carte scolaire UNE FOIS par domicile (les collégiens d'un même
    # bâtiment partagent adresse et secteur).
    addr = None
    if addresses is not None and len(addresses):
        addr = addresses[~addresses.index.duplicated(keep="first")]
    home_pos: dict = {}
    for home_id in pd.unique(result["home_id"]):
        pos = None
        if addr is not None and home_id in addr.index:
            row = addr.loc[home_id]
            rne = resolve_college_sector(row["code_insee"], row["nom_voie_norm"],
                                         row["numero"], secteurs)
            if rne is not None:
                pos = rne_pos.get(str(rne))  # None si UAI hors zone d'étude
        home_pos[home_id] = pos

    resolved = result["home_id"].map(home_pos)  # NaN si non résolu
    public = ~is_private
    direct = public & resolved.notna().to_numpy()

    icol = {c: result.columns.get_loc(c)
            for c in ("dest_id", "dist_m", "dest_x", "dest_y")}

    # Publics résolus : affectation DIRECTE au collège de secteur (dist_m réel
    # domicile↔collège pour la cohérence du diagnostic).
    if direct.any():
        idx = np.flatnonzero(direct)
        cpos = resolved.to_numpy()[idx].astype(int)
        hx = result.geometry.x.to_numpy()[idx]
        hy = result.geometry.y.to_numpy()[idx]
        d = np.hypot(col_x[cpos] - hx, col_y[cpos] - hy)
        result.iloc[idx, icol["dest_id"]] = col_ids[cpos]
        result.iloc[idx, icol["dist_m"]] = np.round(d, 1)
        result.iloc[idx, icol["dest_x"]] = np.round(col_x[cpos], 1)
        result.iloc[idx, icol["dest_y"]] = np.round(col_y[cpos], 1)

    # Replis gravité : publics non résolus sur le pool public (ou global si
    # vide), privés sur le pool privé (ou global si vide).
    for mask, pool, label in (
        (public & ~direct, pub_pool if not pub_pool.empty else colleges,
         "collège public (repli gravité)"),
        (is_private, priv_pool if not priv_pool.empty else colleges,
         "collège privé (gravité)"),
    ):
        if not mask.any():
            continue
        sub = result[mask]
        assigned = assign_facilities(sub, pool, decay_m=decay_m, seed=seed,
                                     id_col="equip_id", label=label)
        for col in ("dest_id", "dist_m", "dest_x", "dest_y"):
            result.loc[sub.index, col] = assigned[col].values

    logger.info(
        "Affectation collège (carte scolaire) : %d agents — %d publics affectés "
        "par secteur, %d publics en repli gravité, %d privés",
        len(result), int(direct.sum()), int((public & ~direct).sum()),
        int(is_private.sum()),
    )
    return result
