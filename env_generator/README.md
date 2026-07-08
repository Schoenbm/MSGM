# env_generator

Génère l'**environnement de simulation** de la métropole grenobloise pour l'ABM
d'évacuation (module 1 de **MSGM**) : **réseau routier** (piéton + voiture),
**bâtiments** qualifiés (logement / lieu de travail / école / bâtiment
stratégique), et **population synthétique en ménages** localisée au bâtiment —
chaque individu a un âge, une CSP, un rôle familial (référent / conjoint /
enfant), une activité de jour et une destination (lieu de travail ou école).

Tout est produit en **Lambert-93 (EPSG:2154)** — non négociable (standard
national, jumeau numérique). Le modèle GAMA (module 2) consomme les sorties par
fichiers, sans appel direct.

> **Comment lire la doc.** Ce README explique **ce que fait le pipeline, avec
> quel algorithme et quelles données** — c'est le document à lire pour
> comprendre. [METHODE.md](METHODE.md) est la référence détaillée (mesures,
> décisions prises et écartées, limites). [claude.md](claude.md) porte les
> conventions de développement.

---

## Démarrage rapide

```bash
pip install -r requirements.txt
python -m src.main --step env --verbose      # le -m est obligatoire
```

Le pipeline est piloté par `config.yaml` ; les sorties vont dans `output.dir`
(défaut `data/processed/env/`). Les données distantes (INSEE, IGN, OSM) sont
téléchargées et mises en cache automatiquement ; seuls trois fichiers locaux
sont attendus dans `data/` (bâti BD TOPO, contours IRIS, et la BDNB —
optionnelle).

```bash
python -m pytest -q                          # tests (doivent rester verts)
```

---

## Vue d'ensemble

`--step env` enchaîne 8 étapes :

```
config.yaml (2 zones : population ⊆ région)
     │
[1] ZONE POPULATION   IRIS + recensement INSEE ──► démographie par IRIS (âge, CSP, ménages)
[2] ZONE RÉGION       emprise + buffer          ──► terrain d'évacuation (plus large)
[3] RÉSEAU ROUTIER    OSM via osmnx             ──► roads_walk.gpkg, roads_drive.gpkg
[4] BÂTIMENTS         BD TOPO + OSM + BDNB      ──► bâti nettoyé, usage qualifié, nb de logements
[5] POI               OSM + BPE + BDNB ERP      ──► fonction des bâtiments (mairie, hôpital, école…)
[6] ALLOCATION        INSEE ↔ bâtiments         ──► ménages et population par bâtiment
[7] AGENTS            RP détail + IPU + MOBPRO  ──► individus en ménages réels + destinations
[8] EXPORT            trois couches             ──► buildings.* · env.* · agents.*
```

Deux zones distinctes, et c'est voulu : la **région** est le terrain complet
(routes + bâtiments partout), la **population** est le sous-ensemble
effectivement peuplé. Un bâtiment hors zone population avec `population = 0`
est normal, pas une erreur.

---

## Comment ça marche, étape par étape

### 1-2. Les deux zones (`loaders/iris.py`)

La zone se définit par une liste de codes dans `config.yaml`, au niveau `iris`,
`commune` ou `departement` (résolus par préfixe du `CODE_IRIS`). Les géométries
viennent d'une **source unique** — shapefile local s'il couvre tous les codes,
sinon bascule *entière* sur le téléchargement IGN après confirmation (jamais de
mélange local + téléchargement : les millésimes d'IRIS sont incompatibles).

Sur la zone population, chaque IRIS reçoit ses attributs du recensement
INSEE RP 2022 : population totale, nombre de ménages, 10 tranches d'**âge**,
8 catégories **CSP**, et la **composition des ménages** (personne seule, couple
avec/sans enfants, famille monoparentale… — utilisée à l'étape 7).

La région est l'union des géométries + un buffer (300 m par défaut, pour ne pas
couper le réseau routier en bord de carte). Un garde-fou vérifie
`population ⊆ région`.

### 3. Réseau routier (`loaders/roads.py`)

Deux graphes OSM via `osmnx` — `walk` (piéton) et `drive` (voiture) — sur
l'emprise région, simplifiés et reprojetés en Lambert-93.

### 4. Bâtiments (`loaders/buildings.py`, `bdnb.py`, `osm.py`)

Le **socle est la BD TOPO** (IGN) : géométries, usage (`USAGE1/2`), nombre de
logements (`NB_LOGTS`), hauteur, étages. Quatre traitements successifs :

1. **Dé-fragmentation** (`absorb_slivers`) : la BD TOPO sur-segmente (vitrines,
   avancées comptées comme bâtiments séparés). Un petit fragment (< 20 m²)
   collé à un voisin ≥ 5 fois plus grand, avec ≥ 50 % de son contour partagé,
   est fusionné dans ce voisin. Conservateur par construction : les maisons
   mitoyennes (tailles comparables) ne fusionnent jamais, la surface bâtie est
   conservée à 100 %, les exits sont préservés.
2. **Qualification d'usage**. Beaucoup de bâtiments BD TOPO sont
   « Indifférencié » (sans usage). La **BDNB** (base nationale des bâtiments,
   CSTB) les qualifie : résidentiel / travail / annexe. **OSM** apporte en
   complément les tags `building:flats` / `building:levels`.
3. **Filtre résidentiel** (`filter_residential`) : un bâtiment est un logement
   si la BD TOPO le dit résidentiel ; sinon s'il a un signal positif (OSM ou
   BDNB) ; sinon, un « Indifférencié » sans signal négatif n'est gardé que s'il
   est **plausiblement habitable** : surface de plancher (emprise × étages)
   ≥ 25 m². Cette « porte de plausibilité » évite de transformer un abri de
   jardin en logement (~17 800 faux logements écartés sur la métropole).
4. **Estimation du nombre de logements** quand `NB_LOGTS` manque : tags OSM,
   sinon surface de plancher ÷ surface moyenne par logement (médiane locale de
   l'IRIS si assez de bâtiments connus, sinon 160 m² par défaut).

La BDNB fournit aussi **matériaux et année de construction** (`mat_mur`,
`mat_toit`, `annee_construction`), exportés pour le module crise (prédiction de
dommages D1-D5). Sans BDNB, le pipeline tourne — en moins précis.

### 5. Fonction des bâtiments — POI (`loaders/poi.py`)

La BD TOPO bâti est **anonyme** : l'Hôtel de Ville ou le Stade des Alpes y sont
des « Indifférencié » sans nom. Cette étape tagge une colonne `fonction` :

- **OSM** : amenities stratégiques nommées (hôpital, mairie, caserne, police,
  gare, stade, lieu de culte, EHPAD, gymnase…), appariées au footprint BD TOPO
  (point dans polygone, ou chevauchement ≥ 30 % pour les polygones) ;
- **BPE** (INSEE) : équipements éducatifs géolocalisés → `creche`, `ecole`,
  `college`, `lycee` ;
- **BDNB ERP** (grands établissements recevant du public, cat. 1-2) en repli.

Priorité en cas de conflit : OSM > éducation > BDNB. Les flags `is_strategic`
et `is_education` du contrat `env` en dérivent.

### 6. Allocation de la population (`matching/spatial_join.py`, `allocator.py`)

Chaque bâtiment résidentiel est rattaché à son IRIS (centroïde dans le
polygone), puis les totaux INSEE de l'IRIS sont répartis entre ses bâtiments
**au prorata du nombre de logements**, avec la **méthode du plus fort reste** :
on distribue les parts entières, puis les unités restantes vont aux plus gros
restes décimaux. Résultat : des effectifs **entiers** dont la somme retombe
*exactement* sur le chiffre INSEE. Ménages d'abord (`menages_alloues`), puis
population, tranches d'âge et CSP par la même méthode.

### 7. Agents — des ménages réels (`matching/agents.py`, `households.py`, `workplaces.py`)

C'est le cœur du générateur. On ne fabrique pas des individus statistiques :
on tire des **ménages réels** observés par l'INSEE.

**a. Le pool** (`loaders/rp_detail.py`). Le fichier détail du recensement
(« Individus canton-ou-ville » 2022) contient un échantillon anonymisé de
vrais ménages : pour chaque membre, âge, CSP et rôle dans le ménage (référent,
conjoint, enfant…). On charge ceux des communes de la zone. Les corrélations
réelles (âge du parent × présence d'enfants × CSP × taille) sont donc
**observées, pas reconstruites**.

**b. Le calage par IRIS** (`households.py`, algorithme **IPU** — Iterative
Proportional Updating, Ye et al. 2009). Le pool est à la maille
canton-ou-ville, plus grosse que l'IRIS ; pour reproduire le profil *local*,
on **repondère** : on cherche un poids par ménage tel que les totaux pondérés
retombent sur les chiffres INSEE de l'IRIS, à la fois au niveau **individus**
(10 tranches d'âge + 8 CSP) et au niveau **ménages** (nombre total et
composition : personne seule, couple avec/sans enfants, monoparentale…).
L'algorithme cycle sur chaque contrainte en remettant à l'échelle les ménages
qui y contribuent, jusqu'à convergence. Un **ajustement final**
(« tilt » exponentiel sur la taille, `w′ = w·exp(θ·taille)`) garantit
exactement la taille moyenne de ménage `population/ménages` de l'IRIS — parce
que les tables INSEE peuvent se contredire entre elles et qu'on fait alors
primer population et nombre de ménages (un WARNING signale les IRIS
concernés).

**c. Le tirage.** Par bâtiment, exactement `menages_alloues` ménages sont
tirés (avec remise, proportionnellement aux poids), et leurs membres sont
instanciés **tels quels** : âge, CSP, rôle observés. Chaque tirage reçoit un
`household_id` unique ; le `role` exporté donne le lien parent→enfant pour le
regroupement familial en évacuation côté GAMA. Le nombre de ménages par
bâtiment est déterministe ; la population suit les vraies tailles des ménages
tirés (reproductible à seed fixé). Validé sur données réelles : écart de
population ~0,5 % médian par rapport à l'INSEE.

**d. Activité et destination.** Chaque individu reçoit une activité par âge :
0-2 ans → crèche, 3-10 → école, 11-14 → collège, 15-17 → lycée, 18-62 actif
occupé → travail, sinon (inactif, chômeur, retraité > 62 ans) → reste au
domicile. Puis une destination :

- **Travail — en 2 étapes, calé sur les flux réels** (`workplaces.py`) :
  (1) la **commune** de travail est tirée selon la matrice `P(commune de
  travail | commune de domicile)` construite depuis **MOBPRO** (flux
  domicile-travail INSEE observés), restreinte aux communes de la région et
  renormalisée — personne ne sort de la carte ; (2) le **bâtiment** dans cette
  commune est tiré par un modèle gravitaire : `P(j) ∝ capacité_j ×
  exp(−distance/3000 m)`, parmi les lieux de travail (usages BD TOPO d'emploi
  + bâtiments « travail » BDNB, capacité = surface de plancher). Si MOBPRO est
  indisponible, repli sur la gravité seule.
- **École / crèche** : équipement BPE du bon niveau, gravité de proximité pure
  (decay 1200 m — on scolarise près de chez soi).

**Chemin de repli.** Si le pool RP est indisponible (ou en mode Filosofi), on
retombe sur l'ancien tirage d'**individus indépendants** : âge et CSP tirés
dans les marges du bâtiment, sans structure de ménage.

### 8. Export (`output/export.py`) — trois couches

Pensées pour la chaîne à 3 modules (environnement → crise → simulation) :

| Couche | Contenu | Pour qui |
|---|---|---|
| `buildings.{geojson,csv,shp}` | **Tous** les bâtiments, toutes colonnes : population, flag résidentiel, usage BDNB, marges âge/CSP (recalculées depuis les agents + version allocateur en `*_alloc`), matériaux/période | Inspection QGIS, `casualties.py` |
| `env.{geojson,csv,shp}` | **Contrat de simulation** curaté, sans population : `ID`, géométrie, flags de rôle (`is_residential`, `is_workplace`, `is_education`, `is_strategic`), `fonction`, profil vertical (`n_etages`, `hauteur`, `z_min_sol`, `z_max_toit`), vulnérabilité (`mat_mur`, `mat_toit`, `annee_construction`) | Modules crise **et** simulation |
| `agents.{gpkg,geojson,csv}` | 1 ligne = 1 individu : `agent_id`, `home_id`, `household_id`, `role`, `age`, `age_band`, `csp`, `activity`, `is_worker`, `dest_id`, `dest_x/y`, `dist_m`, point domicile | Simulation GAMA |

La **source unique de la population** est la couche agents :
`population_allouee` de `buildings.*` est recalculé depuis
`groupby(home_id)` — une seule vérité, cohérente entre les couches.

S'y ajoutent les intermédiaires : `population_iris.gpkg` (IRIS + attributs
INSEE), `region.gpkg` (emprise), `roads_walk.gpkg` / `roads_drive.gpkg`.

> **Sous QGIS** : les `.gpkg`/`.shp` sont en Lambert-93, les `.geojson` en
> WGS84 (même contenu). Les fichiers suffixés d'un hash
> (`roads_walk_<hash>.gpkg`, `osm_buildings_<hash>.geojson`…) sont des
> **caches** — les livrables sont les versions sans hash.

---

## Les données utilisées

| Source | Millésime | Rôle | Accès |
|---|---|---|---|
| **BD TOPO bâti** (IGN) | local | Socle du bâti : géométrie, usage, logements, hauteur | fichier `data/batim_grenoble.shp` |
| **CONTOURS-IRIS** (IGN) | 2024 | Géométries des IRIS | local, sinon téléchargé |
| **RP base-ic pop + logement** (INSEE) | 2022 | Âge, CSP, ménages par IRIS | téléchargé (cache) |
| **RP base-ic couples-familles-ménages** (INSEE) | 2022 | Composition des ménages par IRIS (contraintes IPU) | téléchargé (cache) |
| **RP détail individus canton-ou-ville** (INSEE) | 2022 | **Échantillon de ménages réels** (membres, âge, CSP, rôle) | téléchargé (cache) |
| **MOBPRO** (INSEE) | 2022 | Flux domicile-travail commune→commune | téléchargé (cache) |
| **BPE** (INSEE) | 2024 | Équipements éducatifs géolocalisés | téléchargé (cache) |
| **BDNB** (CSTB) | local ~2,5 Go | Usage des « Indifférencié », matériaux/période, ERP | fichier `data/BDNB/` — **optionnel** |
| **OSM** (Overpass / osmnx) | live | Réseau routier ; tags bâtiments ; POI stratégiques | téléchargé (cache) |
| **Filosofi carroyé 200 m** (INSEE) | 2019 | Population carroyée (source alternative, `--source filosofi`) | fichier local |

Tous les téléchargements passent par un **cache unique à écriture atomique**
(`loaders/cache.py`) : un download interrompu ne laisse jamais un fichier
corrompu pris pour valide.

> **Limite millésime** : les URLs sont dans `config.yaml`, mais les noms de
> colonnes INSEE (`P22_*`, `C22_*`) sont codés en dur pour 2022 — changer
> d'année demande de toucher au code (`loaders/iris.py`, `rp_detail.py`).

---

## Configuration (`config.yaml`)

L'essentiel :

```yaml
crs: "EPSG:2154"

sources:                        # fichiers locaux
  contours_iris: "./data/contour_iris.shp"
  buildings:     "./data/batim_grenoble.shp"
  bdnb:          "./data/BDNB/gpkg/bdnb.gpkg"   # optionnel

datasets:                       # URLs INSEE/IGN (millésime couplé au code)
  contours_iris_url: ...
  insee_pop_url: ...
  insee_logement_url: ...
  insee_familles_url: ...       # composition des ménages (IPU)

zones:
  region:                       # terrain complet (161 IRIS métropole)
    selector: { type: iris, codes: [...] }
    buffer_m: 300
  population:                   # sous-ensemble peuplé
    selector: { type: iris, codes: [...] }

network:  { types: [walk, drive], simplify: true }

buildings:
  min_dwelling_floor_area_m2: 25   # porte de plausibilité d'habitation
  absorb_slivers: true             # dé-fragmentation conservatrice
  sliver_max_area_m2: 20

workplaces:
  usages: ["Commercial et services", "Industriel", "Agricole", "Religieux", "Sportif"]
  decay_m: 3000                    # gravité intra-commune (l'inter-communes est calé MOBPRO)
  seed: 42

education: { decay_m: 1200 }

output: { dir: "./data/processed/env", format: gpkg }
```

Si des codes IRIS manquent du shapefile local, le pipeline **s'arrête** en les
listant (souvent une faute de frappe) ; `--yes` autorise la bascule sur le
téléchargement IGN France entière.

---

## Ancien pipeline (`--step all`) — hérité

Chaîne historique par étapes séparées (`load`, `match`, `export`, `visualize`,
`compare`, `casualties`), sans réseau routier ni agents :

```bash
python -m src.main --step all --source iris --iris 381850208,381850209
python -m src.main --step casualties --damage-csv degats.csv   # victimes D1..D5
```

`--source filosofi` alloue sur les carreaux 200 m au lieu des IRIS. Utile pour
la validation (`compare`) et les victimes (`casualties`) ; pour tout le reste,
préférer `--step env`.

---

## Ce qui est simplifié (assumé)

Les principales approximations, détaillées dans [METHODE.md](METHODE.md) § 7 :
retraite couperet à 62 ans ; niveaux scolaires découpés par âge ; capacité des
écoles uniforme (donnée BPE absente) ; capacité d'un lieu de travail = surface
de plancher (pas d'effectifs SIRENE) ; pas de flux entrants ni de télétravail
(les actifs travaillant hors zone sont réaffectés en interne — choix
« évacuation » : personne ne sort de la carte) ; communautés (EHPAD…) tirées
comme personnes seules en logement ordinaire (placement dédié = chantier
futur) ; decays gravitaires intra-commune (3000 m) et éducation (1200 m) posés
à la main.
