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
│   │   ├── osm.py               # OSM via Overpass : bâtiments (flats/levels) + équipements éducatifs
│   │   ├── buildings.py         # BD TOPO : filtre résidentiel, NB_LOGTS, load_all_buildings
│   │   ├── insee.py             # carreaux Filosofi (Ind_total)
│   │   └── roads.py             # réseau routier OSM (osmnx, walk/drive) -> Lambert-93
│   │   ├── mobpro.py            # flux domicile-travail INSEE (MOBPRO 2022) — calage futur
│   ├── matching/
│   │   ├── spatial_join.py      # centroïdes bâtiments ↔ grille (porte les colonnes âge/CSP)
│   │   ├── allocator.py         # allocation pop/ménages + âge/CSP (plus fort reste)
│   │   ├── agents.py            # génère les individus (âge + CSP + domicile + travail)
│   │   └── workplaces.py        # affectation gravitaire d'un lieu de travail aux actifs
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

**Population d'agents (`matching/agents.py` + `workplaces.py`, étape 6 de
`--step env`).** Descend de l'agrégat-par-bâtiment au **grain individuel** : une
ligne = un individu, avec domicile, âge, CSP, une **activité** et une destination.

`generate_agents` (microsimulation par tirage, PAS un IPF) :
- 1 agent par tête de `population_allouee` du bâtiment résidentiel ;
- âge tiré dans les tranches `age_*` (multinomiale ∝ effectifs) + âge entier dans
  la tranche ; CSP des 18+ tirée dans les `csp_*` ; mineurs (<18) → `csp="mineur"` ;
- **repli sur la distribution globale** quand un petit bâtiment a 0 dans chaque
  tranche (artefact du plus fort reste marge par marge) — évite les "inconnu" ;
- **activité** (destination de jour) : 0-2 → `creche`, 3-10 → `ecole`, 11-14 →
  `college`, 15-17 → `lycee`, 18-62 actif occupé → `travail`, sinon (inactif,
  chômeur, **retraité > 62 ans**) → `aucune`. Niveaux scolaires découpés par âge,
  **répartition uniforme** (tirage d'âge uniforme dans la tranche INSEE) ;
- contrôle de conservation : nb d'agents == `population_allouee` totale.

`assign_facilities` (inspiré du localisateur `spll`/`GravityFunction` de Genstar) :
affecte par activité une destination tirée par `P(j|i) ∝ capacité_j × exp(-dist_ij
/ decay_m)`, réutilisé pour travail / école / crèche :
- **travail** : bâtiments `buildings_all` dont `USAGE1`/`USAGE2` ∈
  `workplaces.usages` (défaut Commercial et services, Industriel, Agricole,
  Religieux, Sportif ; **`Indifférencié` exclu** car ~35k bâtiments trop bruités —
  à corriger) ; capacité = surface de plancher ; `workplaces.decay_m` (déf. 3000 m).
- **crèche / école / collège / lycée** : équipements OSM
  (`loaders.osm.fetch_osm_education`) ; capacité unité (proximité dominante) ;
  `education.decay_m` (déf. 1200 m, plus court — on scolarise au plus proche).
  OSM ne séparant pas les niveaux (`amenity=school` partout), collège/lycée sont
  identifiés par **nom** (« Collège … », « Lycée … ») et `isced:level`, avec repli
  sur le pool « école » générique si aucun établissement typé. Le supérieur
  (`college`/`university` OSM) est exclu (aucun agent > 17 ans scolarisé).

Sortie : `agents.gpkg` / `agents.geojson` / `agents.csv` (colonnes : `agent_id`,
`home_id`, `age`, `age_band`, `csp`, `activity`, `is_worker`, `dest_id`,
`dest_x/y`, `dist_m`, géométrie = point domicile).

**Équipements éducatifs (`loaders/osm.py::fetch_osm_education`).** Requête Overpass
**distincte** de `fetch_osm_buildings` (qui ne ramène que building:flats/levels) :
cible les tags éducatifs, ramène chaque équipement à un point (`out center`), cache
GeoJSON par bbox (`osm_education_<hash>.geojson`).

**MOBPRO (`loaders/mobpro.py`).** Télécharge (via `ensure_cached`) la base de flux
domicile-travail INSEE 2022 (commune→commune, ~11 Mo, `NBFLUX_C22_ACTOCC15P`).
`load_mobpro(communes=[...])` filtre par commune de résidence. **Pas encore
branché** : réservé au calage futur des `decay_m` (gravitaire non calibré).

> Assomptions du premier jet (« on redesignera si besoin ») : âge et CSP tirés
> comme marges indépendantes (pas de table jointe gospl/IPF) ; seuil adulte 18 ans
> (15-17 ans en mineurs côté CSP) ; retraite couperet à 62 ans ; équipements
> éducatifs à capacité uniforme (pas d'effectifs réels) ; niveaux scolaires
> distingués par âge (répartition uniforme) et par nom/isced côté OSM (faillible) ;
> pas de fuite hors région ni télétravail ; gravitaires non calibrés. Voir les
> docstrings de `agents.py` / `workplaces.py`.

---

## Décisions de conception à respecter

- **Pipeline de cache unique (`loaders/cache.py`).** Tout accès à des données
  onéreuses (download IGN/INSEE, requête OSM/Overpass, extraction d'archive) passe
  par `ensure_cached(dest, produce=..., validate=...)` : check local → contrôle
  d'intégrité → sinon (re)production **atomique** (`.part` puis renommage). Un
  produit interrompu ne laisse jamais un cache corrompu pris pour valide (bug réel
  rencontré : zip INSEE tronqué accepté comme cache). La **seule** partie
  spécifique au type de données est le *validateur* (`valid_zip`, `valid_7z`,
  `valid_geofile`, `valid_dir_with`). **Ne pas** réintroduire de `if path.exists():`
  ad hoc dans un loader — ajouter un validateur et router par `ensure_cached`.
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
