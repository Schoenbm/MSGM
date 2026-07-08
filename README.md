# MSGM — Macro Sim of Grenoble Metro

> Vue d'ensemble du dépôt : philosophie et carte des modules. Le détail de chaque
> module vit dans son propre README (ex. [`env_generator/README.md`](env_generator/README.md)).

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

C'est le seul module opérationnel à ce stade. À partir d'une liste de **codes
IRIS** (dans `config.yaml`), il produit automatiquement, en Lambert-93 :

- **Population synthétique en ménages réels** : des ménages observés du
  recensement (fichier détail RP 2022) sont repondérés par IPU sur les marges de
  chaque IRIS (âge, CSP, nombre et composition des ménages) puis tirés bâtiment
  par bâtiment. Chaque individu a un âge, une CSP, un rôle familial
  (`household_id` + `role` → lien parent→enfant pour le regroupement familial en
  évacuation), une **activité** de jour et une **destination** : lieu de travail
  affecté en 2 étapes sur les flux réels domicile-travail (MOBPRO) + gravité
  intra-commune, école/crèche au plus proche (équipements BPE).
- **Bâtiments** BD TOPO (IGN) nettoyés (dé-fragmentation conservatrice),
  qualifiés par la BDNB et OSM (logement / lieu de travail / annexe), avec une
  colonne `fonction` (mairie, hôpital, caserne, gare, stade, école…) issue
  d'OSM + BPE + BDNB ERP, et les attributs de vulnérabilité (matériaux, année de
  construction) pour le module crise.
- **Réseau routier** piéton et voiture (via OSM / osmnx).
- **Deux zones distinctes** : une *région* (terrain d'évacuation complet, plus
  large) et une *population* (sous-ensemble effectivement peuplé) — on simule
  sur un grand terrain une population concentrée, sans tout peupler.

Trois couches de sortie : `buildings.*` (tous les bâtiments + population),
`env.*` (contrat de simulation curaté) et `agents.*` (1 ligne = 1 individu).
Algorithmes, données sources et sorties détaillés dans
[`env_generator/README.md`](env_generator/README.md) ; référence méthodologique
complète dans [`env_generator/METHODE.md`](env_generator/METHODE.md).

## Démarrage rapide (module 1)

```bash
cd env_generator
pip install -r requirements.txt
python -m src.main --step env --verbose
```

Détails, configuration et sorties : [`env_generator/README.md`](env_generator/README.md).
