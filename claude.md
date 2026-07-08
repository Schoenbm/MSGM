# MSGM — CLAUDE.md (racine du monorepo)

**MSGM** (Macro Sim of Grenoble Metro) = chaîne de simulation multi-agents
d'évacuation de la métropole grenobloise, pour une thèse sur le comportement humain
en **crise en cascade** (séisme → rupture de barrage → inondation).

Dépôt GitHub : `github.com/Schoenbm/MSGM` (monorepo). Branche : `master`.

## Structure du dépôt

```
MSGM/
├── README.md          # vue d'ensemble du dépôt (philosophie + carte des modules)
├── claude.md          # ce fichier
└── env_generator/     # MODULE 1 — voir env_generator/claude.md pour les détails
```

> Le repo a été **restructuré en monorepo** : `env_generator` était auparavant un
> dépôt autonome (`PyPopulationGenerator`), désormais sous-dossier de MSGM.
> L'historique a été préservé (`git log --follow env_generator/...` remonte avant la
> bascule). Le `.git` est à la **racine MSGM**, pas dans `env_generator/`.

## Philosophie modulaire (à respecter)

- **Modules découplés par fichiers**, pas par appels directs : chaque module produit
  des `.gpkg` en **Lambert-93 (EPSG:2154)** que le suivant consomme. CRS non
  négociable partout.
- **Cible de simulation = GAMA.** Le module simulateur (à venir) sera en GAML ; les
  modules amont produisent ce que GAMA charge directement (couches géo + attributs).
- **2-3 modules** : `env_generator` (1, opérationnel), simulateur GAMA (2, à venir),
  crise/aléas (3, à définir — module séparé ou intégré au simulateur, non tranché).
- Projet à ses débuts : ne pas sur-construire l'archi des modules absents.

## Travailler dans le dépôt

- Le code Python du module 1 vit dans `env_generator/`. **Lire
  `env_generator/claude.md`** avant d'y toucher (décisions de conception : source
  unique IRIS, pas d'`input()` dans les loaders, millésime INSEE couplé au code).
- Lancer le pipeline : `cd env_generator && python -m src.main --step env --verbose`
  (le `-m` est obligatoire).
- Tests : `cd env_generator && python -m pytest -q` doit rester vert.
- Commits : messages clairs, ne committer/push que sur demande.
