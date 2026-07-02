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
   [5] ALLOCATION ─────── INSEE ↔ bâtiments (ménages d'abord, plus fort reste)
        │                 → population + CSP + âge par bâtiment
        │
   [6] AGENTS ─────────── 1 individu / habitant : âge, CSP, activité
        │                 → destination par gravité (travail/école/collège/lycée/crèche)
        │
   [7] EXPORT ─────────── buildings.* (tous + population) · buildings_light.* · agents.*
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
| OSM réseau routier (osmnx) | live | `loaders/roads.py` | Réseau piéton + voiture | Active |
| **BPE** (INSEE) | 2024 | `loaders/bpe.py` | Équipements éducatifs géolocalisés (crèche/école/collège/lycée) | Active |
| **BDNB** (CSTB) | local ~2,5 Go | `loaders/bdnb.py` | Usage (qualifie les Indifférencié) **+ matériaux/période** (`ffo_bat_*`) pour la vulnérabilité (NN D1-D5) | Active (optionnelle) |
| MOBPRO (INSEE) | 2022 | `loaders/mobpro.py` | Flux domicile-travail commune→commune | **Réservée** (non branchée) |
| **RP détail — Individus canton-ou-ville** (INSEE) | 2022 | `loaders/rp_detail.py` | **Échantillon de ménages réels** (membres, âge, CSP, rôle) pour la génération sample-based en ménages | **Chantier ménages** (briques 1-2 faites, non branchée) |

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
Microsimulation par **tirage** (PAS un IPF / échantillon type gospl) :
- 1 agent par tête de `population_allouee` ;
- **âge** : multinomiale ∝ `age_*` puis âge entier uniforme dans la tranche ;
  **repli** sur la distribution globale si un bâtiment a 0 dans chaque tranche
  (artefact du plus fort reste marge-par-marge) → évite les "inconnu" ;
- **CSP** : tirée pour les 18+ ∝ `csp_*` ; `mineur` pour < 18 ;
- **activité** (destination de jour) :

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

### 3.8 Destinations — modèle gravitaire (`matching/workplaces.py`)
Inspiré du localisateur `spll`/`GravityFunction` de Genstar.
`P(destination j | domicile i) ∝ capacité_j × exp(−distance_ij / decay_m)`
(les agents d'un même domicile partagent le vecteur de proba).
- **travail** : bâtiments dont `USAGE1/2` ∈ usages d'emploi (Commercial et services,
  Industriel, Agricole, Religieux, Sportif) **+ BDNB `travail`** (`extra_ids`,
  récupère ~+4 500 lieux de travail parmi les Indifférencié). Capacité = surface de
  plancher. `decay_m` = **3000 m**.
- **crèche/école/collège/lycée** : équipements **BPE** géolocalisés (`bpe.py`,
  codes `TYPEQU`). Capacité unité (capacité d'accueil BPE non renseignée pour
  l'enseignement → proximité dominante). `education.decay_m` = **1200 m**.
  Collège/lycée retombent sur le pool « école » si vide.

### 3.9 Sorties (`output/export.py`)
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
  rôle (`is_residential`, `is_workplace` ; éducation/stratégique différés), **profil
  vertical** (`n_etages`, `hauteur`, `emprise_m2`, `z_min_sol`, `z_max_toit` → la
  capacité de refuge est calculée **en aval** selon la cote d'inondation, donc côté
  crisis_gen), **vulnérabilité** (`mat_mur`, `mat_toit`, `annee_construction` — BDNB
  ffo, pour le NN D1-D5). Consommé par les modules crise ET simulation.
- **`agents.{gpkg,geojson,csv}`** — 1 ligne = 1 individu : `agent_id`, `home_id`,
  `age`, `age_band`, `csp`, `activity`, `is_worker`, `dest_id`, `dest_x/y`, `dist_m`,
  géométrie = point domicile. **Source unique de la population** : le contrat `env`
  ne porte plus le détail (population/bâtiment = `groupby(home_id)`). Le *nombre*
  par bâtiment est déterministe (le seed change qui sont les agents, pas combien).

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
| `workplaces.decay_m` | 3000 | config.yaml | décroissance distance domicile-travail |
| `workplaces.seed` | 42 | config.yaml | reproductibilité des tirages |
| `education.decay_m` | 1200 | config.yaml | décroissance distance école (plus court) |
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

- Âge et CSP tirés comme **marges indépendantes** (pas de table jointe âge×CSP type
  IPF/gospl) → marges respectées en espérance, pas exactement. **Sans ménages** :
  enfants sans parent rattaché. → adressé par le **chantier ménages** (sample-based,
  briques 1-2 faites ; § 8 et `claude.md`).
- **Gravitaires non calibrés** (decays au doigt mouillé) — calage MOBPRO réservé.
- Capacité des équipements éducatifs **uniforme** (donnée BPE absente).
- 15-17 ans comptés en **mineurs côté CSP** (léger écart avec la pop INSEE « 15+ »).
- Pas de **fuite hors région** ni de **télétravail** ; tout actif travaille dans la zone.
- ~23 000 Indifférencié restants laissés tels quels (structures sans contenu).
- `code_iris` stocké en **float** dans les sorties (commune = `str(int(x)).zfill(9)[:5]`).
- Sur-segmentation BD TOPO : les **slivers** (vitrines/avancées) sont recollés
  (§ 9) ; la **sur-division de grandes parties comparables** est laissée telle
  quelle (pas de fusion sûre sans risquer de coller des bâtiments distincts).

## 8. Chantiers à venir

0. **Population en ménages (sample-based) — PRIORITAIRE, briques 1-2 faites.**
   Remplacer le tirage d'**individus indépendants** (§ 3.7, sans ménage ni lien
   parent→enfant) par des **ménages réels** tirés d'un échantillon INSEE et calés
   par IRIS. Débloque le **regroupement familial** en évacuation.
   - **Brique 1 (faite)** `loaders/rp_detail.py` : échantillon de ménages réels
     (RP détail Individus canton-ou-ville 2022 ; `hh_id`, `role` via `LPRM`, âge,
     CSP, `IPONDI`).
   - **Brique 2 (faite)** `matching/households.py` : **IPU** qui repondère le pool
     sur les marges `age_*`/`csp_*` de chaque IRIS (collapse en types ; validé
     médiane 0,12 %).
   - **Brique 3 (à faire)** : par bâtiment, tirer `menages_alloues` ménages ∝ poids
     IPU de l'IRIS, instancier les membres. **Décision** : honorer le *nombre* de
     ménages et laisser la population suivre les vraies tailles (option A).
   - **Brique 4 (à faire)** : refonte `agents.py` (`household_id` + rôle) + câblage.
   - Détail complet, décisions verrouillées et **pièges à ne pas refaire** (faux
     ménages de communautés `LPRM=Z`, label `csp_chomeurs_inactifs`=retraités, ne
     pas clipper les centenaires) : `claude.md` § « population en ménages ».
1. **Brancher MOBPRO** — caler le gravitaire domicile-travail (affectation 2 étapes :
   tirer la commune de travail via flux MOBPRO, puis bâtiment par gravité). Notes
   détaillées dans `claude.md` (§ Chantier MOBPRO).
2. **Flags de rôle `is_education` / `is_strategic`** (contrat `env`, différés) :
   - `is_education` : matching **BPE → footprint** (point-in-polygon) — BPE est
     aujourd'hui en points séparés, pas rattaché aux bâtiments.
   - `is_strategic` : tagger mairie/hôpital/caserne/gare/stade via **OSM nommé +
     BDNB** (ERP, monument historique) — central pour la QR « évacuer vs évacuation
     verticale ». Notes dans `claude.md`.
3. **Capacité de refuge vertical** — *hors env_generator*, côté **crisis_gen** :
   dérivée du profil vertical (`n_etages`, `z_*`) et de la **cote d'inondation du
   scénario** (rupture totale ≠ partielle → refuges différents). env_generator
   n'expose que les ingrédients (cf. § 3.9).
4. **Migrer `casualties.py` vers les agents** quand il rejoindra crisis_gen : il lit
   aujourd'hui `population_allouee` sur `buildings.*` ; demain `groupby home_id` sur
   `agents.csv`.
5. ~~Absorption de slivers~~ — **fait** (§ 9). ~~Contrat de sortie `env`~~ — **fait** (§ 3.9).

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
