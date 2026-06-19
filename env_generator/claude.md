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
│   │   ├── osm.py               # OSM via Overpass : enrichissement bâtiments (flats/levels)
│   │   ├── buildings.py         # BD TOPO : filtre résidentiel, NB_LOGTS, load_all_buildings
│   │   ├── insee.py             # carreaux Filosofi (Ind_total)
│   │   ├── roads.py             # réseau routier OSM (osmnx, walk/drive) -> Lambert-93
│   │   ├── bpe.py               # BPE 2024 : équipements éducatifs géolocalisés (crèche/école/collège/lycée)
│   │   ├── bdnb.py              # BDNB (CSTB) : usage bâtiment -> lieux de travail (récupère les Indifférencié)
│   │   └── mobpro.py            # flux domicile-travail INSEE (MOBPRO 2022) — calage futur
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
  Religieux, Sportif), **+ récupération BDNB** : les bâtiments dont
  `usage_principal_bdnb_open` ∈ {Tertiaire, Secondaire, Primaire} (passés via
  `extra_ids`, cf. `loaders/bdnb.py`) rattrapent une partie des « Indifférencié »
  exclus par la BD TOPO (~+4 500 lieux de travail sur la métropole). Capacité =
  surface de plancher ; `workplaces.decay_m` (déf. 3000 m).
- **crèche / école / collège / lycée** : équipements **BPE** géolocalisés
  (`loaders.bpe.load_bpe_education`, codes `TYPEQU` → niveau exact) ; capacité
  unité (la capacité d'accueil BPE n'est pas renseignée pour l'enseignement →
  proximité dominante) ; `education.decay_m` (déf. 1200 m, plus court — on
  scolarise au plus proche). Collège/lycée retombent sur le pool « école » si vide.

Sortie : `agents.gpkg` / `agents.geojson` / `agents.csv` (colonnes : `agent_id`,
`home_id`, `age`, `age_band`, `csp`, `activity`, `is_worker`, `dest_id`,
`dest_x/y`, `dist_m`, géométrie = point domicile).

**Équipements éducatifs (`loaders/bpe.py`).** Source autoritaire (BPE 2024, INSEE) :
`load_bpe_education(departement)` télécharge le fichier détail (~157 Mo, cache
unique), filtre aux `TYPEQU` éducatifs (C107/108/109 → école, C201 → collège,
C301/302/303 → lycée, D502 → crèche) et au département, et renvoie des points
Lambert-93 (`equip_id`, `kind`, `capacity`, `nom`).

**MOBPRO (`loaders/mobpro.py`).** Télécharge (via `ensure_cached`) la base de flux
domicile-travail INSEE 2022 (commune→commune, ~11 Mo, `NBFLUX_C22_ACTOCC15P`).
`load_mobpro(communes=[...])` filtre par commune de résidence. **Pas encore
branché** : réservé au calage futur des `decay_m` (gravitaire non calibré).

> Assomptions du premier jet (« on redesignera si besoin ») : âge et CSP tirés
> comme marges indépendantes (pas de table jointe gospl/IPF) ; seuil adulte 18 ans
> (15-17 ans en mineurs côté CSP) ; retraite couperet à 62 ans ; équipements
> éducatifs à capacité uniforme (capacité BPE absente) ; niveaux scolaires
> distingués par âge (répartition uniforme) ; pas de fuite hors région ni
> télétravail ; gravitaires non calibrés. Voir les docstrings de `agents.py` /
> `workplaces.py`.

---

## Sources de données (qui sert à quoi)

| Source | Millésime | Loader | Rôle dans le pipeline | Statut | Lien (vérif. millésime) |
|---|---|---|---|---|---|
| **BD TOPO — bâti** (IGN) | local | `loaders/buildings.py` | **Socle du bâti** : géométrie, `USAGE1/2`, `NB_LOGTS`, `HAUTEUR`, `NB_ETAGES`. Domiciles + lieux de travail. | **Active — principale** | https://geoservices.ign.fr/bdtopo |
| **CONTOURS-IRIS** (IGN) | 2024 | `loaders/iris.py` | Géométries des IRIS (zones `population` / `region`). | Active | https://geoservices.ign.fr/contoursiris |
| **RP — base-ic pop + logement** (INSEE) | 2022 | `loaders/iris.py` | Démographie par IRIS : population, **âge** (`age_*`), **CSP** (`csp_*`), ménages. | Active | https://www.insee.fr/fr/statistiques/8647014 |
| **Filosofi carroyé 200 m** (INSEE) | 2019 | `loaders/insee.py` | Population carroyée (allocation `--source filosofi`). | Active (alternative) | https://www.insee.fr/fr/statistiques/7655475 |
| **OSM — bâtiments** (Overpass) | live | `loaders/osm.py` | **Enrichissement** du bâti : `building:flats`/`levels`, tag usage (filtre résidentiel). | Active — secondaire | https://www.openstreetmap.org |
| **OSM — réseau routier** (osmnx) | live | `loaders/roads.py` | Réseau routier piéton + voiture (Lambert-93). | Active | https://www.openstreetmap.org |
| **BPE** (INSEE) | 2024 | `loaders/bpe.py` | **Équipements éducatifs géolocalisés** (`TYPEQU`) : crèche/école/collège/lycée. | Active | https://www.insee.fr/fr/statistiques/8217525 |
| **BDNB** (CSTB) | local (~2,5 Go) | `loaders/bdnb.py` | `usage_principal_bdnb_open` : **récupère des lieux de travail** parmi les « Indifférencié » BD TOPO (Tertiaire/Secondaire/Primaire). | **Active** (optionnelle) | https://bdnb.io |
| **MOBPRO** (INSEE) | 2022 | `loaders/mobpro.py` | Flux domicile-travail commune→commune. | **Réservée** — calage futur, non branchée | https://www.insee.fr/fr/statistiques/8582949 |

> Toutes les sources distantes passent par le **pipeline de cache unique**
> (`loaders/cache.py`, `ensure_cached`). Millésimes couplés au code (RP/MOBPRO 2022,
> BPE 2024, CONTOURS-IRIS 2024) — changer d'année demande de toucher loaders/URLs.
> La BDNB est un fichier local hors dépôt : si absent, le pipeline tourne sans
> (lieux de travail = BD TOPO seuls).

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
