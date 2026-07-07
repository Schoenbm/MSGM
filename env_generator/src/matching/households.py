"""Reweighting du pool de ménages sur les marges IRIS (brique 2 du chantier ménages).

L'échantillon de ménages réels (`loaders/rp_detail.load_rp_households`) est tiré à
la maille **canton-ou-ville**, plus grosse que l'IRIS. Pour générer une population
fidèle au profil *local* d'un IRIS, on ne pioche donc pas un ménage local : on
**repondère le pool entier** pour que ses marges pondérées reproduisent les marges
IRIS (`age_*`, `csp_*` d'`iris.py`), puis on tire (brique 3).

Méthode = **IPU** (Iterative Proportional Updating, Ye et al. 2009), la variante de
l'IPF adaptée au cas **multi-niveaux** : l'unité repondérée est le *ménage* (un
poids par ménage, partagé par ses membres), mais les contraintes sont au niveau
*individu* (tranches d'âge + CSP). On cycle sur chaque contrainte en mettant à
l'échelle les poids des ménages qui y contribuent, jusqu'à convergence.

Subtilités assumées (cohérentes avec le reste du pipeline) :
- les marges `csp_*` portent sur les **15+** (base-ic) alors que la CSP du loader
  ne concerne que les **18+** (mineurs <18 → "mineur", hors contraintes) : les
  15-17 ans fuient les contraintes CSP — même approximation que `agents.py` ;
- une contrainte de cible nulle (IRIS sans effectif dans une case) est **ignorée**
  plutôt que d'annuler en cascade les ménages concernés (robustesse sur petits IRIS).
"""

import logging

import numpy as np
import pandas as pd

from src.matching.agents import AGE_BANDS, ALL_CSP_COLS

logger = logging.getLogger(__name__)

# Ordre figé des contraintes : 10 tranches d'âge puis 8 CSP.
AGE_COLS: tuple[str, ...] = tuple(AGE_BANDS)
CSP_COLS: tuple[str, ...] = ALL_CSP_COLS

# Contraintes de NIVEAU MÉNAGE : composition INSEE (base-ic couples-familles-
# ménages, C22_MENPSEUL / C22_MENSFAM / C22_MENCOUPSENF / C22_MENCOUPAENF /
# C22_MENFAMMONO). Chaque ménage du pool contribue 1 dans SA classe — c'est la
# forme complète de l'IPU (Ye et al. 2009 : contraintes ménage × individu).
# Les noms ci-dessous sont ceux portés par le grid d'iris.py (renommage des C22_*)
# et forwardés par spatial_join — garder les trois alignés.
HH_TYPE_COLS: tuple[str, ...] = (
    "men_seul",         # personne seule (C22_MENPSEUL) — inclut les singletons Zc_ (D4)
    "men_sans_fam",     # autres ménages sans famille (C22_MENSFAM)
    "men_couple_senf",  # couple sans enfant (C22_MENCOUPSENF)
    "men_couple_aenf",  # couple avec enfant(s) (C22_MENCOUPAENF)
    "men_mono",         # famille monoparentale (C22_MENFAMMONO)
)


def household_types(members: pd.DataFrame) -> pd.Series:
    """Classe chaque ménage du pool dans une catégorie de composition (HH_TYPE_COLS).

    Classification par les rôles LPRM du loader (approximation de la « famille
    principale » INSEE) : taille 1 → personne seule (y compris les singletons de
    communauté `Zc_*`, D4) ; conjoint présent → couple avec/sans enfant(s) ;
    enfant(s) sans conjoint → monoparentale ; le reste (colocations, référent +
    ami/pensionnaire…) → sans famille. Validé sur les communes de la zone test :
    parts pondérées IPONDI à 1-3 points des parts INSEE C22_MEN* (l'excès « seul »
    = les communautés, comptées dans les ménages sous D4).

    Returns:
        Series hh_id → classe (str), indexée par hh_id (ordre du groupby).
    """
    role, hh = members["role"], members["hh_id"]
    size = members.groupby("hh_id").size()
    has_conjoint = role.eq("conjoint").groupby(hh).any()
    has_child = role.eq("enfant").groupby(hh).any()

    types = pd.Series("men_sans_fam", index=size.index, name="hh_type")
    types[has_conjoint & has_child] = "men_couple_aenf"
    types[has_conjoint & ~has_child] = "men_couple_senf"
    types[~has_conjoint & has_child & (size > 1)] = "men_mono"
    types[size == 1] = "men_seul"
    return types


def _age_band(age: "pd.Series") -> "pd.Series":
    """Affecte chaque âge entier à sa tranche INSEE (clé de AGE_BANDS).

    Les âges hors de toute tranche (AGED > 99, ~150 centenaires sur 310k ; ou âge
    inconnu) tombent en NA et ne contribuent à aucune contrainte d'âge — assumé, et
    négligeable. ⚠️ NE PAS les rabattre sur age_80p (`clip`) : mesuré, ça rend le
    calage IPU **infaisable** (médiane d'erreur 0,12 % → 6 %, l'erreur ne baisse
    plus même à 3000 itérations). Régression réelle écartée, ne pas réintroduire.
    """
    band = pd.Series(pd.NA, index=age.index, dtype=object)
    for name, (lo, hi) in AGE_BANDS.items():
        band[(age >= lo) & (age <= hi)] = name
    return band


class HouseholdReweighter:
    """Pool de ménages + matrice de contributions, repondérable par IRIS.

    La matrice de contributions ``A`` (n_ménages × 18) est construite **une fois** :
    ``A[h, c]`` = nombre de membres du ménage ``h`` dans la catégorie ``c`` (tranche
    d'âge ou CSP). Chaque IRIS ne fait ensuite que relancer l'IPU sur ses cibles.
    """

    def __init__(self, members: pd.DataFrame):
        """Args: members = sortie de `load_rp_households` (une ligne par individu)."""
        m = members.copy()
        m["age_band"] = _age_band(m["age"])

        # Index ménage commun et ordonné. On le fixe explicitement : un crosstab
        # laisse tomber les ménages dont TOUS les membres ont une modalité NA (âge
        # hors [0,99] → tranche NA), d'où des index de tailles différentes sinon.
        hh_index = pd.Index(sorted(m["hh_id"].unique()), name="hh_id")

        # Contributions âge (tous les membres) et CSP (adultes seulement : les
        # mineurs ont csp="mineur", hors des 8 colonnes csp_*).
        age_mat = (
            pd.crosstab(m["hh_id"], m["age_band"])
            .reindex(index=hh_index, columns=AGE_COLS, fill_value=0)
        )
        csp_mat = (
            pd.crosstab(m["hh_id"], m["csp"])
            .reindex(index=hh_index, columns=CSP_COLS, fill_value=0)
        )
        self.hh_ids: np.ndarray = hh_index.to_numpy()
        self.A: np.ndarray = np.hstack([age_mat.to_numpy(), csp_mat.to_numpy()]).astype(float)
        self.constraint_cols: tuple[str, ...] = AGE_COLS + CSP_COLS

        # Contraintes de niveau MÉNAGE : indicatrice de la classe de composition
        # (1 dans sa colonne HH_TYPE_COLS, 0 ailleurs). Gardée SÉPARÉE de `A`
        # (contrat public : A = 18 contraintes individus, cf. tests brique 2).
        type_series = household_types(m).reindex(hh_index).fillna("men_sans_fam")
        self.type_matrix: np.ndarray = (
            pd.get_dummies(type_series).reindex(columns=HH_TYPE_COLS, fill_value=0)
            .to_numpy(dtype=float)
        )
        self.hh_type_cols: tuple[str, ...] = HH_TYPE_COLS

        # Poids initial = IPONDI du référent (proxy du poids logement IPONDL, absent
        # de ce fichier) ; repli sur la moyenne du ménage si pas de référent (29 cas).
        ref = m[m["role"] == "referent"].groupby("hh_id")["weight"].first()
        mean_w = m.groupby("hh_id")["weight"].mean()
        self.init_weight: np.ndarray = (
            ref.reindex(hh_index).fillna(mean_w.reindex(hh_index)).fillna(1.0).to_numpy()
        )

        # Collapse en **types** : des milliers de ménages partagent le même vecteur
        # de contributions (âge×CSP×composition). On repondère les types (≈ quelques
        # milliers) au lieu des 141k ménages — l'IPU devient ~20× plus rapide. Le poids
        # de type est ensuite redistribué aux ménages réels au prorata de leur IPONDI
        # initial, ce qui préserve les marges ET garde des ménages distincts pour le
        # tirage. Le collapse inclut les colonnes de composition : deux ménages de même
        # profil âge×CSP mais de classe différente restent des types distincts (sans
        # cibles de composition, l'IPU les traite à l'identique → rétrocompatible).
        full = np.hstack([self.A, self.type_matrix])
        self._types, self._inverse = np.unique(full, axis=0, return_inverse=True)
        self._inverse = self._inverse.ravel()
        self._type_init = np.bincount(self._inverse, weights=self.init_weight)

        logger.info(
            "Pool de ménages : %d ménages → %d types, %d contraintes individus + %d composition",
            len(self.hh_ids), len(self._types), self.A.shape[1], self.type_matrix.shape[1],
        )

    def weights_for(
        self,
        age_targets: "dict[str, float] | pd.Series",
        csp_targets: "dict[str, float] | pd.Series",
        hh_type_targets: "dict[str, float] | pd.Series | None" = None,
        n_households: "float | None" = None,
        mean_size_target: "float | None" = None,
        n_iter: int = 80,
        tol: float = 1e-4,
    ) -> np.ndarray:
        """Repondère le pool pour un IRIS et renvoie un poids par ménage.

        Sans contrainte de niveau ménage, seules les marges *individus* sont
        pincées et le nombre de ménages implicite (Σ poids) dérive librement :
        mesuré +9 à +20 % vs P22_MEN → population tirée −15 %. La contrainte du
        seul NOMBRE (`n_households`) corrige le compte mais pas la TAILLE : la
        repondération sur l'âge déforme la distribution des tailles (+31 % local
        mesuré, IRIS 381510102). La forme complète (Ye et al. 2009) passe par
        `hh_type_targets` : le nombre de ménages PAR CLASSE de composition, qui
        pince à la fois le compte (somme des classes) et la structure des tailles.

        Args:
            age_targets: cibles par tranche d'âge (clés de AGE_BANDS).
            csp_targets: cibles par CSP (colonnes csp_*).
            hh_type_targets: cibles par classe de composition (clés HH_TYPE_COLS,
                marges INSEE C22_MEN* de l'IRIS). **Prioritaire** : rend
                `n_households` redondant (somme des classes = nombre total).
            n_households: repli quand la composition n'est pas disponible —
                contrainte du seul nombre total de ménages (colonne « tous = 1 »).
            mean_size_target: cible de TAILLE MOYENNE de ménage (population /
                ménages de l'IRIS). Appliquée en tilt exponentiel final (cf.
                commentaire dans le corps) : garantit E[taille pondérée] = cible
                même quand les cibles INSEE sont mutuellement incohérentes.
            n_iter:      nombre max d'itérations IPU.
            tol:         seuil de convergence (variation relative max d'un facteur).

        Returns:
            Vecteur de poids (taille n_ménages), aligné sur `self.hh_ids`.
        """
        # Cibles de composition absentes → 0 : l'ipu() ignore les contraintes de
        # cible nulle, donc le comportement historique est préservé à l'identique.
        hh_type_targets = hh_type_targets if hh_type_targets is not None else {}
        targets = np.array(
            [float(age_targets.get(c, 0.0)) for c in AGE_COLS]
            + [float(csp_targets.get(c, 0.0)) for c in CSP_COLS]
            + [float(hh_type_targets.get(c, 0.0)) for c in HH_TYPE_COLS]
        )
        types = self._types
        if n_households is not None:
            # Colonne constante 1 (chaque ménage compte pour 1) : ne scinde aucun
            # type, la matrice collapse reste valide telle quelle.
            types = np.hstack([types, np.ones((len(types), 1))])
            targets = np.append(targets, float(n_households))
        # IPU sur les types (rapide), puis redistribution aux ménages réels : chaque
        # ménage reçoit le poids de son type au prorata de son IPONDI initial dans le
        # type. La somme par type est conservée → marges identiques à l'IPU ménage.
        type_w = ipu(types, targets, self._type_init, n_iter=n_iter, tol=tol)

        if mean_size_target is not None:
            # Tilt final sur la TAILLE : quand les cibles âge×CSP×composition sont
            # mutuellement incohérentes (mesuré : C22_MEN à +38 % de P22_MEN sur un
            # petit IRIS), l'IPU converge sur un compromis où la taille moyenne
            # pondérée — un RATIO, insensible aux contraintes de niveau (nombre,
            # personnes : scalings uniformes) — dérive de +5 à +42 %. Le tilt
            # exponentiel w' = w·exp(θ·taille) est la correction de plus petite
            # divergence (famille exponentielle / max-entropie) qui amène la
            # taille moyenne exactement sur la cible ; θ résolu par bisection.
            # Les marges de structure s'en écartent (mesuré : TV d'âge ≤ 0,16 sur
            # l'IRIS aux données incohérentes, ~0 ailleurs) — arbitrage assumé :
            # population et nombre de ménages priment (D1/D2), le WARNING par
            # IRIS signale les quartiers dégradés.
            t_sizes = self._types[:, : len(AGE_COLS)].sum(axis=1)
            type_w = _mean_size_tilt(type_w, t_sizes, float(mean_size_target))
            # Renormalise le NIVEAU (le tirage n'utilise que les ratios) pour que
            # les diagnostics absolus (fitted vs cibles) restent comparables.
            count = n_households if n_households is not None else targets[
                len(AGE_COLS) + len(CSP_COLS):].sum()
            if count and type_w.sum() > 0:
                type_w = type_w * (float(count) / type_w.sum())

        share = self.init_weight / self._type_init[self._inverse]
        return type_w[self._inverse] * share


def ipu(
    A: np.ndarray,
    targets: np.ndarray,
    init_weight: np.ndarray,
    n_iter: int = 80,
    tol: float = 1e-4,
) -> np.ndarray:
    """Iterative Proportional Updating : poids ménages calant les marges `targets`.

    Args:
        A:           matrice n_ménages × n_contraintes (contributions entières).
        targets:     vecteur cible par contrainte (même ordre que les colonnes de A).
        init_weight: poids initial par ménage.
        n_iter:      nombre max de balayages de l'ensemble des contraintes.
        tol:         arrêt quand le facteur d'ajustement max s'écarte de 1 de < tol.

    Returns:
        Vecteur de poids ménages (float).
    """
    w = init_weight.astype(float).copy()
    cols = [A[:, c] for c in range(A.shape[1])]  # vues colonnes réutilisées
    max_dev = 0.0  # défini hors boucle (sûreté si n_iter=0 → clause else)
    for it in range(n_iter):
        max_dev = 0.0
        for c, col in enumerate(cols):
            t = targets[c]
            if t <= 0:
                continue  # cible nulle → contrainte ignorée (cf. docstring module)
            weighted = float(w @ col)  # produit scalaire (les 0 ne contribuent pas)
            if weighted <= 0:
                continue  # aucun ménage ne porte cette catégorie dans le pool
            factor = t / weighted
            w *= np.where(col > 0, factor, 1.0)
            max_dev = max(max_dev, abs(factor - 1.0))
        if max_dev < tol:
            logger.debug("IPU convergé en %d itérations (déviation %.2e)", it + 1, max_dev)
            break
    else:
        logger.debug("IPU : %d itérations sans atteindre tol=%.0e (déviation %.2e)",
                     n_iter, tol, max_dev)
    return w


def _mean_size_tilt(
    weights: np.ndarray,
    sizes: np.ndarray,
    mean_target: float,
    theta_bounds: "tuple[float, float]" = (-2.0, 2.0),
    n_bisect: int = 80,
) -> np.ndarray:
    """Tilt exponentiel w' = w·exp(θ·taille) amenant la taille moyenne pondérée
    sur `mean_target` (θ résolu par bisection ; monotone croissante en θ).

    θ est borné (|θ| ≤ 2, soit un facteur e² ≈ 7,4 par personne d'écart) : si la
    cible est hors de portée du pool, on sature au lieu de dégénérer."""
    if not np.isfinite(mean_target) or mean_target <= 0:
        return weights
    lo, hi = theta_bounds
    for _ in range(n_bisect):
        mid = 0.5 * (lo + hi)
        w = weights * np.exp(mid * sizes)
        s = w.sum()
        if s <= 0:
            return weights
        if (w * sizes).sum() / s < mean_target:
            lo = mid
        else:
            hi = mid
    return weights * np.exp(0.5 * (lo + hi) * sizes)


def fit_quality(A: np.ndarray, weights: np.ndarray, targets: np.ndarray) -> pd.DataFrame:
    """Compare marges pondérées obtenues vs cibles (diagnostic de convergence).

    Returns un DataFrame indexé par contrainte : `target`, `fitted`, `rel_err`.
    Les contraintes de cible nulle (ignorées par l'IPU) sont exclues.
    """
    fitted = (weights[:, None] * A).sum(axis=0)
    keep = targets > 0
    return pd.DataFrame(
        {"target": targets[keep], "fitted": fitted[keep],
         "rel_err": (fitted[keep] - targets[keep]) / targets[keep]},
    )
