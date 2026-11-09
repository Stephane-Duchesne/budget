# Budget

Gestion de budget personnelle qui fonctionne entièrement sur votre ordinateur :
import des relevés Desjardins (PDF, QIF, QFX), catégorisation automatique sans
service externe, et rapports.

Document de conception : https://claude.ai/artifact/KW97ggjtHViWomwD9bnVAP

## État

Étape a, en cours : les lecteurs de relevés.

| Lecteur | Fichiers | État |
| --- | --- | --- |
| PDF Visa Desjardins | relevé mensuel de carte | testé sur un relevé réel (122 transactions, totaux du sommaire concordants) |
| QIF | relevés Desjardins, sorties de StatementToQif.ps1, export complet de Quicken | testé sur des exemples |
| QFX / OFX | compte chèques Desjardins, export Quicken | testé sur un exemple synthétique |

## Essayer

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows : .venv\Scripts\pip ...
.venv/bin/python -m budget.cli lire releve.pdf
.venv/bin/python -m budget.cli qif releve.pdf   # remplace StatementToQif.ps1
.venv/bin/python -m pytest
```

## Confidentialité

Les vrais relevés servant aux tests vont dans `tests/fixtures/private/`, qui
n'est jamais versionné ; les tests qui en ont besoin sont sautés s'ils sont absents.
