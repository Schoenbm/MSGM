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

        # Poids initial = IPONDI du référent (proxy du poids logement IPONDL, absent
        # de ce fichier) ; repli sur la moyenne du ménage si pas de référent (29 cas).
        ref = m[m["role"] == "referent"].groupby("hh_id")["weight"].first()
        mean_w = m.groupby("hh_id")["weight"].mean()
        self.init_weight: np.ndarray = (
            ref.reindex(hh_index).fillna(mean_w.reindex(hh_index)).fillna(1.0).to_numpy()
        )

        # Collapse en **types** : des milliers de ménages partagent le même vecteur
        # de contributions (âge×CSP). On repondère les types (≈ quelques milliers)
        # au lieu des 141k ménages — l'IPU devient ~20× plus rapide. Le poids de type
        # est ensuite redistribué aux ménages réels au prorata de leur IPONDI initial,
        # ce qui préserve les marges ET garde des ménages distincts pour le tirage.
        self._types, self._inverse = np.unique(self.A, axis=0, return_inverse=True)
        self._inverse = self._inverse.ravel()
        self._type_init = np.bincount(self._inverse, weights=self.init_weight)

        logger.info(
            "Pool de ménages : %d ménages → %d types, %d contraintes",
            len(self.hh_ids), len(self._types), self.A.shape[1],
        )

    def weights_for(
        self,
        age_targets: "dict[str, float] | pd.Series",
        csp_targets: "dict[str, float] | pd.Series",
        n_iter: int = 80,
        tol: float = 1e-4,
    ) -> np.ndarray:
        """Repondère le pool pour un IRIS et renvoie un poids par ménage.

        Args:
            age_targets: cibles par tranche d'âge (clés de AGE_BANDS).
            csp_targets: cibles par CSP (colonnes csp_*).
            n_iter:      nombre max d'itérations IPU.
            tol:         seuil de convergence (variation relative max d'un facteur).

        Returns:
            Vecteur de poids (taille n_ménages), aligné sur `self.hh_ids`.
        """
        targets = np.array(
            [float(age_targets.get(c, 0.0)) for c in AGE_COLS]
            + [float(csp_targets.get(c, 0.0)) for c in CSP_COLS]
        )
        # IPU sur les types (rapide), puis redistribution aux ménages réels : chaque
        # ménage reçoit le poids de son type au prorata de son IPONDI initial dans le
        # type. La somme par type est conservée → marges identiques à l'IPU ménage.
        type_w = ipu(self._types, targets, self._type_init, n_iter=n_iter, tol=tol)
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
