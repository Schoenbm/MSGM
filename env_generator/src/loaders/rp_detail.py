"""Loader des **ménages** depuis le fichier détail RP (microdonnées INSEE).

Brique 1 du chantier « ménages/familles » : la génération de population
(`matching/agents.py`) tirait jusqu'ici des **individus indépendants** par bâtiment
(marges `age_*`/`csp_*` séparées), sans ménage ni lien parent→enfant. Ce loader
fournit un **échantillon de ménages réels** (approche *sample-based*, vs
reconstruction IPF d'une table jointe) : la cohérence familiale est **observée**,
pas inventée.

Source : fichier détail **« Individus localisés au canton-ou-ville » RP 2022**
(INSEE réf. 8647104). C'est le seul fichier détail public qui porte À LA FOIS le
lien ménage (`NUMMI`) et une géo fine (`CANTVILLE`, + `IRIS` quand l'IRIS ≥ 200
hab. — renseigné à 100 % sur l'Isère urbaine). Découpé en 5 zones ; l'Isère est en
**Zone E** (Auvergne-Rhône-Alpes, PACA, Corse, DOM).

Reconstruction d'un ménage = `groupby([CANTVILLE, NUMMI])`. Le rôle de chaque
membre vient de `LPRM` (1=personne de référence, 2=conjoint, 3=enfant…) : c'est lui
qui porte le **lien parent→enfant** réutilisé pour le regroupement familial
(child pick-up) en évacuation.

Millésime 2022 **couplé au code** (URL + noms de colonnes en dur), comme `iris.py`.
"""

import logging
import zipfile
from pathlib import Path

import pandas as pd

from .cache import ensure_cached, valid_zip

logger = logging.getLogger(__name__)

# Fichier détail Individus canton-ou-ville 2022, Zone E (contient l'Isère).
# Page : insee.fr/fr/statistiques/8647104
_RP_URL = "https://www.insee.fr/fr/statistiques/fichier/8647104/RP2022_indcvize.zip"
_CSV_IN_ZIP = "FD_INDCVIZE_2022.csv"

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
_CHUNK = 500_000

# Colonnes lues (sous-ensemble des 94). Couplées au millésime 2022.
_USECOLS = [
    "CANTVILLE", "NUMMI", "DEPT", "IRIS",   # identité ménage + géo
    "AGED", "SEXE", "LPRM",                 # individu : âge, sexe, rôle ménage
    "STAT_GSEC", "TACT",                    # activité / CSP
    "IPONDI",                               # poids de sondage
]

# LPRM (lien à la personne de référence du ménage) → rôle synthétique.
LPRM_TO_ROLE: dict[str, str] = {
    "1": "referent",
    "2": "conjoint",
    "3": "enfant",
    "4": "petit_enfant",
    "5": "ascendant",
    "6": "autre_parent",
    "7": "ami",
    "8": "pensionnaire",
    "Z": "hors_menage",
}

# STAT_GSEC (statut × groupe socio-économique) → catégorie `csp_*`, alignée sur les
# regroupements de `iris.py` (C22_POP15P_STAT_GSEC11_21 → csp_agriculteurs, etc.).
# Les actifs occupés (11-16) ET les chômeurs ayant travaillé (21-26) tombent dans
# la même CSP — exactement la définition agrégée INSEE de la base-ic.
STAT_GSEC_TO_CSP: dict[str, str] = {
    "11": "csp_agriculteurs",          "21": "csp_agriculteurs",
    "12": "csp_artisans_commercants",  "22": "csp_artisans_commercants",
    "13": "csp_cadres",                "23": "csp_cadres",
    "14": "csp_prof_intermediaires",   "24": "csp_prof_intermediaires",
    "15": "csp_employes",              "25": "csp_employes",
    "16": "csp_ouvriers",              "26": "csp_ouvriers",
    # Inactifs — alignés sur le CODE base-ic, PAS sur le label (mesuré sur la
    # région vs base-ic : GSEC32 ≈ GSEC40, et le code détail "32" = retraités colle
    # littéralement au "GSEC32"). ⚠️ Le label `csp_chomeurs_inactifs` d'iris.py
    # (GSEC32) est un abus de langage : il désigne en fait les RETRAITÉS. On garde
    # l'alignement par colonne (le label vit dans iris.py), pas par sémantique.
    #   32 = retraités                       → csp_chomeurs_inactifs (= GSEC32)
    #   20 chômeur jamais travaillé / 31 étudiant 14+ / 33 au foyer ou autre inactif
    #                                        → csp_autres_inactifs   (= GSEC40)
    "32": "csp_chomeurs_inactifs",
    "20": "csp_autres_inactifs",
    "31": "csp_autres_inactifs",
    "33": "csp_autres_inactifs",
    "ZZ": "csp_autres_inactifs",       # hors champ (mineurs < 15) — écrasé par "mineur"
}

# Cohérent avec matching/agents.py : seuil adulte 18 ans, mineurs sans CSP.
ADULT_MIN_AGE = 18
MINOR_CSP = "mineur"


def _stream_to_file(url: str, dest: Path) -> None:
    """Stream HTTP `url` → `dest`. Lève si transfert incomplet (Content-Length)."""
    import requests

    logger.info("Téléchargement RP détail Zone E (~125 Mo) : %s", url)
    with requests.get(url, stream=True, timeout=900) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
    if total and downloaded != total:
        raise IOError(f"Téléchargement RP incomplet : {downloaded}/{total} octets ({url}).")


def load_rp_households(
    departement: str = "38",
    communes: "list[str] | set[str] | None" = None,
    url: str = _RP_URL,
    cache_dir: "Path | None" = None,
) -> pd.DataFrame:
    """Charge un échantillon de ménages réels (membres individuels) du RP 2022.

    Args:
        departement: code département à conserver (filtre `DEPT`). Défaut Isère "38".
        communes:    si fourni, restreint aux communes dont le `CANTVILLE` commence
                     par l'un de ces préfixes (pseudo-cantons). `None` = tout le
                     département (recommandé : plus l'échantillon est large, plus le
                     reweighting IPF par IRIS est stable).
        url:         URL du fichier détail Zone E (millésime 2022 par défaut).
        cache_dir:   répertoire de cache (défaut : data/cache).

    Returns:
        DataFrame, **une ligne par individu**, trié par ménage. Colonnes :
        - hh_id    : identifiant ménage **global** = ``f"{CANTVILLE}_{NUMMI}"``
                     (NUMMI n'est unique qu'à l'intérieur d'un canton-ou-ville).
        - iris     : code IRIS de résidence (str ; renseigné ≈100 % en urbain).
        - age      : âge détaillé (int, depuis AGED).
        - sexe     : "1"/"2".
        - role     : rôle dans le ménage (referent/conjoint/enfant/…), depuis LPRM.
        - csp      : catégorie `csp_*` pour les 18+, "mineur" pour les < 18
                     (mêmes étiquettes que matching/agents.py).
        - is_minor : âge < 18.
        - weight   : poids de sondage individuel (IPONDI, décimales conservées).

        Le ménage se reconstruit par ``groupby("hh_id")`` ; le lien parent→enfant
        par ``role`` (referent/conjoint = figures parentales, enfant = enfants).
    """
    cache_dir = cache_dir or _CACHE_DIR
    archive = ensure_cached(
        cache_dir / "RP2022_indcvize.zip",
        produce=lambda tmp: _stream_to_file(url, tmp),
        validate=valid_zip,
        label="RP2022_indcvize.zip",
    )

    dep = str(departement).strip()
    prefixes = tuple(communes) if communes else None

    parts: list[pd.DataFrame] = []
    with zipfile.ZipFile(archive) as z:
        with z.open(_CSV_IN_ZIP) as f:
            for chunk in pd.read_csv(f, sep=";", usecols=_USECOLS, dtype=str,
                                     chunksize=_CHUNK, low_memory=False):
                keep = chunk[chunk["DEPT"] == dep]
                if prefixes is not None and not keep.empty:
                    keep = keep[keep["CANTVILLE"].str.startswith(prefixes)]
                if not keep.empty:
                    parts.append(keep)

    if not parts:
        logger.warning("RP détail : aucun individu pour le département %s", dep)
        return _empty_households()

    raw = pd.concat(parts, ignore_index=True)
    out = _build_members(raw)

    n_hh = out["hh_id"].nunique()
    logger.info(
        "RP ménages (dept %s) : %d individus, %d ménages, %d IRIS | "
        "taille moyenne %.2f | %.1f%% mineurs",
        dep, len(out), n_hh, out["iris"].nunique(),
        len(out) / n_hh if n_hh else 0.0,
        100.0 * out["is_minor"].mean(),
    )
    return out


def _build_members(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise les colonnes INSEE brutes en schéma membres (cf. docstring publique)."""
    age = pd.to_numeric(raw["AGED"], errors="coerce").fillna(-1).astype(int)
    is_minor = age < ADULT_MIN_AGE

    csp = raw["STAT_GSEC"].map(STAT_GSEC_TO_CSP).fillna("csp_autres_inactifs")
    csp = csp.where(~is_minor, MINOR_CSP)  # < 18 ans → "mineur", peu importe STAT_GSEC

    out = pd.DataFrame({
        "hh_id": raw["CANTVILLE"].astype(str) + "_" + raw["NUMMI"].astype(str),
        "iris": raw["IRIS"].astype(str),
        "age": age,
        "sexe": raw["SEXE"].astype(str),
        "role": raw["LPRM"].map(LPRM_TO_ROLE).fillna("autre_parent"),
        "csp": csp.to_numpy(),
        "is_minor": is_minor.to_numpy(),
        "weight": pd.to_numeric(raw["IPONDI"], errors="coerce").fillna(0.0).to_numpy(),
    })
    return out.sort_values("hh_id", kind="stable").reset_index(drop=True)


def _empty_households() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["hh_id", "iris", "age", "sexe", "role", "csp", "is_minor", "weight"]
    )
