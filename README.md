# MSGM — Macro Sim of Grenoble Metro

> ⚠️ **README placeholder** — rédigé par IA pour poser le décor, à reprendre et
> affiner. Ne fait pas foi sur les détails ; il sert surtout à donner la philosophie
> du dépôt et la carte des modules.

## De quoi il s'agit

MSGM est la chaîne de production qui prépare et fait tourner une **simulation
multi-agents d'évacuation de la métropole grenobloise**. Le cadre est une thèse sur
le **comportement humain en crise en cascade** : un séisme majeur endommage la ville
puis provoque une rupture partielle de barrage, dont la vague inonde Grenoble. On
cherche à comprendre comment les individus se comportent face à des **injonctions
contradictoires** (évacuer les bâtiments vs. évacuation verticale) et comment ces
comportements modifient la dynamique collective de l'évacuation.

Finalité applicative : alimenter un **jumeau numérique / serious game** pour préparer
et sensibiliser les acteurs du risque.

Tout est produit en **Lambert-93 (EPSG:2154)** — standard national français, non
négociable côté jumeau numérique.

## Philosophie : 2-3 modules découplés, taillés pour GAMA

Le projet est volontairement découpé en modules indépendants qui communiquent par
**fichiers** (`.gpkg` en Lambert-93), pas par appels directs. Chacun peut évoluer,
être rejoué ou remplacé sans casser les autres. La cible de simulation est **GAMA** :
les modules amont produisent exactement ce qu'un modèle GAMA sait charger (couches
géographiques + tables d'attributs), et le module de simulation est écrit en GAML.

| Module | Rôle | État |
|---|---|---|
| [`env_generator/`](env_generator/) | **Module 1 — environnement.** Génère le terrain de simulation : réseau routier, bâtiments, et population synthétique localisée. Sorties `.gpkg` consommées par GAMA. | **Opérationnel** |
| _simulateur_ (GAMA / GAML) | **Module 2 — agents.** Charge l'environnement et fait évoluer les agents (déplacement, décision BDI, états d'alerte) pendant la crise. | À venir |
| _crise_ | **Module 3 — aléas.** Décrit la cascade (cinétique séisme → inondation) qui s'impose à l'environnement et aux agents. Périmètre encore à définir (module à part ou intégré au simulateur). | À définir |

Le découplage par fichiers permet aussi de **figer le terrain** (généré une fois) et
de faire varier la population ou les scénarios de crise par-dessus, sans tout
régénérer.

### Ce qu'`env_generator` sait faire aujourd'hui

C'est le seul module opérationnel à ce stade. À partir d'une simple liste de **codes
IRIS** (dans `config.yaml`), il produit automatiquement, en Lambert-93 :

- **Population synthétique localisée à partir des données INSEE** (recensement RP
  2022) : population et ménages désagrégés par bâtiment résidentiel, avec ventilation
  par **tranche d'âge** et par **CSP**. L'allocation se fait au prorata du nombre de
  logements (`NB_LOGTS`, estimé via hauteur/surface BD TOPO et enrichi par OSM quand
  dispo), avec une méthode du plus fort reste garantissant des effectifs entiers.
- **Bâtiments** issus de la BD TOPO (IGN), filtrés (résidentiel / non), enrichis par
  OpenStreetMap (`building:flats`, `building:levels`).
- **Réseau routier** piéton et voiture (via OSM / osmnx).
- **Deux zones distinctes** : une *région* (terrain d'évacuation complet, plus large)
  et une *population* (sous-ensemble effectivement peuplé) — on simule sur un grand
  terrain une population concentrée, sans tout peupler.

Les géométries IRIS proviennent d'une **source unique** (shapefile local, ou
téléchargement IGN en repli sur confirmation) pour éviter de mélanger des millésimes
incompatibles.

## Démarrage rapide (module 1)

```bash
cd env_generator
pip install -r requirements.txt
python -m src.main --step env --verbose
```

Détails, configuration et sorties : [`env_generator/README.md`](env_generator/README.md).
