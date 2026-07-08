"""Normalisation de chaînes partagée entre loaders et matching.

Module racine (ni `loaders/` ni `matching/`) : la normalisation des noms de voie
doit être IDENTIQUE des deux côtés de la jointure adresse → secteur collège
(BAN d'un côté, carte scolaire des collèges publics de l'autre), sinon le
rattachement échoue silencieusement (aucune correspondance trouvée).
"""

import math
import re
import unicodedata

_SEPARATORS = re.compile(r"['’\-]")
_MULTISPACE = re.compile(r"\s+")


def normalize_voie(s: "str | None") -> str:
    """Nom de voie comparable entre sources (BAN vs carte scolaire).

    Uppercase, accents retirés, apostrophes/tirets remplacés par un espace,
    espaces multiples réduits, strip. ``None``/NaN → chaîne vide.
    """
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _SEPARATORS.sub(" ", s.upper())
    return _MULTISPACE.sub(" ", s).strip()
