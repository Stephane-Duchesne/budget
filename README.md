# Budget

Gestion de budget personnelle qui fonctionne entièrement sur votre ordinateur :
import des relevés Desjardins (PDF, QIF, QFX), catégorisation automatique sans
service externe, et rapports.

Document de conception : https://claude.ai/artifact/KW97ggjtHViWomwD9bnVAP

## Lancer l'application

```
python -m venv .venv
.venv/bin/pip install -e .                 # Windows : .venv\Scripts\pip install -e .
.venv/bin/budget                            # Windows : .venv\Scripts\budget
```

L'application s'ouvre dans le navigateur à l'adresse http://127.0.0.1:8765 et
n'est accessible que depuis cet ordinateur. Les données sont dans
`~/Budget/budget.sqlite` (variable `BUDGET_DB` pour un autre emplacement).

Déjà disponible : comptes (importés ou manuels) et leurs soldes, import d'un
relevé avec aperçu, contrôle des totaux et dédoublonnage, annulation d'un
import, liste des transactions avec recherche.

## État des lecteurs

| Lecteur | Fichiers | État |
| --- | --- | --- |
| PDF Visa Desjardins | relevé mensuel de carte | testé sur un relevé réel (122 transactions, totaux du sommaire concordants) |
| QIF | relevés Desjardins, sorties de StatementToQif.ps1, export complet de Quicken | testé sur des exemples |
| QFX / OFX | compte chèques Desjardins, export Quicken | testé sur un exemple synthétique |

## Ligne de commande et tests

```
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m budget.cli lire releve.pdf
.venv/bin/python -m budget.cli qif releve.pdf   # remplace StatementToQif.ps1
.venv/bin/python -m pytest
```

## Confidentialité

Les vrais relevés servant aux tests vont dans `tests/fixtures/private/`, qui
n'est jamais versionné ; les tests qui en ont besoin sont sautés s'ils sont absents.
