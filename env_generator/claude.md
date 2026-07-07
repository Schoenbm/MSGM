# env_generator — CLAUDE.md

## Objectif
Générer l'**environnement de simulation** de la métropole grenobloise pour le
jumeau numérique / l'ABM d'évacuation : **réseau routier** (piéton + voiture),
**bâtiments**, **population synthétique** localisée (âge + CSP).

Module 1 de **MSGM** (`../`, Macro Sim of Grenoble Metro). Le modèle GAMA
(simulateur) vit ailleurs et **consomme les sorties `.gpkg`** de ce module.

> 📘 **Référence méthodo complète et lisible : [`METHODE.md`](METHODE.md)** — vue
> d'ensemble, méthode étape par étape, paramètres, journal des décisions, pistes
> écartées (avec preuves), hypothèses/limites, chantiers. À tenir à jour avec ce
> fichier quand la méthode change.

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
│   │   ├── bdnb.py              # BDNB (CSTB) : usage (residentiel/travail/annexe) -> qualifie Indiff. ; + matériaux/période (ffo) pour vulnérabilité
│   │   ├── mobpro.py            # flux domicile-travail INSEE (MOBPRO 2022) — calage futur
│   │   └── rp_detail.py         # RP détail (indiv. canton-ou-ville 2022) : échantillon de ménages réels (chemin ménages)
│   ├── matching/
│   │   ├── spatial_join.py      # centroïdes bâtiments ↔ grille (porte âge/CSP + code_iris d'allocation)
│   │   ├── allocator.py         # allocation pop/ménages + âge/CSP (plus fort reste)
│   │   ├── agents.py            # génère les agents : MÉNAGES réels (principal) ou individus (repli)
│   │   ├── workplaces.py        # affectation gravitaire d'un lieu de travail aux actifs
│   │   └── households.py        # IPU ménage×individu : marges IRIS + composition ménages + tilt taille
│   ├── output/
│   │   ├── export.py            # merge_buildings + export_buildings (complète) ; build_env_layer/export_env (contrat env) ; export_agents
│   │   ├── visualize.py         # carte
│   │   ├── compare.py / compare_grid.py  # validation vs recensement
│   │   └── casualties.py        # victimes/sans-abris depuis dommages D1..D5
│   └── utils/logging_config.py
└── tests/                       # pytest (~398 tests ; test_*realdata.py skippés si cache/données absents)
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

**Population d'agents (`matching/agents.py` + `workplaces.py`, étape 7 de
`--step env`).** Descend de l'agrégat-par-bâtiment au **grain individuel** : une
ligne = un individu, avec domicile, âge, CSP, une **activité** et une destination.

`generate_household_agents` (chemin **PRINCIPAL** — chantier ménages, cf. section
dédiée) : tire des **ménages réels** du RP détail, repondérés par IPU
**ménage×individu** sur les marges de chaque IRIS — âge/CSP (individus) **+
composition des ménages** (`C22_MEN*`, pince la distribution des tailles, Ye et
al. 2009) **+ tilt final de taille moyenne** (garantit population/ménages même
quand les cibles INSEE sont incohérentes entre elles) — et instancie leurs
membres (âge/CSP/rôle observés → `household_id` + `role`, lien parent→enfant).
`step_env` l'utilise dès que le pool RP est chargeable et que `menages_alloues`
existe (mode `--source iris`) ; il applique **D2** (recalcul `population_allouee`
depuis les agents, en place) puis **D3** (`update_building_demographics` : marges
`age_*`/`csp_*` par bâtiment recalées sur les agents, version allocateur gardée en
`*_alloc`).

`generate_agents` (chemin de **REPLI** : mode Filosofi ou pool RP indisponible —
microsimulation par tirage d'individus indépendants, PAS un IPF) :
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
- **travail** : bâtiments de la couche région (frame interne `all_buildings`,
  pas un fichier de sortie : l'export est la couche unique `buildings`) dont `USAGE1`/`USAGE2` ∈
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

Sorties (`output/export.py`) — pensées pour **3 modules** (env_generator
indépendant de la crise → crisis_gen → simulation) :
- **`buildings.{geojson,csv,shp}`** — **couche complète** : tous les bâtiments
  de la région (`merge_buildings`), toutes colonnes, `population_allouee` (0 si
  non-logement), flag `residentiel`, `usage_bdnb`, CSP/âge, matériaux/période BDNB.
  Superset d'inspection QGIS + source pop actuelle de `casualties.py`. Remplace
  l'ancien couple `buildings_full`/`buildings_all`.
- **`env.{geojson,csv,shp}`** (`build_env_layer`/`export_env`) — **contrat de
  simulation** curaté, *sans* population, indépendant de la crise (remplace
  `buildings_light`) : `ID`, géométrie, flags de rôle (`is_residential`,
  `is_workplace`, `is_education`, `is_strategic`) + colonne `fonction` (str :
  hopital, mairie, ecole… via `loaders/poi.py`), profil vertical (`n_etages`,
  `hauteur`, `emprise_m2`, `z_min_sol`, `z_max_toit` → capacité de refuge calculée
  côté crisis_gen selon la cote), vulnérabilité (`mat_mur`, `mat_toit`,
  `annee_construction` — BDNB ffo, pour le NN D1-D5).
- **`agents.gpkg` / `agents.geojson` / `agents.csv`** (colonnes : `agent_id`,
  `home_id`, **`household_id`**, **`role`** (referent/conjoint/enfant/… — lien
  parent→enfant pour le regroupement familial GAMA), `age`, `age_band`, `csp`,
  `activity`, `is_worker`, `dest_id`, `dest_x/y`, `dist_m`, géométrie = point
  domicile). **Source unique de la population** (population/bâtiment =
  `groupby(home_id)`, recopiée dans `population_allouee` par D2 ; le *nombre de
  ménages* par bâtiment est déterministe (= `menages_alloues`), la population
  suit les vraies tailles des ménages tirés — reproductible à seed fixé).
  La couche `buildings` porte en plus les marges `age_*`/`csp_*` recalculées
  depuis les agents ET la version allocateur en `*_alloc` (D3, mesure d'écart ;
  dans le `.shp` les `*_alloc` sont tronquées avec un suffixe `_a`).

**Qualification des bâtiments via BDNB (`loaders/bdnb.py`).** `usage_principal_bdnb_open`
→ catégorie (`travail` / `residentiel` / `annexe`), carte ID bâtiment → catégorie
consommée à deux endroits : (1) `filter_residential` exclut des **logements** les
« Indifférencié » que la BDNB dit non-résidentiels (travail/annexe) et garde ceux
dits résidentiels ; (2) `identify_workplaces` ajoute les bâtiments `travail`.
`filter_residential` applique en plus une **porte de plausibilité par la taille**
(`min_floor_area`, déf. 25 m²) qui écarte les abris/garages « Indifférencié » sans
signal — générique, indépendante de la BDNB (cf. section « Indifférencié » plus bas).
En amont, `absorb_slivers` (toggles `buildings.absorb_slivers` +
`buildings.sliver_max_area_m2`, déf. 20 m²) **recolle les petits fragments**
(vitrines/avancées, **y compris** ceux enchâssés entre deux gros voisins — cas
sandwich) à leur grand voisin jointif — dé-fragmentation conservatrice, surface
conservée à 100 %, exits préservés (cf. METHODE.md § 9).
La couche bâtiment exportée porte une colonne `usage_bdnb` (inspection QGIS/GAMA).
Sur la région : ~+4 500 lieux de travail récupérés, ~17 800 faux logements
écartés (chiffre aligné sur METHODE.md § 3.4). NB : OSM ne couvre ici que les bâtiments tagués flats/levels (apport
faible sur les Indifférencié) ; la BDNB fait l'essentiel.
`load_bdnb_building_attrs` extrait en plus les **matériaux + année de construction**
(`ffo_bat_mat_mur_txt`/`_toit_txt`, `ffo_bat_annee_construction` ; couverture mesurée
~64 % / 58 %, bien mieux que `MAT_MURS` BD TOPO ~35 % en codes cryptiques) → portés
dans le contrat `env` pour le réseau de neurones D1-D5 (module crise).

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
> comme marges indépendantes (**repli seulement** — le chemin ménages OBSERVE la
> table jointe âge×CSP×ménage des ménages réels) ; seuil adulte 18 ans (15-17 ans
> en mineurs côté CSP) ; retraite couperet à 62 ans ; équipements éducatifs à
> capacité uniforme (capacité BPE absente) ; niveaux scolaires distingués par âge
> (répartition uniforme) ; pas de fuite hors région ni télétravail ; gravitaires
> non calibrés. Voir les docstrings de `agents.py` / `workplaces.py`.

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
| **BDNB** (CSTB) | local (~2,5 Go) | `loaders/bdnb.py` | `usage_principal_bdnb_open` → catégorie (travail / résidentiel / annexe) : **qualifie les « Indifférencié »** BD TOPO. Ajoute des lieux de travail ET affine le filtre résidentiel. Annote `usage_bdnb`. **Aussi : matériaux/période** (`ffo_bat_*`, `load_bdnb_building_attrs`) → contrat `env` pour la vulnérabilité (NN D1-D5). | **Active** (optionnelle) | https://bdnb.io |
| **MOBPRO** (INSEE) | 2022 | `loaders/mobpro.py` | Flux domicile-travail commune→commune. | **Réservée** — calage futur, non branchée | https://www.insee.fr/fr/statistiques/8582949 |
| **RP détail — Individus canton-ou-ville** (INSEE) | 2022 | `loaders/rp_detail.py` | **Échantillon de ménages réels** (membres + âge + CSP + rôle `LPRM`) pour la génération sample-based en ménages (`generate_household_agents`). Zone E (contient l'Isère). | **Active** — chemin principal des agents en `--source iris` | https://www.insee.fr/fr/statistiques/8647104 |
| **RP base-ic — couples-familles-ménages** (INSEE) | 2022 | `loaders/iris.py` | **Composition des ménages par IRIS** (`C22_MENPSEUL/SFAM/COUPSENF/COUPAENF/FAMMONO` → `men_*`) : contraintes de niveau ménage de l'IPU (pince la distribution des tailles). Optionnelle : sans elle, repli sur la contrainte du seul nombre. | **Active** | https://www.insee.fr/fr/statistiques/8647008 |

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

---

## Chantier — population en ménages (sample-based) — FAIT (briques 1-4)

**Pourquoi.** `generate_agents` tirait des **individus indépendants** par
bâtiment (marges `age_*`/`csp_*` séparées), **sans ménage ni lien parent→enfant** :
un enfant était posé sans parent rattaché. Trou structurel pour une sim d'évacuation
(le regroupement familial — parents qui vont chercher leurs enfants — est un
déterminant comportemental majeur). Prioritaire **devant** MOBPRO (qui n'est que du
raffinage de destination).

**Approche = sample-based** (ménages réels INSEE), **pas** de reconstruction IPF
d'une table jointe. Source : fichier détail **Individus canton-ou-ville RP 2022**
(`loaders/rp_detail.py`). Calage local par **IPU** (`matching/households.py`).

**État :**
- **Brique 1 (faite)** `loaders/rp_detail.py` — `load_rp_households(dep, communes=None)`
  reconstruit un échantillon de ménages réels : `hh_id` (= `CANTVILLE`+`NUMMI`),
  `role` (depuis `LPRM` : référent/conjoint/enfant → **lien parent→enfant**), `age`,
  `csp` (alignée sur les `csp_*` d'`iris.py`), `iris`, `weight` (`IPONDI`).
- **Brique 2 (faite)** `matching/households.py` — `HouseholdReweighter(members)` +
  `weights_for(age_targets, csp_targets, hh_type_targets=None, n_households=None,
  mean_size_target=None)` : IPU **ménage×individu** (Ye et al. 2009) qui repondère
  le pool (collapse en types âge×CSP×composition → ~ms/IRIS) pour caler les marges
  IRIS. Validé région : erreur médiane 0,12 % (marges individus seules). Étendue en
  sessions briques 3-4 : contrainte de nombre de ménages, puis **contraintes de
  COMPOSITION** (`HH_TYPE_COLS`, `household_types()`) et **tilt final de taille
  moyenne** (`_mean_size_tilt`) — cf. décisions de session ci-dessous.
- **Brique 3 (faite)** `matching/agents.py::generate_household_agents(residential,
  all_buildings, members, education=None, …, seed=42)` — par IRIS : IPU (V1), puis
  par bâtiment tirage de `menages_alloues` ménages **avec remise** (V2, D1) dans le
  pool entier ∝ poids, instanciation des membres réels (âge/CSP/rôle observés).
  Applique **D2 en place** sur `residential`. Constante `IRIS_FIT_WARN_THRESHOLD`
  (0,05) : WARNING par IRIS dont > 5 % de la masse des marges reste mal placée
  après calage (diagnostic runtime). Repli D5 par IRIS (poids IPONDI bruts) si
  poids dégénérés ; repli global `generate_agents` (avec `household_id`/`role`
  vides) si pool RP indisponible ou `menages_alloues` absent.
- **Brique 4 (faite)** schéma `agents.*` + `household_id` + `role` (V4), câblage
  `step_env` (étape 7 : pool RP chargé sur les communes de la zone population →
  chemin ménages + D2 + D3 via `update_building_demographics` ; sinon repli), et
  destinations partagées via `_assign_destinations` (V5, `generate_agents` intact).

**Décisions prises EN SESSION d'implémentation (briques 3-4) — à valider à la relecture :**
- **Contrainte de nombre de ménages dans l'IPU** (`weights_for(...,
  n_households=Σ menages_alloues de l'IRIS = P22_MEN)`). **Pourquoi** : mesuré sur
  Île Verte, l'IPU marges-individus seul laisse dériver le nombre implicite de
  ménages (Σ poids = +9 à +20 % vs P22_MEN) → taille moyenne pondérée 1,43-1,50 au
  lieu de ~1,7 → population tirée **−15,5 %**. C'est le cas d'usage originel de
  l'IPU (Ye et al. 2009 : contraintes ménage + individu). Additive et optionnelle
  (`None` = comportement historique, tests brique 2 inchangés). Avec elle :
  headcount médian ~0,5 % (max 2,3 % sur 10 seeds, bruit de tirage). `n_iter=300`
  dans ce mode (80 laissent un biais résiduel de −0,29 %).
- **Hygiène du pool avant tirage** (`_sanitize_pool`) : écarte les rares ménages
  qui violeraient les invariants de sortie — âge inconnu, **ménage sans adulte**
  (mineur seul, internats `Zc_*`), ménage ordinaire **sans référent unique** (29
  cas Isère). Volumes ~0,1 ‰, loggés ; imposé par les gates certaines (K1/K9).
- **`code_iris` = IRIS d'allocation** (`spatial_join`) : la colonne est réécrite
  depuis la cellule de jointure (str 9 caractères) — le shapefile BD TOPO est
  lacunaire (23/552 bâtiments peuplés sans code sur la zone test, 0 désaccord
  sinon) et l'IRIS qui a donné les marges fait foi.
- **Seuils empiriques calibrés** (`test_households_realdata.py`) sur 10 seeds,
  zone Île Verte (~6 300 hab.) : le bruit de tirage domine à cette taille (σ
  headcount ≈ 1,1 %) → seuils = max mesuré × ~1,5 (valeurs mesurées en
  commentaire des constantes). La gate « forme des distributions » à 0,5 %
  était intenable sous tirage fini (mesuré : 2-5 % d'écart médian sur les parts).
- **Export `.shp`** : les colonnes `*_alloc` (D3) collisionnaient avec leurs
  jumelles après troncature à 10 caractères → suffixe `_a` + garde-fou d'unicité
  dans `_write_shp`.

**Décisions prises EN SESSION — itération 2 (biais de taille de ménage) — à valider :**
- **Constat (mesuré, zone 9 IRIS)** : la contrainte du seul *nombre* de ménages
  corrige le compte mais pas la TAILLE — repondérer sur l'âge déforme la
  distribution des tailles : +5,4 % de taille moyenne zone (+4,3 % de population),
  jusqu'à **+31 % par IRIS** (381510102). Un test zone-globale masquait ces
  compensations → nouveau garde-fou PAR IRIS (`test_mean_household_size_per_iris`).
- **Contraintes de COMPOSITION des ménages dans l'IPU** (la forme complète de Ye
  et al. 2009, ménage×individu). Source : base-ic **couples-familles-ménages 2022**
  (`C22_MENPSEUL/SFAM/COUPSENF/COUPAENF/FAMMONO` — la taille 1/2/3/4/5+ n'existe
  PAS à l'IRIS, vérifié sur le fichier). Chargées par `iris.py` (`men_*`, cache
  unique, millésime couplé), forwardées par `spatial_join`, classification du pool
  par rôles LPRM (`household_types` : seule/couple±enfants/mono/sans-famille ;
  validé : parts pondérées à 1-3 points des parts INSEE communales). Cibles
  **rescalées à Σ = menages_alloues** (= P22_MEN ; le niveau C22, exploitation
  complémentaire, diverge jusqu'à +38 % de P22_MEN sur un petit IRIS — on garde
  les proportions C22 au niveau P22). Zc_* comptés en « personne seule » (D4).
- **Hygiène du pool étendue aux >99 ans** : les membres hors tranches d'âge (~150
  centenaires, piège verrouillé « pas de clip ») ne contribuent à AUCUNE contrainte
  → direction dégénérée que l'IPU exploitait (+9,2 % de taille sur 381510103 avec
  toutes contraintes exactes : 361 personnes pondérées « invisibles »). Ménages
  concernés écartés du pool (les centenaires disparaissent des agents, ~0,01 %).
- **Tilt final de taille moyenne** (`w′ = w·exp(θ·taille)`, θ par bisection →
  E[taille pondérée] = P22_POP/P22_MEN exactement). **Pourquoi** : quand les cibles
  INSEE sont mutuellement incohérentes (C22 vs P22, âge vs composition vs pool
  communal), l'IPU converge sur un compromis où la taille moyenne — un RATIO,
  aveugle aux contraintes de niveau — reste fausse (mesuré : +3 à +42 % selon
  l'ordre des contraintes, aucun ordre ne gagne). Le tilt est la correction de
  plus petite divergence (max-entropie) sur la contrainte de moyenne. Coût assumé
  et mesuré : la FORME d'âge se dégrade là où les données INSEE se contredisent
  (TV attendue ≤ 0,16 sur 381510102, ~0 ailleurs) — la population et le nombre de
  ménages priment (D1/D2), le WARNING par IRIS signale les quartiers dégradés.

**Décisions verrouillées (session prépa Fable) — NE PAS re-trancher :**
- **D1 — Option A.** On honore le **nombre de ménages** (`menages_alloues`) et la
  population suit les **vraies tailles** des ménages tirés. `population_allouee`
  n'est qu'un indicateur, pas gospel : il **bouge** vs l'allocateur, c'est assumé.
- **D2 — un seul indicateur de population.** Après tirage, `population_allouee` est
  **recalculé depuis les agents** (`groupby(home_id).size()`) avant export → une
  seule vérité de population, cohérente entre `buildings.*` et `agents.*`.
  `casualties.py` reste juste **sans être modifié** (il lit `population_allouee`).
- **D3 — âge/CSP par bâtiment : deux jeux.** On **recalcule** `age_*`/`csp_*` par
  bâtiment depuis les agents (= primaire, vérité) ET on **garde** la version
  allocateur (`_allocate_csp_columns`) à côté (suffixée) pour **mesurer l'écart**.
  Impacte légèrement `compare.py`.
- **D4 — communautés (`Zc_*` EHPAD/foyers) = ménages singletons ordinaires**, tirés
  comme les autres. Le placement dédié dans les bâtiments `fonction=ehpad` (POI)
  est un **chantier futur** (débloqué par le POI), pas ici.
- **D5 — repli si l'IPU échoue** sur un IRIS (trop petit / marges irréalisables) :
  tirer de vrais ménages du **pool départemental** (poids IPONDI bruts, structure
  familiale préservée). Dernier recours (zéro donnée RP) : ancien tirage d'individus.
- **D6 — déterminisme.** Le nb/bâtiment devient **stochastique** (tailles de ménages
  variables), calé sur `menages_alloues`, reproductible à seed fixé. Assumé — la
  **convergence multi-runs** des marges est à vérifier (test).
- **V1** IPU **une fois par IRIS** (pool dép. construit une fois), cibles = marges
  `age_*`/`csp_*` INSEE de l'IRIS. ~161 IRIS ≈ +1 min. · **V2** tirage **avec remise**
  dans le **pool entier repondéré**. · **V3** chantier lié au mode **`--source iris`**
  (`P22_MEN` présent) ; mode Filosofi → ancien `generate_agents` en repli. · **V4**
  schéma `agents.*` = colonnes actuelles **+** `household_id` (unique par ménage
  tiré : un ménage cloné dans 2 bâtiments = 2 ids) **+** `role` (LPRM). Âge/CSP
  viennent du membre réel. · **V5** `activity`/destinations réutilisent
  `_classify_activity` + `assign_facilities` inchangés ; le lien parent→enfant est
  **exporté** (household_id+role) pour GAMA, pas modélisé ici.

**Pièges / décisions verrouillées (NE PAS refaire) :**
- **Communautés `LPRM=Z`** (EHPAD, foyers, internats) : leur `NUMMI` est un
  placeholder partagé → groupées, elles formaient de **faux ménages** (un EHPAD = un
  « ménage » de 22 personnes, collisions multi-IRIS). Chaque hors-ménage est un
  **singleton** (`hh_id` `Zc_*`), gardé pour les marges base-ic.
- Le label **`csp_chomeurs_inactifs`** d'`iris.py` (`GSEC32`) est un **abus de
  langage : ce sont les RETRAITÉS**. Le mapping `STAT_GSEC` d'`rp_detail.py` s'aligne
  sur le **code**, pas sur le label.
- **NE PAS rabattre (`clip`) les âges >99 sur `age_80p`** : mesuré, ça rend le calage
  IPU **infaisable** (médiane 0,12 % → 6 %). Les ~150 centenaires restent hors
  tranche d'âge (négligeable).
- **Filtre commune = `IRIS[:5]`**, PAS `CANTVILLE` (pseudo-canton, code sans rapport).

**Tests de non-régression** : `tests/test_rp_realdata.py` (skippé si le cache
`data/cache/RP2022_indcvize.zip` est absent) vérifie sur le **vrai** fichier les
propriétés émergentes que les mocks ratent (0 ménage multi-IRIS, 0 orphelin, taille
≈ INSEE, fit IPU médiane < 1 %). **Leçon** : les mocks n'ont attrapé aucun des bugs
ci-dessus ; valider sur données réelles avant de committer ce chantier.

**Tests-contrat des briques 3-4 (écrits AVANT l'implémentation, test-first) — tous verts :**
- `tests/test_household_agents.py` (18 tests) — contrat **structurel** sur fixture
  synthétique (schéma `household_id`/`role`, K1-K14 : 0 orphelin, 0 ménage éclaté,
  conservation des ménages == `menages_alloues`, éducation adaptée à l'âge,
  déterminisme du nb de ménages…).
- `tests/test_households_realdata.py` (14 tests) — **cohérence + correctness** sur
  vraies données (skippé si cache RP / IRIS / bâti absents). *Gates certaines*
  (exactes) : conservation ménages, `population_allouee` recalculé ==
  `groupby(home_id)` (D2), 0 orphelin, 1 référent/ménage ordinaire. *Gates à
  seuil* : constantes EMPIRIQUES calibrées sur la zone 9 IRIS, 10 seeds (valeurs
  mesurées en commentaire) — identités comptables serrées (headcount, taille
  moyenne, y c. **par IRIS**), forme des distributions plus lâche (arbitrage du
  tilt, cf. décisions itération 2). Diagnostic runtime : `IRIS_FIT_WARN_THRESHOLD`
  + un WARNING par IRIS mal calé (l'utilisateur sait quels quartiers sont moins
  fiables).

---

## Chantier suivant — brancher MOBPRO (calage domicile-travail)

Objectif : remplacer le `decay_m=3000 m` posé à la main par un calage sur les flux
réels domicile-travail INSEE, pour des trajets d'agents réalistes.

**État** : `loaders/mobpro.py` est écrit et **testé**, mais **non branché** dans le
pipeline. La base est déjà en cache (`data/cache/mobpro-flux-2022.zip`).

**API existante** : `load_mobpro(communes=None)` → DataFrame avec
`CODGEO` (commune résidence), `DCLT` (commune travail), `NBFLUX_C22_ACTOCC15P`
(nb d'actifs). `communes=[...]` filtre la commune de **résidence**.

**Approche recommandée — affectation en 2 étapes** (Option B des échanges) :
1. Pour un travailleur de commune `c`, tirer sa **commune de travail** `c'` selon
   `P(c'|c) = NBFLUX(c,c') / Σ_c' NBFLUX(c,c')` (distribution MOBPRO).
2. Dans `c'`, choisir le **bâtiment** précis par le gravitaire actuel
   (capacité × exp(-dist/decay_m)) restreint aux lieux de travail de `c'`.
Plus fidèle que de tout faire reposer sur la distance. (Option A plus légère :
ne caler que `decay_m` sur la distribution de distances observée.)

**Briques nécessaires / points d'intégration** :
- **Commune d'un bâtiment** = `code_iris` → `str(int(x)).zfill(9)[:5]`. ⚠️ `code_iris`
  est en **float** dans les sorties (ex. `384710000.0`) ; le `zfill(9)` gère les
  départements < 10 (zéro de tête). Présent sur `result` (domiciles) **et**
  `buildings_all` (lieux de travail).
- `workplaces.identify_workplaces` renvoie déjà les lieux de travail ; il faudra
  leur attacher leur commune (`code_iris[:5]`) pour les regrouper par `c'`.
- Adapter `workplaces.assign_facilities` (ou ajouter une variante MOBPRO-aware)
  pour : grouper les actifs par commune de domicile, tirer `c'` via MOBPRO, puis
  gravité intra-`c'`. Garder le chemin actuel (sans MOBPRO) en repli.
- Câbler dans `matching/agents.py` (passer la table MOBPRO / la matrice `P(c'|c)`)
  et `main.py` `step_env` (charger via `load_mobpro(communes=<communes de la région>)`).
  Les communes de la région = préfixes 5 des `CODE_IRIS` de la config `region`.

**Décisions à trancher** :
- **Flux sortant de la région** : MOBPRO enverra des actifs vers des communes hors
  zone simulée. Choix : (a) clipper aux communes de la région + renormaliser
  `P(c'|c)`, ou (b) modéliser un agent « travaille à l'extérieur » (sort de la
  carte). Pour l'évacuation, (a) est plus simple ; (b) plus réaliste.
- **Commune `c'` sans lieu de travail identifié** dans la zone (aucun bâtiment
  emploi) → repli : gravité globale sur toute la région.
- `decay_m` reste utile pour la gravité **intra-commune** (étape 2).

**Validation** : comparer la distribution des distances domicile-travail générées
et/ou la matrice de flux commune→commune des agents à MOBPRO (étalon).

**Données** : millésime 2022 (couplé au code). Colonnes confirmées ci-dessus.

---

## Bâtiments stratégiques (équipements / POI évacuation) — FAIT

> Ce chantier **remplit les flags `is_strategic` / `is_education`** et la colonne
> `fonction` du contrat `env` (laissés de côté à la création du contrat).
> Implémenté dans `loaders/poi.py` (OSM amenities → footprint BD TOPO), câblé aux
> étapes 5-8 de `step_env`, exporté par `build_env_layer`. Cf. METHODE.md § 8.

**Ce qui est en place.** `fetch_osm_pois` requête Overpass les amenities
stratégiques (hôpital, mairie, caserne, police, gare, stade, culte, EHPAD, centre
commercial, gymnase, préfecture), `match_pois_to_buildings` les apparie au footprint
BD TOPO (point-in-polygon pour les nœuds, chevauchement ≥ 30 % pour les polygones),
`match_education_to_buildings` fait de même pour les points BPE, et `merge_fonctions`
fusionne les sources avec **priorité OSM > éducation > BDNB ERP** (`load_bdnb_erp`,
grands ERP cat 1-2 en fallback). Résultat : colonne `fonction` (str) + flags dérivés
sur la couche `env`.

**Constat d'origine (vérifié).** Les grands bâtiments-clés de l'évacuation **ne sont
PAS filtrés** — ils sont dans la couche `buildings`. Mais la BD TOPO *bâti* est **sans
nom** : ils sortent en `Indifférencié` anonymes. Exemples mesurés : Hôtel de Ville
de Grenoble = `BATIMENT…302526662`, Indifférencié 923 m² (BDNB → `travail`) ; Stade
des Alpes = Indifférencié 3 989 m² (BDNB → `travail`). Présents, mais non
**identifiables** comme mairie / stade / hôpital — ce que la colonne `fonction`
résout désormais.

**Objectif.** Tagger une **fonction stratégique** sur les bâtiments (colonne
`fonction`/`poi_type` sur la couche `buildings`) : mairie/préfecture, hôpital/clinique,
caserne pompiers (SDIS), police/gendarmerie, gare, lieu de culte, gymnase/stade,
centre commercial, EHPAD… — pour modéliser PC de crise, refuges, points de
rassemblement et populations vulnérables.

**Hors périmètre — refuge vertical (déféré crise/simulateur).** La cascade
séisme→inondation porte l'injonction contradictoire **« évacuer les bâtiments » vs
« évacuation verticale »**, d'où l'envie de tagger un **refuge vertical**. **Ce
n'est PAS le rôle du générateur** : (1) « au-dessus de la cote » dépend d'un aléa
(input du module crise), le générateur reste threat-agnostic ; (2) « refuge sûr »
a **trois définitions non équivalentes** — sûr objectivement (crise), perçu par
les autorités (message d'alerte), perçu par les agents (simulateur) — trancher ici
injecterait un biais orienté-résultat (cf. principe de design METHODE.md). Le
générateur livre seulement le **substrat physique** (`hauteur`, `n_etages`,
`z_min_sol`, `z_max_toit`, déjà dans le contrat `env`) ; chaque module aval calcule
sa propre aptitude au refuge à partir de ces faits bruts.

**Sources, par ROI :**
1. **OSM amenities nommées** (quick win, gratuit, bien mappé pour les gros
   équipements) : `amenity` ∈ {townhall, hospital, clinic, fire_station, police,
   community_centre, place_of_worship, prefecture}, `leisure` ∈ {stadium,
   sports_centre}, `railway=station` / `public_transport`, `emergency=*`,
   `social_facility` (EHPAD). Méthode : requête Overpass (réutiliser `_run_overpass`)
   + appariement au footprint BD TOPO par chevauchement (cf. `match_osm_to_bdtopo`),
   nœud → point-in-polygon. Produit `poi_type` par bâtiment.
2. **BDNB déjà en main** : `dpe_ter_categorie_erp_dpe_tertiaire` (1-5) +
   `dpe_ter_type_erp_dpe_tertiaire` → **grands ERP** (cat 1-2 = fort accueil public,
   ex. salles, commerces, enseignement) ; `monument_historique` / `merimee_*` →
   patrimoine (les « bâtiments historiques » repérés par l'utilisateur).
3. **BPE** (même mécanisme que `bpe.py` éducation) : santé (domaine D : hôpitaux,
   EHPAD), sport (F), services publics (A). Géolocalisé, codes `TYPEQU` nets.
4. **BD TOPO équipement / ZOA** (NATURE + toponyme : « Mairie », « Stade »,
   « Hôpital »…) : autoritaire et déjà nommé, mais demande de charger la couche
   *équipement/zone* complète (on n'a que le bâti). La BDNB l'expose en partie
   (`bdtopo_equ_l_nature_detaillee` / `bdtopo_zoa_l_nature` / `_toponyme`) mais
   **mal renseignée** sur la zone (mesuré : `bdtopo_equ_l_nature_detaillee` = 0).

**Réalisé** : OSM (1) pour les landmarks à fort enjeu (hôpital, mairie, caserne,
police, gare, stade, culte, gymnase) → colonne `fonction` sur `env`, enrichi par
l'ERP BDNB (2, fallback). **Pistes restantes (optionnelles)** : patrimoine BDNB
(`monument_historique` / `merimee_*`), BPE santé/sport/services publics (3),
capacité d'accueil réelle (ERP / BPE / surface) si on veut dimensionner des refuges
côté module aval.

## Indifférencié — état et leviers restants

**Méthode corrigée (fait).** Le vrai problème n'était pas que les données : le
filtre **fabriquait** des logements (défaut « Indifférencié → résidentiel » + plancher
`NB_LOGTS ≥ 1`), donc le moindre abri de 9 m² absorbait de la population. Corrigé
par une **porte de plausibilité d'habitation** dans `filter_residential`
(`min_floor_area`, déf. 25 m² de plancher) : un « Indifférencié » sans signal positif
n'est gardé comme logement que s'il est assez grand. **Générique** (taille = BD TOPO,
aucune dépendance), reproductible : ~17 800 faux logements écartés sur la région
(emprise médiane des Indifférencié peuplés : 17 m²). Sur la zone Île Verte, ~12 % de
la population était allouée à des Indifférencié, concentrée sur peu de gros bâtiments.

**Bases ouvertes épuisées pour le *fond*.** Tentatives mesurées et écartées :
`ffo_bat_usage_niveau_1_txt` (+69), colonnes BDNB sup. equ/zoa/merimee (+334),
requête OSM élargie shop/office/amenity (+116). Les petits Indifférencié restants
sont de vrais abris/garages sans contenu — ne pas réinvestir là.

**Leviers restants (génériques, optionnels), par ROI :**
- **Adresse BAN** (via BDNB `nb_adresse_valid_ban`, déjà mesuré) : très discriminant
  (166/246 Indifférencié peuplés sans adresse). Pourrait durcir la porte (taille ET
  adresse) — mais couple le filtre à la BDNB (fichier optionnel). À garder hors du
  cœur générique, comme raffinement quand la BDNB est présente.
- **Imputation par voisins** pour le résidu des **gros** Indifférencié (≈50 ≥ 50 m²
  peuplés sur la zone) : leur donner l'usage majoritaire du voisinage. Générique.
- **Override manuel optionnel** (Solution 2) : CSV `ID → {résidentiel, travail,
  inutile}` versionné, pour la poignée de très gros cas spécifiques (nouveaux
  bureaux, bâtiments historiques) absents des bases. **Optionnel** : le programme
  reste « pris tel quel » sans lui.
- **SIRENE** (non testé) : établissements actifs géolocalisés + tranches d'effectifs
  → renforcerait les **lieux de travail** ET donnerait une **capacité réelle**
  (remplace le proxy uniforme). Autre objectif que les logements.
