# env_generator — Méthode (référence)

> **But de ce document.** Trace méthodologique **complète et lisible** de la
> génération de l'environnement (carte + population synthétique de la métropole
> grenobloise). Sert à garder la **vue d'ensemble** : ce qu'on fait, comment,
> avec quelles données, quels paramètres, quelles décisions (prises ET écartées),
> et ce qui reste à faire. Mis à jour à chaque changement de méthode.
>
> Principe de design (utilisateur) : construire le référentiel le plus **fidèle au
> réel possible**, **indépendamment de l'usage** en simulation, pour ne pas injecter
> de biais orienté-résultat. Limite assumée : on ne modélise pas l'insignifiant
> (abris). Corollaire pratique : **dans le doute, choix conservateur** (ne pas
> inventer de géométrie, ne pas fusionner des bâtiments distincts).

---

## 1. Vue d'ensemble

Pipeline `python -m src.main --step env` (piloté par `config.yaml`), tout en
**Lambert-93 (EPSG:2154)**. Sorties consommées par le modèle GAMA.

```
   config.yaml (2 zones : population ⊆ région)
        │
   [1] ZONE POPULATION ── IRIS + INSEE RP 2022 ──────► démographie/IRIS (âge, CSP, ménages)
        │
   [2] ZONE RÉGION ────── emprise + buffer ──────────► garde-fou population ⊆ région
        │
   [3] RÉSEAU ROUTIER ─── OSM/osmnx (walk + drive) ──► roads_*.gpkg
        │
   [4] BÂTIMENTS ──────── BD TOPO (socle)
        │                 + OSM (flats/levels, tag usage)
        │                 + BDNB (usage : travail/residentiel/annexe)
        │                 → filtre résidentiel + porte de plausibilité (taille)
        │                 → estimation NB_LOGTS
        │
   [5] POI ────────────── OSM amenities + BPE + BDNB ERP ─► colonne `fonction`
        │                 (mairie, hôpital, école…) → flags is_strategic/is_education
        │
   [6] ALLOCATION ─────── INSEE ↔ bâtiments (ménages d'abord, plus fort reste)
        │                 → population + CSP + âge par bâtiment
        │
   [7] AGENTS ─────────── MÉNAGES RÉELS (RP détail + IPU par IRIS) : membres
        │                 instanciés (âge, CSP, rôle → lien parent→enfant),
        │                 household_id/role ; activité ; destination travail en
        │                 2 étapes MOBPRO + gravité, éducation par gravité BPE
        │                 (repli sans pool RP : tirage d'individus indépendants)
        │
   [8] EXPORT ─────────── buildings.* (tous + population recalculée des agents) ·
                          env.* (contrat simu) · agents.*
```

---

## 2. Données sources

| Source | Millésime | Loader | Rôle | Statut |
|---|---|---|---|---|
| **BD TOPO bâti** (IGN) | local | `loaders/buildings.py` | **Socle du bâti** (géométrie, USAGE1/2, NB_LOGTS, HAUTEUR, NB_ETAGES) | Active — principale |
| CONTOURS-IRIS (IGN) | 2024 | `loaders/iris.py` | Géométries IRIS (zones pop/région) | Active |
| RP base-ic pop+logement (INSEE) | 2022 | `loaders/iris.py` | Âge, CSP, ménages par IRIS | Active |
| Filosofi carroyé 200 m (INSEE) | 2019 | `loaders/insee.py` | Population carroyée (`--source filosofi`) | Active (alt.) |
| OSM bâtiments (Overpass) | live | `loaders/osm.py` | Enrichissement flats/levels + tag usage | Active — secondaire |
| OSM POI (Overpass) | live | `loaders/poi.py` | Amenities stratégiques nommées (hôpital, mairie, caserne, gare, stade…) → colonne `fonction` (§ 3.9) | Active |
| OSM réseau routier (osmnx) | live | `loaders/roads.py` | Réseau piéton + voiture | Active |
| **BPE** (INSEE) | 2024 | `loaders/bpe.py` | Équipements éducatifs géolocalisés (crèche/école/collège/lycée) | Active |
| **BDNB** (CSTB) | local ~2,5 Go | `loaders/bdnb.py` | Usage (qualifie les Indifférencié) **+ matériaux/période** (`ffo_bat_*`) pour la vulnérabilité (NN D1-D5) | Active (optionnelle) |
| MOBPRO (INSEE) | 2022 | `loaders/mobpro.py` | Flux domicile-travail commune→commune → matrice P(c'\|c) de l'affectation travail (§ 3.8) | **Active** |
| **RP détail — Individus canton-ou-ville** (INSEE) | 2022 | `loaders/rp_detail.py` | **Échantillon de ménages réels** (membres, âge, CSP, rôle) — génération sample-based en ménages (§ 3.7) | **Active** — chemin principal des agents |
| **RP base-ic couples-familles-ménages** (INSEE) | 2022 | `loaders/iris.py` | **Composition des ménages par IRIS** (`C22_MEN*` → `men_*`) : contraintes ménage de l'IPU (distribution des tailles, § 3.7) | **Active** (optionnelle — repli nombre seul) |
| **Carte scolaire collèges publics** (data.education.gouv.fr) | live | `loaders/carte_scolaire.py` | **Secteurs de recrutement par rue** (commune + voie + plage de numéros + parité → UAI) : affectation collège déterministe (§ 3.8bis) | **Active** (optionnelle — repli gravité) |
| **Établissements 1er/2nd degré** (data.education.gouv.fr) | live | `loaders/carte_scolaire.py` | **Collèges géolocalisés avec UAI** (public + privé, Lambert-93) — remplace le BPE pour le seul niveau collège (le BPE n'a pas l'UAI, donc pas de clé vers la carte scolaire) | **Active** (optionnelle — repli gravité) |
| **BAN** (adresse.data.gouv.fr) | live | `loaders/ban.py` | **Adresses ponctuelles** (numéro + voie + commune, Lambert-93) : rattachement bâtiment → adresse (≤ 50 m) pour résoudre le secteur collège (§ 3.8bis) | **Active** (optionnelle — repli gravité) |

Toutes les sources distantes passent par le **cache unique** (`loaders/cache.py`,
`ensure_cached` : check local → contrôle d'intégrité → (re)production atomique).
Millésimes **couplés au code** (noms de colonnes INSEE en dur).

---

## 3. Méthode, étape par étape

### 3.1 Zones — population & région (`loaders/iris.py`)
- **Sélecteur multi-niveaux** `iris | commune | departement`, résolu par préfixe du
  `CODE_IRIS` sur une **source unique** (shapefile local, sinon download IGN ;
  jamais de fusion local+download → millésimes incompatibles).
- **population** : IRIS + jointure INSEE RP 2022 → `Ind_total`, `taille_moy_menage`,
  tranches `age_*` (10), `csp_*` (8 : 6 actifs occupés + chômeurs/inactifs + autres).
- **région** : emprise unifiée + `buffer_m` (défaut 300 m). Garde-fou
  `validate_subset` : population ⊆ région (warn sinon).

### 3.2 Réseau routier (`loaders/roads.py`)
- osmnx, types `walk` + `drive`, `simplify=true`, reprojeté Lambert-93.

### 3.3 Bâtiments — qualification d'usage (`loaders/bdnb.py`, `osm.py`)
- **BDNB** `usage_principal_bdnb_open` → catégorie : `Tertiaire/Secondaire/Primaire`
  → **travail** ; `Résidentiel individuel/collectif` → **residentiel** ;
  `Dépendance` → **annexe**. Carte `ID bâtiment → catégorie`. Optionnelle.
- **OSM** : appariement par chevauchement (`match_osm_to_bdtopo`, seuil 50 % de la
  surface OSM) → tag `osm_building`, `osm_flats`, `osm_levels`. NB : la requête OSM
  ne récupère **que** les bâtiments tagués `building:flats`/`levels` → apport faible
  hors logements.

### 3.4 Bâtiments — filtre résidentiel (`loaders/buildings.py::filter_residential`)
Un bâtiment est gardé comme **logement** si :
1. `USAGE1 == "Résidentiel"` (BD TOPO, fiable) → toujours ; **sinon** :
2. signal **positif** résidentiel (tag OSM résidentiel **ou** BDNB `residentiel`) ; **ou**
3. `Indifférencié` **sans** signal négatif (ni OSM non-rés. ni BDNB travail/annexe)
   **ET plausiblement habitable** : surface de plancher (emprise × étages)
   ≥ `min_floor_area` (**défaut 25 m²**) — **porte de plausibilité** qui évite de
   transformer un abri/garage en logement.
- Générique : la taille vient de la BD TOPO (sans dépendance BDNB).
- Effet mesuré (région) : ~17 800 faux logements écartés.

### 3.5 Estimation NB_LOGTS (`loaders/buildings.py::estimate_nb_logts`)
Priorité décroissante : `NB_LOGTS` BD TOPO → `osm_flats` → **estimation surfacique**
(emprise × nb étages / surface-moyenne-par-logement). Surface/logt = médiane par
IRIS si ≥ `SURFACE_MIN_IRIS` (5) bâtiments connus, sinon fallback
`SURFACE_MOY_DEFAULT` (160 m²). Nb d'étages : `NB_ETAGES` > `HAUTEUR`/3 > `osm_levels` > 1.

### 3.6 Allocation de la population (`matching/`)
- **Jointure** (`spatial_join.py`) : centroïde bâtiment ∈ carreau/IRIS → porte
  `Ind_total`, `P22_MEN`, `taille_moy_menage`, `age_*`, `csp_*`.
- **Allocation** (`allocator.py`) : **ménages d'abord** (répartis ∝ `NB_LOGTS` par
  cellule, **méthode du plus fort reste** → entiers ≥ 0, somme exacte) puis
  population = ménages × taille moyenne. CSP et âge alloués ∝ `NB_LOGTS` (même
  méthode). **Conservation** : Σ population allouée = population INSEE.

### 3.7 Génération des agents (`matching/agents.py`)

**Chemin principal — ménages réels (`generate_household_agents`, sample-based).**
Actif dès que `menages_alloues` existe (mode IRIS) et que le pool RP détail est
chargeable (sinon : repli individus, plus bas).
1. **Pool** : `load_rp_households` (RP détail 2022, communes de la zone
   population) → membres de ménages réels (`hh_id`, âge, CSP, rôle `LPRM`).
   *Hygiène avant tirage* (`_sanitize_pool`) : écarte les rares ménages qui
   violeraient les invariants ou dégénéreraient le calage (âge inconnu ; membre
   **> 99 ans** — hors de toute contrainte d'âge, direction que l'IPU exploite
   pour gonfler les tailles « gratuitement », mesuré +9,2 % ; ménage sans adulte —
   mineur seul, internat ; ménage ordinaire sans référent unique). ~0,1 ‰, loggé.
2. **Calage par IRIS (IPU ménage×individu, `HouseholdReweighter.weights_for`)** —
   la forme complète de Ye et al. 2009 :
   - marges *individus* `age_*`/`csp_*` de l'IRIS (sommes des colonnes bâtiment,
     exactes par construction du plus fort reste) ;
   - marges *ménages* : **composition** (`men_*` ← base-ic couples-familles-
     ménages `C22_MEN*`, classification du pool par rôles LPRM via
     `household_types`), rescalées à Σ = `menages_alloues` (= P22_MEN) — elles
     pincent le nombre ET la distribution des tailles de ménage. Sans données de
     composition : repli sur la contrainte du seul nombre (`n_households`) ;
   - **tilt final de taille moyenne** (`w′ = w·exp(θ·taille)`, θ par bisection) :
     E[taille pondérée] = P22_POP/P22_MEN exactement, même quand les cibles INSEE
     se contredisent (C22 vs P22, âge vs composition) — la population et le
     nombre de ménages priment, la forme d'âge encaisse l'écart résiduel là où
     les données sont incohérentes (WARNING par IRIS). `n_iter=300`.
3. **Tirage (option A)** : par bâtiment, exactement `menages_alloues` ménages,
   **avec remise** dans le pool entier ∝ poids IPU ; les membres réels sont
   instanciés tels quels (âge, CSP, rôle observés — la table jointe âge×CSP×ménage
   est **observée**, pas reconstruite). `household_id` unique **par tirage**,
   `role` = referent/conjoint/enfant/… → **lien parent→enfant exporté** pour le
   regroupement familial GAMA. Ages > 99 étiquetés `age_80p` en sortie.
4. **Cohérence aval** : `population_allouee` **recalculé depuis les agents**
   (`groupby(home_id).size()`, D2 — une seule vérité de population) ; marges
   `age_*`/`csp_*` par bâtiment recalées sur les agents, version allocateur
   conservée en `*_alloc` (D3, `update_building_demographics`) pour mesurer
   l'écart (~50 % de masse redistribuée entre bâtiments à l'intérieur des IRIS —
   attendu : l'allocateur lissait ∝ NB_LOGTS, les ménages réels concentrent).
5. **Diagnostics runtime** : WARNING par IRIS dont > `IRIS_FIT_WARN_THRESHOLD`
   (5 %) de la masse des marges reste mal placée ; repli D5 par IRIS (poids
   IPONDI bruts, structure familiale préservée) si l'IPU rend des poids dégénérés.

Propriétés : nb de **ménages**/bâtiment déterministe (= `menages_alloues`), la
**population** suit les vraies tailles (stochastique, reproductible à seed fixé) ;
validé sur données réelles (`test_households_realdata.py`) : headcount à ~0,5 %
médian de l'INSEE (max 2,3 % sur 10 seeds), 0 orphelin, conservation exacte.

**Chemin de repli — individus indépendants (`generate_agents`).** Mode Filosofi
(pas de `menages_alloues`) ou pool RP indisponible. Microsimulation par **tirage**
(PAS un IPF / échantillon type gospl) :
- 1 agent par tête de `population_allouee` ;
- **âge** : multinomiale ∝ `age_*` puis âge entier uniforme dans la tranche ;
  **repli** sur la distribution globale si un bâtiment a 0 dans chaque tranche
  (artefact du plus fort reste marge-par-marge) → évite les "inconnu" ;
- **CSP** : tirée pour les 18+ ∝ `csp_*` ; `mineur` pour < 18 ; pas de ménage
  (`household_id`/`role` vides).

**Commun aux deux chemins** — **activité** (destination de jour) :

  | Âge / statut | activité |
  |---|---|
  | 0–2 | `creche` |
  | 3–10 | `ecole` |
  | 11–14 | `college` |
  | 15–17 | `lycee` |
  | 18–62 actif occupé | `travail` |
  | inactif / chômeur / **> 62 ans (retraite)** | `aucune` (reste au domicile) |

  Niveaux scolaires découpés par âge, **répartition uniforme** (le tirage uniforme
  dans la tranche INSEE donne mécaniquement le partage, ex. 11-17 → ~57 % collège /
  43 % lycée).
- **Conservation** : nb d'agents = `population_allouee` totale.

### 3.8 Destinations — MOBPRO + modèle gravitaire (`matching/workplaces.py`)
Gravité (inspirée du localisateur `spll`/`GravityFunction` de Genstar) :
`P(destination j | domicile i) ∝ capacité_j × exp(−distance_ij / decay_m)`
(les agents d'un même domicile partagent le vecteur de proba).
- **travail — affectation en 2 étapes calée MOBPRO** (`assign_workplaces_mobpro`,
  chemin principal) :
  1. tirer la **commune de travail** `c'` selon `P(c'|c)` — matrice des flux
     domicile-travail MOBPRO 2022 (`build_commute_matrix`), **clippée aux communes
     de la région et renormalisée par ligne** (les actifs qui travailleraient hors
     zone sont réaffectés aux destinations internes ; les entrants extérieurs =
     chantier futur) ;
  2. dans `c'`, tirer le **bâtiment** par la gravité ci-dessus **restreinte aux
     lieux de travail de `c'`** (`decay_m` = 3000 m, intra-commune).
  Replis : commune de résidence absente de la matrice, ou `c'` sans lieu de
  travail identifié → gravité globale sur toute la région ; MOBPRO indisponible
  (cache/réseau) → gravité pure (chemin historique). Un comparatif des plus gros
  flux générés vs MOBPRO est loggé à chaque run (validation à l'œil).
  Lieux de travail : bâtiments dont `USAGE1/2` ∈ usages d'emploi (Commercial et
  services, Industriel, Agricole, Religieux, Sportif) **+ BDNB `travail`**
  (`extra_ids`, récupère ~+4 500 lieux de travail parmi les Indifférencié).
  Capacité = surface de plancher. Commune d'un bâtiment = `code_iris[:5]`
  (l'attribut BD TOPO de `buildings_all`, lacunaire : les bâtiments sans code
  restent tirables au repli global mais pas à l'étape intra-commune).
- **crèche/école/lycée** : équipements **BPE** géolocalisés (`bpe.py`, codes
  `TYPEQU`), gravité de proximité pure. Capacité unité (capacité d'accueil BPE
  non renseignée pour l'enseignement → proximité dominante).
  `education.decay_m` = **1200 m**. Collège/lycée retombent sur le pool « école »
  si vide.
- **collège** : carte scolaire officielle, pas la gravité — cf. § 3.8bis
  (la gravité BPE reste le repli quand les sources sont indisponibles).

### 3.8bis Collège — affectation par la carte scolaire (`matching/schooling.py`)

En France, l'affectation en collège public suit une **carte scolaire officielle**
(secteur de recrutement par adresse), pas la proximité : la gravité pure était
irréaliste pour ce niveau. Chemin principal (`assign_colleges_carte_scolaire`,
même esprit 2 étapes que MOBPRO) :

1. statut **public/privé** tiré **par ménage** — `Bernoulli(private_rate)`,
   **`private_rate` = 0,20** (taux national DEPP ; pas de taux communal
   disponible pour l'Isère, on ne cherche pas plus précis). Les collégiens d'un
   même ménage partagent le statut (une fratrie va dans le même secteur, elle
   n'est pas tirée agent par agent) ; repli par agent si `household_id` est
   absent (chemin individus indépendants) ;
2. **privé** → gravité (§ 3.8) restreinte aux collèges `secteur = "Privé"`
   (repli gravité globale si aucun privé dans la zone) ;
3. **public** → résolution **déterministe** par la carte scolaire : adresse BAN
   du bâtiment domicile (jointure plus-proche-voisin ≤ **50 m**,
   `attach_building_addresses`) → ligne de secteur (commune + nom de voie
   normalisé + numéro dans `[n_de_voie_debut, n_de_voie_fin]` + parité P/I/PI,
   `resolve_college_sector`) → collège UAI. Une commune en `secteur_unique="O"`
   envoie toute la commune au même collège (la rue est ignorée). Tout échec —
   pas d'adresse BAN dans le rayon, rue inconnue, numéro hors plage ou parité
   incompatible, UAI hors zone d'étude — → repli gravité restreinte aux collèges
   publics (ou globale si vide).

Décisions verrouillées :
- **Granularité = la RUE, pas l'IRIS** (vérifié sur les données : une même
  commune, souvent un seul IRIS, peut être coupée en plusieurs secteurs par rue —
  ex. Apprieu, 38013). D'où la jointure BAN obligatoire, pas de raccourci IRIS.
  La normalisation des noms de voie (`src/text_utils.py::normalize_voie` :
  majuscules, accents/apostrophes/tirets neutralisés) est appliquée à
  l'identique des deux côtés (BAN et carte scolaire) — validé sur données
  réelles : > 60 % des adresses de Grenoble résolvent directement.
- **Nouvelle source établissements** (avec UAI) pour le seul niveau collège :
  le BPE n'a pas d'identifiant UAI, donc aucune clé de jointure vers la carte
  scolaire. École/lycée/crèche restent sur le BPE, inchangés (aucune carte
  scolaire ouverte pour ces niveaux en Isère — dataset national = collèges
  publics uniquement, vérifié).
- **Capacité = 1,0 constante** : aucune capacité fiable dans aucune source (le
  `CAPACITE_D_ACCUEIL` du BPE est toujours `_Z` pour les collèges/lycées).
- **Philosophie défensive** (comme MOBPRO) : les 3 sources (secteurs,
  établissements, BAN) se chargent dans un try/except de `step_env` ; n'importe
  laquelle indisponible ou vide → gravité pure BPE (comportement historique),
  le pipeline tourne sans réseau. `education.carte_scolaire.enabled: false`
  désactive sans même tenter le téléchargement.

### 3.9 Fonction des bâtiments — POI stratégiques & éducation (`loaders/poi.py`)

La BD TOPO bâti est **anonyme** (l'Hôtel de Ville = « Indifférencié » sans nom) ;
cette étape (5 de `step_env`) tagge une colonne **`fonction`** (str) par bâtiment,
dont dérivent les flags `is_strategic` / `is_education` du contrat `env` :
- **OSM** (`fetch_osm_pois`) : amenities stratégiques nommées — hôpital, mairie,
  caserne, police, gare, stade, culte, EHPAD, centre commercial, gymnase,
  préfecture. Appariement au footprint BD TOPO (`match_pois_to_buildings`) :
  nœud → point-in-polygon ; polygone → chevauchement ≥ 30 %.
- **BPE** (`match_education_to_buildings`) : les équipements éducatifs de § 3.8
  rapportés au footprint → `creche` / `ecole` / `college` / `lycee`.
- **BDNB ERP** (`load_bdnb_erp`) : grands ERP catégories 1-2 en **fallback**.
- Fusion `merge_fonctions`, priorité **OSM > éducation > BDNB ERP**.

Le refuge vertical n'est PAS taggé ici (dépend de l'aléa → module crise) : le
générateur n'expose que le substrat physique (profil vertical, § 3.10).

### 3.10 Sorties (`output/export.py`)
Architecture en 3 modules : **env_generator** (ce module, **indépendant de la
crise**) → **crisis_gen** (NN D1-D5, inondation, capacité de refuge selon la cote)
→ **simulation**. D'où la séparation faits physiques / population / crise.
- **`buildings.{geojson,csv,shp}`** — couche **complète** (superset d'inspection
  QGIS + source pop actuelle de `casualties.py`) : tous les bâtiments, toutes
  colonnes, `population_allouee` (0 si non-logement), `residentiel`, `usage_bdnb`,
  CSP/âge, matériaux/période BDNB. (`merge_buildings` fusionne l'ancien couple
  `buildings_full`/`buildings_all`.)
- **`env.{geojson,csv,shp}`** — **contrat de simulation** (curaté, *sans* population,
  indépendant de la crise ; remplace `buildings_light`) : `ID`, géométrie, flags de
  rôle (`is_residential`, `is_workplace`, `is_education`, `is_strategic`) +
  colonne **`fonction`** (§ 3.9), **profil
  vertical** (`n_etages`, `hauteur`, `emprise_m2`, `z_min_sol`, `z_max_toit` → la
  capacité de refuge est calculée **en aval** selon la cote d'inondation, donc côté
  crisis_gen), **vulnérabilité** (`mat_mur`, `mat_toit`, `annee_construction` — BDNB
  ffo, pour le NN D1-D5). Consommé par les modules crise ET simulation.
- **`agents.{gpkg,geojson,csv}`** — 1 ligne = 1 individu : `agent_id`, `home_id`,
  **`household_id`**, **`role`** (referent/conjoint/enfant/… — lien parent→enfant
  pour le regroupement familial), `age`, `age_band`, `csp`, `activity`, `is_worker`,
  `dest_id`, `dest_x/y`, `dist_m`, géométrie = point domicile. **Source unique de
  la population** : `population_allouee` de `buildings.*` est recopié depuis
  `groupby(home_id)` (D2). Le *nombre de ménages* par bâtiment est déterministe
  (= `menages_alloues`) ; la population suit les tailles réelles des ménages tirés
  (le seed change qui sont les agents). `buildings.*` porte aussi les marges
  `age_*`/`csp_*` recalculées des agents + la version allocateur `*_alloc` (D3 ;
  suffixe `_a` dans le `.shp`, limite 10 caractères).

---

## 4. Paramètres

| Paramètre | Valeur | Où | Effet |
|---|---|---|---|
| `region.buffer_m` | 300 m | config.yaml | bordure de l'emprise (réseau de bord) |
| `network.types` | walk, drive | config.yaml | réseaux routiers générés |
| `buildings.min_dwelling_floor_area_m2` | 25 | config.yaml | porte de plausibilité d'habitation |
| `buildings.absorb_slivers` | true | config.yaml | recolle les fragments (vitrines) aux gros voisins |
| `buildings.sliver_max_area_m2` | **20** | config.yaml | taille max d'un fragment absorbable (constante lib `SLIVER_MAX_AREA` = 15) |
| `_SNAP_TOL` / `_SIZE_RATIO` / `_BOUNDARY_SHARE` | 0,3 m / 5 / 0,5 | buildings.py | collé / hôte ≥ 5× / part de contour (totale) mini |
| `workplaces.usages` | Commercial/Industriel/Agricole/Religieux/Sportif | config.yaml | usages BD TOPO = lieux de travail |
| `workplaces.decay_m` | 3000 | config.yaml | décroissance distance domicile-travail (gravité **intra-commune** depuis MOBPRO ; échelle globale en repli) |
| `workplaces.seed` | 42 | config.yaml | reproductibilité des tirages |
| `education.decay_m` | 1200 | config.yaml | décroissance distance école (plus court) |
| `education.carte_scolaire.enabled` | true | config.yaml | affectation collège par carte scolaire (§ 3.8bis) ; false = gravité pure sans téléchargement |
| `education.carte_scolaire.private_rate` | 0,20 | config.yaml | part de collégiens dans le privé (taux national DEPP) |
| `ADDRESS_MAX_DIST_M` | 50 m | schooling.py | rayon de rattachement bâtiment → adresse BAN (constante) |
| `sources.bdnb` | chemin gpkg | config.yaml | BDNB (optionnelle) |
| `ADULT_MIN_AGE` | 18 | agents.py | seuil adulte (CSP) |
| `RETIREMENT_AGE` | 62 | agents.py | au-delà → retraite (pas de travail) |
| bornes scolaires | 2 / 10 / 14 / 17 | agents.py | crèche / école / collège / lycée |
| `SURFACE_MOY_DEFAULT` | 160 m² | buildings.py | surface/logement de secours |
| `SURFACE_MIN_IRIS` | 5 | buildings.py | nb mini pour calibrer localement |

---

## 5. Journal des décisions (session 2026-06)

- **Grain individuel** pour les agents (vs agrégat) : sortie liste d'agents,
  exploitable ou non par le module 2.
- **Activité par âge** + **retraite couperet 62 ans**, **adulte 18 ans**.
- **BPE** = source autoritaire des écoles (remplace l'heuristique nom/isced OSM).
- **BDNB** = qualification des Indifférencié (logements ET lieux de travail).
- **Porte de plausibilité (25 m²)** : ne plus fabriquer de logement à partir d'un abri.
- **Fusion `buildings_full` + `buildings_all`** en une couche `buildings` (le flag
  `residentiel` porte la distinction).
- **Gravitaire exponentiel**, decay travail 3000 m / éducation 1200 m (non calibrés).

### Session 2026-07 — chantier ménages, briques 3-4

- **Génération en MÉNAGES réels = chemin principal** (`generate_household_agents`),
  décisions verrouillées D1-D6/V1-V5 appliquées (option A : nb de ménages exact,
  population émergente ; détail : `claude.md` § « population en ménages »).
- **Contrainte de nombre de ménages dans l'IPU** (`weights_for(...,
  n_households=P22_MEN)`, Ye et al. 2009). Mesuré sans elle : Σ poids = +9 à +20 %
  vs P22_MEN → taille moyenne 1,43-1,50 au lieu de ~1,7 → **population tirée
  −15,5 %**. Avec elle : headcount ~0,5 % médian (max 2,3 %, 10 seeds, bruit de
  tirage) ; `n_iter=300` (80 laissaient −0,29 % de biais résiduel).
- **Itération 2 — le nombre ne suffit pas, il faut la TAILLE.** Mesuré (zone
  9 IRIS) : avec le seul nombre contraint, la repondération sur l'âge déforme la
  distribution des tailles — taille moyenne +5,4 % zone (population +4,3 %),
  **+31 % sur un IRIS** ; un contrôle zone-globale masquait ces compensations
  (leçon : tester PAR IRIS). Fix en trois pièces (cf. § 3.7 et `claude.md`) :
  **composition des ménages dans l'IPU** (C22_MEN* par IRIS — la taille 1/2/3/4/5+
  n'existe pas à l'IRIS, vérifié), **hygiène >99 ans** (direction dégénérée),
  **tilt final de taille moyenne** (les cibles INSEE peuvent être mutuellement
  incohérentes : C22_MEN jusqu'à +38 % de P22_MEN — aucun ordre de contraintes ne
  s'en sort, mesuré ; le tilt garantit taille et population, la forme d'âge
  encaisse localement, TV ≤ 0,16 sur l'IRIS le plus incohérent, ~0 ailleurs).
- **`code_iris` = IRIS d'allocation** (réécrit par `spatial_join` depuis la cellule
  de jointure, str 9 car.) : l'attribut du shapefile BD TOPO était lacunaire
  (23/552 bâtiments peuplés sans code sur la zone test ; 0 désaccord sinon).
- **Hygiène du pool avant tirage** : ménages sans adulte / sans référent unique /
  à âge inconnu écartés (~0,1 ‰) — imposé par les invariants « 0 orphelin » et
  « 1 référent par ménage ».
- **D3 mesuré** : ~50 % de la masse âge/CSP redistribuée entre bâtiments (à
  l'intérieur des IRIS, totaux conservés) — l'allocateur lissait ∝ NB_LOGTS, les
  ménages réels concentrent. Version allocateur gardée en `*_alloc`.

### Session 2026-07 — chantier MOBPRO (affectation domicile-travail)

- **Affectation travail en 2 étapes** (option B, § 3.8) : commune de travail tirée
  sur les flux réels MOBPRO `P(c'|c)`, puis bâtiment par gravité intra-commune —
  remplace la gravité pure région-entière au `decay_m` posé à la main (qui
  sur-concentrait le travail près du domicile, aveugle aux vrais bassins d'emploi).
- **Clip région + renormalisation** (`build_commute_matrix`) : les flux vers des
  communes hors zone simulée sont écartés et chaque ligne renormalisée — tous les
  actifs restent dans la zone (choix « évacuation » : pas d'agents qui sortent de
  la carte). Les actifs venant de l'extérieur travailler dans la zone = chantier
  futur, hors périmètre.
- **Replis en cascade** : commune de résidence hors matrice ou commune tirée sans
  lieu de travail → gravité globale ; MOBPRO indisponible → gravité pure (le
  pipeline tourne sans réseau). Comparatif « flux générés vs MOBPRO » loggé à
  chaque run.
- **Commune d'un bâtiment = `code_iris[:5]`** — côté domiciles le code est garanti
  (réécrit par `spatial_join`, str 9 car.) ; côté lieux de travail (`buildings_all`)
  l'attribut BD TOPO est lacunaire → parse tolérant, bâtiments sans code tirables
  seulement au repli global.

### Session 2026-07 — chantier carte scolaire (affectation collège)

- **Collège = carte scolaire officielle, plus la gravité** (§ 3.8bis) : les
  publics sont affectés au collège de secteur de leur adresse (déterministe),
  les privés (Bernoulli 0,20 tiré **par ménage** — fratrie cohérente, taux
  national DEPP) par gravité restreinte au privé. École/lycée/crèche inchangés (aucune carte scolaire ouverte pour ces
  niveaux — vérifié, dataset national = collèges publics uniquement).
- **Granularité = la rue** (vérifié : une commune mono-IRIS peut être coupée en
  plusieurs secteurs par rue, ex. Apprieu 38013) → jointure des bâtiments
  résidentiels à la **BAN** (plus proche voisin ≤ 50 m), pas de raccourci IRIS.
  Normalisation des noms de voie identique des deux côtés
  (`text_utils.normalize_voie`) ; validé sur données réelles (> 60 % des
  adresses de Grenoble résolvent directement).
- **Nouvelle source établissements avec UAI** (data.education.gouv.fr) pour le
  seul niveau collège : le BPE n'a pas d'UAI, donc pas de clé de jointure vers
  la carte scolaire. Capacité = 1,0 constante (le `CAPACITE_D_ACCUEIL` BPE est
  toujours `_Z` pour collèges/lycées — vérifié, ne pas re-chercher).
- **Replis en cascade** (philosophie MOBPRO) : échec de résolution → gravité
  restreinte au public ; pool public/privé vide → gravité globale ; n'importe
  laquelle des 3 sources indisponible → gravité pure BPE (le pipeline tourne
  sans réseau). `education.carte_scolaire.enabled: false` coupe tout.

(Historique fin : `git log` du dépôt.)

## 6. Pistes écartées (avec preuve chiffrée)

- **Qualifier le fond des Indifférencié par plus de données** : épuisé.
  `ffo_bat_usage_niveau_1_txt` → **+69** bâtiments ; colonnes BDNB sup.
  (equ/zoa/merimee) → **+334** ; requête OSM élargie shop/office/amenity → **+116**.
  Les ~23 000 Indifférencié restants sont des structures sans contenu (emprise
  médiane 17 m²). → on s'appuie sur la **taille** (porte de plausibilité), pas sur
  une chasse aux données.
- **Dé-fragmenter en dissolvant par `batiment_groupe` BDNB** : **écarté**. Mesuré :
  56 % des groupes multi-bâtiments sont **disjoints**, **gap médian 6,8 m** → le
  `batiment_groupe` est une **parcelle/complexe**, pas un bâtiment physique. Dissoudre
  casserait les exits et inventerait de la géométrie. (Le `groupe_id` reste utile en
  **attribut** d'identité, sans toucher la géométrie.)

## 7. Hypothèses & limites connues

- ~~Âge et CSP tirés comme marges indépendantes, sans ménages~~ — **résolu** par le
  chantier ménages (§ 3.7 : ménages réels, table jointe observée, lien
  parent→enfant). Ne subsiste que dans le **chemin de repli** (`generate_agents`,
  mode Filosofi / pool RP absent). Limites restantes du chemin ménages : pool à la
  maille canton-ou-ville (un IRIS atypique est approché par repondération, pas par
  des ménages locaux) ; communautés (EHPAD…) tirées comme singletons dans les
  logements ordinaires (placement dédié = chantier futur, D4) ; sur les IRIS où
  les sources INSEE se contredisent (C22 vs P22, mesuré jusqu'à +38 % sur le
  nombre de ménages d'un petit IRIS), la **forme d'âge locale** encaisse l'écart
  (le tilt privilégie population + nombre de ménages) — signalé par le WARNING
  par IRIS ; les **centenaires** (>99 ans) sont absents des agents (~0,01 %).
- **Travail** : la répartition **entre communes** est calée sur MOBPRO (§ 3.8),
  mais le `decay_m` **intra-commune** (3000 m) et le decay éducation (1200 m)
  restent posés à la main ; capacité d'un lieu de travail = surface de plancher
  (pas de densité d'emploi par usage, pas d'effectifs SIRENE).
- Capacité des équipements éducatifs **uniforme** (donnée BPE absente).
- 15-17 ans comptés en **mineurs côté CSP** (léger écart avec la pop INSEE « 15+ »).
- Pas de **fuite hors région** ni de **télétravail** : les flux MOBPRO sortants
  sont réaffectés aux destinations internes (clip + renormalisation), et les
  actifs **entrants** (résidence extérieure, travail dans la zone) ne sont pas
  modélisés.
- ~23 000 Indifférencié restants laissés tels quels (structures sans contenu).
- `code_iris` : réécrit en **str 9 caractères** (IRIS d'allocation) par
  `spatial_join` sur les bâtiments joints ; reste en **float** sur les couches
  amont (`buildings_all`) — commune = `str(int(float(x))).zfill(9)[:5]` marche
  pour les deux.
- Sur-segmentation BD TOPO : les **slivers** (vitrines/avancées) sont recollés
  (§ 9) ; la **sur-division de grandes parties comparables** est laissée telle
  quelle (pas de fusion sûre sans risquer de coller des bâtiments distincts).

## 8. Chantiers à venir

0. ~~**Population en ménages (sample-based)**~~ — **FAIT** (briques 1-4, § 3.7).
   Le tirage d'individus indépendants est remplacé par des **ménages réels**
   (`generate_household_agents` : RP détail + IPU par IRIS avec contrainte du
   nombre de ménages + tirage option A), `household_id`/`role` exportés → débloque
   le **regroupement familial** en évacuation côté GAMA. L'ancien chemin reste en
   repli (Filosofi / pool RP absent). Validation : `test_household_agents.py`
   (contrat) + `test_households_realdata.py` (données réelles, seuils calibrés sur
   10 seeds). Décisions verrouillées, décisions de session et **pièges à ne pas
   refaire** : `claude.md` § « population en ménages ». **Reste ouvert (futur)** :
   placement dédié des communautés (EHPAD → bâtiments `fonction=ehpad`, D4).
1. ~~**Brancher MOBPRO**~~ — **FAIT** (§ 3.8) : affectation travail en 2 étapes
   (commune via flux MOBPRO `P(c'|c)` clippés région, puis bâtiment par gravité
   intra-commune), replis gravité globale / gravité pure. Validation :
   `test_commute.py` (contrat) + `test_commute_realdata.py` (vraie base) +
   comparatif flux loggé au run. **Reste ouvert (futur)** : actifs entrants
   (résidence hors zone), calage du `decay_m` intra-commune, capacité
   d'emploi réelle (SIRENE).
2. ~~**Flags de rôle `is_education` / `is_strategic`**~~ — **FAIT** (`loaders/poi.py`,
   étapes 5-8 de `step_env`, colonne `fonction` + flags dans le contrat `env`) :
   - `is_education` : matching **BPE → footprint** (point-in-polygon).
   - `is_strategic` : mairie/hôpital/caserne/gare/stade via **OSM nommé** apparié
     au footprint (+ **BDNB ERP** cat 1-2 en fallback). Priorité OSM > éducation >
     BDNB. Notes dans `claude.md` (§ Bâtiments stratégiques).
3. **Capacité de refuge vertical** — *hors env_generator*, côté **crisis_gen** :
   dérivée du profil vertical (`n_etages`, `z_*`) et de la **cote d'inondation du
   scénario** (rupture totale ≠ partielle → refuges différents). env_generator
   n'expose que les ingrédients (cf. § 3.10).
4. **Migrer `casualties.py` vers les agents** quand il rejoindra crisis_gen : il lit
   aujourd'hui `population_allouee` sur `buildings.*` ; demain `groupby home_id` sur
   `agents.csv`.
5. ~~Absorption de slivers~~ — **fait** (§ 9). ~~Contrat de sortie `env`~~ — **fait** (§ 3.10).

---

## 9. Absorption de slivers (implémenté)

> **Statut : implémenté** (`loaders/buildings.py::absorb_slivers`, branché dans
> `step_env`, toggles `buildings.absorb_slivers` (défaut `true`) et
> `buildings.sliver_max_area_m2` (défaut **20 m²**, cf. § 4). Gère le cas
> **sandwich** (fragment partagé entre 2+ gros voisins : critère 4 sur le contour
> **total**). Tests : `tests/test_buildings.py::TestAbsorbSlivers`.
>
> **Mesuré sur le bâti complet (métropole, 80 295 bât., `boundary_share=0.5`)** :
> à `max_area=20` → **7 387 fragments recollés** (72 908 bâtiments) ;
> à `max_area=15` → **6 076** (dont **+391** sandwiches gagnés par le critère
> total). **Surface bâtie conservée à 100,0000 %** dans les deux cas (unions de
> jointifs uniquement, aucune géométrie inventée ni perdue → exits préservés).
> *(Ancien chiffre, région à 15 m² sans sandwich : 5 688.)*

### Problème
La BD TOPO sur-segmente : (a) petits polygones-bruit (abris, < ~15 m²) ; (b)
« vitrines »/avancées comptées comme bâtiments séparés, jointives ou à quelques
décimètres du bâtiment principal. On veut recoller **ces fragments-là**, sans
toucher au reste.

### Ce qu'on NE fait PAS (et pourquoi)
- **Pas** de dissolve par `batiment_groupe` BDNB (cf. § 6 : sur-groupe, 6,8 m).
- **Pas** de dissolve par simple adjacence : fusionnerait les **maisons mitoyennes**
  (rangées grenobloises) en un bloc → perte de logements/exits réels.
- **Pas** de fermeture morphologique large (`buffer +/−`) : inventerait des murs et
  altèrerait les **exits**.

### Critère retenu — absorption par asymétrie de taille
Un polygone est un **sliver** absorbable s'il vérifie **tout** :
1. **petit** : surface de plancher < `SLIVER_MAX_AREA` (à régler, ~15–20 m²) ;
2. **collé** à un voisin : distance < `SNAP_TOL` (~0,3 m) — jointif ou quasi ;
3. **voisin nettement plus grand** : aire voisin ≥ `SIZE_RATIO` × aire sliver
   (~5×) — garantit qu'on ne fusionne pas deux bâtiments comparables (mitoyens) ;
4. la **part totale** du contour du sliver adossée à ces voisins ≥ `BOUNDARY_SHARE`
   (~50 %) — une avancée du bâtiment, pas un voisin. Le seuil porte sur le contour
   **cumulé** : un fragment enchâssé entre **deux** gros voisins (30 % + 30 %) est
   recollé, dans celui au contour partagé maximal (cas « sandwich »).

Action : **union** du sliver dans le voisin retenu (si plusieurs candidats, celui
qui partage le plus de contour). Sliver **isolé** (aucun grand voisin) : ne pas
fusionner ; le **flaguer** (`is_sliver=True`), suppression seulement si <
`HARD_MIN_AREA` (ex. 5 m²) et l'utilisateur le souhaite.

### Pourquoi ça préserve les exits
On ne recolle qu'une **petite** avancée à son **hôte** → le périmètre extérieur
(donc le contact rue / les exits) est quasi inchangé. On ne bridge pas de gap
(union de jointifs uniquement, à `SNAP_TOL` près). On ne touche pas aux bâtiments
de taille comparable.

### Intégration pipeline
- Étape de **nettoyage géométrique** en amont, sur le bâti de la région
  (juste après le chargement BD TOPO + filtre zone, **avant** `filter_residential`
  et l'allocation), pour que tout l'aval bénéficie de la géométrie nettoyée.
- **Traçabilité** : l'hôte garde son `ID` ; le sliver absorbé disparaît (logué :
  `ID sliver → ID hôte`). Attributs : géométrie = union ; `NB_LOGTS` = somme (un
  sliver a normalement 0 logement) ; usages = ceux de l'hôte.
- Paramètres exposés dans `config.yaml` (section `buildings`).

### Validation (avant de figer)
- **Cas mitoyen** : une rangée de N maisons résidentielles comparables doit rester
  **N bâtiments** (aucune fusion).
- **Cas vitrine** : magasin + petite avancée jointive → **1 bâtiment**.
- **Global** : réduction du nb de bâtiments raisonnable (pas d'effondrement),
  **conservation de la surface bâtie** (Σ aires ≈ constante), aire max d'un sliver
  absorbé bornée.
- Inspection visuelle QGIS sur 3–4 cas connus (un mall, une rangée, une place).

### Risques / cas limites
- Sliver entre **deux** grands voisins → choisir par contour partagé max.
- **Chaînes** de slivers → itérer ou traiter par composantes connexes filtrées en taille.
- Petit **vrai** bâtiment isolé (kiosque) → flagué, pas supprimé (erreur acceptée).
- **Performance** : sjoin de voisinage sur ~80 k bâtiments → index spatial, OK.

### Tests unitaires prévus
- sliver + hôte jointifs → 1 bâtiment, surface = somme.
- deux carrés égaux jointifs → **2** bâtiments (pas de fusion).
- sliver isolé → conservé + `is_sliver=True`.
- sliver entre deux hôtes → rattaché au bon (contour partagé max).

### Paramètres proposés (défauts à valider)
| Paramètre | Valeur proposée | Rôle |
|---|---|---|
| `SLIVER_MAX_AREA` | 15 m² | au-dessus = vrai bâtiment, pas un sliver |
| `SNAP_TOL` | 0,3 m | tolérance « collé » (jointif/quasi) |
| `SIZE_RATIO` | 5 | voisin ≥ 5× le sliver pour absorber |
| `BOUNDARY_SHARE` | 0,5 | part mini du contour du sliver partagée avec l'hôte |
| `HARD_MIN_AREA` | 5 m² | en-deçà, suppression possible si isolé |
