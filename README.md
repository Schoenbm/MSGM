# MSGM — Macro Sim of Grenoble Metro

Pipeline de simulation d'évacuation de la **métropole grenobloise**, dans le cadre
d'une thèse sur le comportement humain en **crise en cascade** (séisme → rupture de
barrage → inondation). Objectif : alimenter un jumeau numérique / serious game et
étudier la dynamique collective d'évacuation.

Tout est produit en **Lambert-93 (EPSG:2154)** — standard national, non négociable.

## Modules

| Module | Rôle | État |
|---|---|---|
| [`env_generator/`](env_generator/) | **Module 1** — génère l'environnement de simulation : réseau routier (piéton + voiture), bâtiments, population synthétique localisée (âge + CSP). | Opérationnel |
| _simulateur_ | Module 2 — modèle multi-agents (GAMA / FlameGPU) consommant les sorties `.gpkg` d'`env_generator`. | À venir |
| _crise_ | Modélisation de la cascade (aléas séisme/inondation). | À définir |

Les modules sont **découplés** : `env_generator` produit des fichiers `.gpkg`
(terrain + population) que le simulateur consomme. Voir le README de chaque module
pour son usage propre.

## Démarrage rapide (module 1)

```bash
cd env_generator
pip install -r requirements.txt
python -m src.main --step env --verbose
```

Détails : [`env_generator/README.md`](env_generator/README.md).
