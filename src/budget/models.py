"""Format commun produit par tous les lecteurs de relevés."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Split:
    """Une ligne de ventilation (QIF : S, $, E)."""
    category: str
    amount: Decimal
    memo: str = ""


@dataclass(frozen=True)
class Transaction:
    """Une transaction normalisée. Montant négatif = sortie d'argent."""
    date: date
    amount: Decimal
    payee: str
    memo: str = ""
    posted_date: date | None = None   # date d'inscription (relevés PDF)
    fitid: str | None = None          # identifiant unique de la banque (QFX)
    category: str | None = None       # catégorie d'origine (QIF Quicken)
    splits: tuple[Split, ...] = ()
    account: str | None = None        # nom de compte présent dans le fichier
    raw: str = ""                     # ligne ou enregistrement source, pour le diagnostic


@dataclass(frozen=True)
class Check:
    """Contrôle de cohérence : total calculé contre total annoncé par le relevé."""
    label: str
    computed: Decimal
    expected: Decimal | None

    @property
    def ok(self) -> bool:
        return self.expected is not None and self.computed == self.expected


@dataclass
class ReadResult:
    """Ce qu'un lecteur renvoie pour un fichier."""
    format: str
    transactions: list[Transaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    statement_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    closing_balance: Decimal | None = None
    account_id: str | None = None

    @property
    def reconciled(self) -> bool:
        """Vrai si tous les contrôles concordent (et qu'il y en a au moins un)."""
        return bool(self.checks) and all(c.ok for c in self.checks)
