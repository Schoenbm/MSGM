# env_generator

Génère l'**environnement de simulation** de la métropole grenobloise pour le
jumeau numérique / l'ABM d'évacuation : **réseau routier** (piéton + voiture),
**bâtiments**, et **population synthétique** localisée (avec ventilation âge / CSP).

Module 1 de **MSGM** (Macro Sim of Grenoble Metro). Tout est produit en
**Lambert-93 (EPSG:2154)** — non négociable (le DT travaille en Lambert, standard
national français).

---

## Deux usages

### A. Génération d'environnement complète (`--step env`) — recommandé
Pipeline piloté par `config.yaml` : deux zones distinctes
- **population** : où vivent les agents (IRIS + données INSEE → population synthétique) ;
- **region** : terrain d'évacuation, plus large (réseau routier + bâtiments).

`population ⊆ region` est attendu ; la carte plus grande que la zone peuplée est
voulu (terrain figé, on fait varier la population).

```bash
python -m src.main --step env --verbose
```

Enchaîne : population → emprise région → réseau routier (walk/drive) → bâtiments
(BD TOPO + enrichissement OSM) → allocation de population → export, dans
`output.dir` (défaut `data/processed/env/`).

### B. Pipeline population seul (héritage Filosofi / IRIS)
Distribue la population aux bâtiments résidentiels, sans réseau routier :

```bash
python -m src.main --step all                              # source Filosofi (carreaux 200 m)
python -m src.main --step all --source iris --iris 381850208,381850209
python -m src.main --step casualties --damage-csv degats.csv   # victimes/sans-abris (D1..D5)
```

Étapes : `load`, `match`, `export`, `visualize`, `compare`, `casualties`, `all`, `env`.

---

## Données d'entrée (`data/`)

| Fichier | Description |
|---|---|
| `contour_iris.shp` (+ sidecars) | IRIS du terrain d'étude (métropole grenobloise) |
| `batim_grenoble.shp` (+ sidecars) | Bâtiments BD TOPO (champs `USAGE1/2`, `NB_LOGTS`, `HAUTEUR`) |
| `insee_metro_grenoble.shp` (+ sidecars) | Carreaux INSEE Filosofi 200 m (champ `Ind`) — pour la source `filosofi` |

> Les gros fichiers ne sont pas versionnés. BD TOPO bâti : IGN. Filosofi : INSEE.

**Téléchargés automatiquement (cache dans `data/cache/`)** : CONTOURS-IRIS (IGN,
seulement si pas de shapefile local ou en fallback) et le recensement INSEE RP
2022 (population + logement, pour la source `iris`). URLs dans `config.yaml`.

---

## Installation

```bash
pip install -r requirements.txt
```

Principales dépendances : `geopandas`, `shapely`, `pyproj`, `pandas`, `osmnx`
(réseau routier), `overpy` (enrichissement bâtiments OSM), `requests`, `py7zr`,
`PyYAML`.

---

## config.yaml

```yaml
crs: "EPSG:2154"

sources:                       # fichiers locaux (prioritaires)
  contours_iris: "./data/contour_iris.shp"
  buildings:     "./data/batim_grenoble.shp"
  insee_filosofi: "./data/insee_metro_grenoble.shp"

datasets:                      # URLs distantes (millésime 2022, cf. limite ci-dessous)
  contours_iris_url: "https://data.geopf.fr/.../CONTOURS-IRIS...7z"
  insee_pop_url: "https://www.insee.fr/.../base-ic-evol-struct-pop-2022_csv.zip"
  insee_logement_url: "https://www.insee.fr/.../base-ic-logement-2022_csv.zip"

zones:
  region:                      # terrain complet (la carte générée)
    selector: { type: iris, codes: [ ... 161 codes ... ] }
    buffer_m: 300              # bordure : récupère le réseau de bord de zone
  population:                  # sous-ensemble simulé
    selector: { type: iris, codes: ["381850208", "381850209", "381850210"] }

network:
  types: [walk, drive]         # drive = voiture
  simplify: true

buildings:
  source: bdtopo               # BD TOPO + enrichissement OSM (flats/levels)

output:
  dir: "./data/processed/env"
  format: gpkg
```

**Sélecteur de zone** : `type: iris | commune | departement` + `codes`. Les trois
se résolvent par préfixe du `CODE_IRIS` sur une **source unique**.

**Codes IRIS manquants du shapefile local** : par défaut le pipeline **s'arrête**
en listant les manquants (souvent une faute de frappe). Avec confirmation
(prompt interactif, ou `--yes`), il **rebascule entièrement** sur le
téléchargement IGN — jamais de mélange local + download (pas de millésimes mêlés).

> **Limite millésime** : les URLs sont paramétrables, mais les noms de colonnes
> INSEE (`P22_*`, `C22_*`) sont **codés en dur pour 2022** dans `loaders/iris.py`.
> Changer d'année demande donc aussi de toucher au code.

---

## Sorties (`data/processed/env/`)

| Fichier | Contenu |
|---|---|
| `population_iris.gpkg` | IRIS population + attributs INSEE (âge, CSP, ménages) |
| `region.gpkg` | Emprise région (terrain) |
| `roads_walk.gpkg`, `roads_drive.gpkg` | Réseaux routiers (Lambert-93) |
| `buildings_light.{geojson,csv,shp}` | Bâtiments résidentiels : ID + géométrie + population (+ CSP) |
| `buildings_full.{geojson,csv,shp}` | Tous attributs |
| `buildings_all.{geojson,shp}` | Tous bâtiments (résidentiels + non) — lieux de travail |

> **Pour vérifier sous QGIS** : charger les `.gpkg` / `.shp`, qui sont en
> **Lambert-93** ; les `.geojson` portent le même contenu mais en **WGS84**.
> Le dossier contient aussi des fichiers de **cache** suffixés d'un hash
> (`roads_walk_<hash>.gpkg`, `osm_buildings_<hash>.geojson`) — les livrables sont
> les versions **sans hash**. Contrôles utiles : `population_iris ⊆ region`, et les
> bâtiments hors zone population avec `population = 0` (attendu).

---

## Algorithme de population

1. **Filtre résidentiel** (`USAGE1/USAGE2 == "Résidentiel"`).
2. **Estimation `NB_LOGTS`** si absent : `floor(surface × étages / surf_moy_logement)`,
   `étages = max(1, round(HAUTEUR / 3))`, enrichie par OSM (`building:flats/levels`).
3. **Jointure spatiale** : centroïde du bâtiment → carreau/IRIS contenant.
4. **Allocation** au prorata de `NB_LOGTS` (méthode du plus fort reste, entiers
   exacts par maille) — population, ménages, et chaque tranche **âge** / **CSP**.

---

## Tests

```bash
python -m pytest -q
```
