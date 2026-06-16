# env_generator — CLAUDE.md

## Objectif
Générer l'**environnement de simulation** de la métropole grenobloise pour le
jumeau numérique / l'ABM d'évacuation : **réseau routier** (piéton + voiture),
**bâtiments**, **population synthétique** localisée (âge + CSP).

Module 1 de **MSGM** (`../`, Macro Sim of Grenoble Metro). Le modèle GAMA
(simulateur) vit ailleurs et **consomme les sorties `.gpkg`** de ce module.

**CRS unique partout : Lambert-93 (EPSG:2154).** Non négociable (DT + standard
France). Tout loader doit retourner du 2154.

---

## Structure réelle

```
env_generator/
├── config.yaml                  # config du pipeline --step env (2 zones, URLs, réseau)
├── requirements.txt
├── data/
│   ├── contour_iris.shp         # IRIS du terrain (métropole) — 161 IRIS
│   ├── batim_grenoble.shp       # BD TOPO bâti (USAGE1/2, NB_LOGTS, HAUTEUR)
│   ├── insee_metro_grenoble.shp # carreaux Filosofi 200 m (Ind)
│   ├── cache/                   # téléchargements IGN/INSEE (gitignored)
│   └── processed/               # sorties (gitignored)
├── src/
│   ├── main.py                  # CLI --step {load,match,export,visualize,compare,casualties,all,env}
│   ├── config.py                # lecture config.yaml (Config, ZoneConfig)
│   ├── loaders/
│   │   ├── iris.py              # CONTOURS-IRIS + INSEE RP 2022 ; Selector, resolve_zone, validate_subset
│   │   ├── osm.py               # enrichissement bâtiments OSM (flats/levels) via Overpass
│   │   ├── buildings.py         # BD TOPO : filtre résidentiel, NB_LOGTS, load_all_buildings
│   │   ├── insee.py             # carreaux Filosofi (Ind_total)
│   │   └── roads.py             # réseau routier OSM (osmnx, walk/drive) -> Lambert-93
│   ├── matching/
│   │   ├── spatial_join.py      # centroïdes bâtiments ↔ grille (porte les colonnes âge/CSP)
│   │   └── allocator.py         # allocation pop/ménages + âge/CSP (plus fort reste)
│   ├── output/
│   │   ├── export.py            # GeoJSON + CSV + Shapefile ; export_all_buildings
│   │   ├── visualize.py         # carte
│   │   ├── compare.py / compare_grid.py  # validation vs recensement
│   │   └── casualties.py        # victimes/sans-abris depuis dommages D1..D5
│   └── utils/logging_config.py
└── tests/                       # pytest (~252 tests)
```

> ⚠️ `loaders/osm.py` concerne les **bâtiments** (tags flats/levels), PAS le
> réseau routier. Le réseau routier, c'est `loaders/roads.py` (osmnx).

---

## Architecture du pipeline `--step env`

Piloté par `config.yaml`, deux zones :
- **population** : `load_iris(selector)` → IRIS + INSEE (âge, CSP, ménages).
- **region** : `resolve_zone(selector, buffer_m)` → emprise géométrique seule
  (terrain d'évacuation, plus large). `region same_as: population` possible.

Enchaînement (`step_env`) : population → région → `validate_subset` (garde-fou
`population ⊆ region`) → `fetch_road_network` (walk + drive) → bâtiments BD TOPO
filtrés sur la région + enrichissement OSM → `allocate_population` → export.

La carte (région) plus grande que la zone peuplée est une **feature** voulue :
les bâtiments hors zone population reçoivent `population = 0` (attendu, pas une
erreur).

---

## Décisions de conception à respecter

- **Source unique pour les IRIS.** Si le shapefile local ne couvre pas tous les
  codes demandés : `on_missing="error"` (défaut, lève `MissingIrisError`) ou
  `"download"` (rebascule **entièrement** sur l'IGN France). **Jamais** de fusion
  local + download (millésimes d'IRIS incompatibles → chevauchements/trous).
- **Confirmation côté CLI uniquement.** Les loaders ne font pas d'`input()` :
  `step_env` attrape `MissingIrisError`, demande confirmation (ou `--yes`),
  refuse en non-interactif. Garder cette séparation (bibliothèque réutilisable).
- **Sélecteur multi-niveaux** (`iris|commune|departement`) résolu par préfixe du
  `CODE_IRIS` sur une seule source.
- **Millésime 2022 couplé au code** : URLs paramétrables (config `datasets`), mais
  noms de colonnes INSEE (`P22_*`, `C22_*`) en dur dans `iris.py`. Le documenter,
  ne pas prétendre l'inverse.

---

## Conventions

- Python ≥ 3.10, type hints sur l'API publique.
- Logging via `utils/logging_config.py` (INFO par défaut, DEBUG avec `--verbose`).
- Tout passe par le CLI (`python -m src.main`), pas de notebooks dans le dépôt.
- **Tester avant de committer** : `python -m pytest -q` doit rester vert. Les
  loaders réseau (osmnx/overpy) sont mockés dans les tests — pour la correctness
  réelle, faire un smoke test sur une petite emprise.
- Commits : messages clairs, ne committer que sur demande.
