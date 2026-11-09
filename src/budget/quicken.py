"""Reprise de l'historique Quicken à partir d'un export QIF complet.

Comptes, catégories (arbre « Parent:Enfant »), transactions confirmées avec
leur catégorie, ventilations et virements liés entre comptes. Le solde
d'ouverture de chaque compte vient de la transaction « Opening Balance » de
Quicken. Les comptes de placement sont créés comme comptes manuels avec leur
solde quand l'export le fournit ; le détail des titres n'est pas repris.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from budget import categorize, importer
from budget.models import ReadResult, Transaction
from budget.readers.qif import QifFile, transfer_account
from budget.store import to_cents
from budget.text import clean_label

FORMAT = "quicken-qif"
LIABILITY_TYPES = {"ccard", "oth l", "loan"}
KNOWN_TYPES = {"bank", "cash", "ccard", "oth a", "oth l", "loan"}
IMPORTED_TYPES = {"bank", "ccard"}
INVESTMENT_TYPES = {"invst", "port", "401(k)/403(b)", "mutual"}
OPENING_PAYEES = {"opening balance", "solde d'ouverture", "solde initial", "solde d’ouverture"}


@dataclass
class AccountSummary:
    name: str
    type: str
    transactions: int = 0
    first: str | None = None
    last: str | None = None
    opening: Decimal = Decimal(0)
    balance: Decimal = Decimal(0)
    investment: bool = False


@dataclass
class Report:
    accounts_created: list[str] = field(default_factory=list)
    categories_created: int = 0
    inserted: dict[str, int] = field(default_factory=dict)
    duplicates: dict[str, int] = field(default_factory=dict)
    transfers_linked: int = 0
    transfers_unmatched: int = 0
    rules_created: int = 0
    proposed: int = 0
    warnings: list[str] = field(default_factory=list)


def _category_name(raw: str | None) -> str | None:
    """« Alimentation:Épicerie/Famille » -> « Alimentation:Épicerie » (la classe Quicken est ignorée)."""
    if not raw or transfer_account(raw):
        return None
    return raw.split("/", 1)[0].strip() or None


def _is_opening(t: Transaction) -> bool:
    return t.payee.strip().lower() in OPENING_PAYEES and transfer_account(t.category) == t.account


def summarize(q: QifFile) -> list[AccountSummary]:
    """Ce que la reprise va créer, compte par compte (pour l'aperçu)."""
    out: dict[str, AccountSummary] = {}
    for a in q.accounts:
        s = out.setdefault(a.name, AccountSummary(a.name, a.type))
        if a.type.lower() in INVESTMENT_TYPES:
            s.investment = True
            if a.balance is not None:
                s.balance = s.opening = a.balance
    for t in q.transactions:
        if t.account is None:
            continue
        s = out.setdefault(t.account, AccountSummary(t.account, ""))
        if _is_opening(t):
            s.opening += t.amount
        else:
            s.transactions += 1
            d = t.date.isoformat()
            s.first = min(s.first or d, d)
            s.last = max(s.last or d, d)
        s.balance += t.amount
    return list(out.values())


class _Categories:
    def __init__(self, db, report: Report):
        self.db, self.report, self.cache = db, report, {}

    def get(self, full_name: str | None, income: bool = False) -> int | None:
        if not full_name:
            return None
        if full_name in self.cache:
            return self.cache[full_name]
        parent_id = None
        path = []
        for part in [p.strip() for p in full_name.split(":") if p.strip()]:
            path.append(part)
            key = ":".join(path)
            if key in self.cache:
                parent_id = self.cache[key]
                continue
            row = self.db.execute(
                "SELECT id FROM categories WHERE name = ? AND parent_id IS ?", (part, parent_id)
            ).fetchone()
            if row:
                parent_id = row["id"]
            else:
                parent_id = self.db.execute(
                    "INSERT INTO categories (name, parent_id, kind) VALUES (?, ?, ?)",
                    (part, parent_id, "income" if income else "expense"),
                ).lastrowid
                self.report.categories_created += 1
            self.cache[key] = parent_id
        return parent_id


def migrate(db, q: QifFile, file_name: str, file_sha256: str) -> Report:
    """Reprend l'export dans la base. Tout ou rien : en cas d'erreur, rien n'est enregistré."""
    report = Report()
    with db:
        cats = _Categories(db, report)
        for c in q.categories:
            cats.get(c.name, c.is_income)

        account_ids: dict[str, int] = {}
        for s in summarize(q):
            row = db.execute("SELECT id FROM accounts WHERE name = ?", (s.name,)).fetchone()
            if row:
                account_ids[s.name] = row["id"]
                continue
            t = s.type.lower()
            account_ids[s.name] = db.execute(
                "INSERT INTO accounts (name, kind, nature, opening_cents, opening_date) VALUES (?,?,?,?,?)",
                (s.name, "imported" if t in IMPORTED_TYPES else "manual",
                 "liability" if t in LIABILITY_TYPES or (t not in KNOWN_TYPES and not s.investment and s.balance < 0)
                 else "asset",
                 to_cents(s.opening), s.first),
            ).lastrowid
            report.accounts_created.append(s.name)
            if s.investment:
                report.warnings.append(
                    f"« {s.name} » est un compte de placement : seul son solde est repris"
                    + ("." if s.balance else " (absent de l'export : à saisir avec « Modifier » sur le tableau de bord).")
                )

        by_account: dict[str, list[Transaction]] = defaultdict(list)
        for t in q.transactions:
            if t.account and not _is_opening(t):
                by_account[t.account].append(t)

        inserted: dict[int, tuple[Transaction, str]] = {}   # id -> (transaction, compte)
        for name, txs in by_account.items():
            acct = account_ids[name]
            pv = importer.preview(db, acct, ReadResult(FORMAT, txs), file_name, file_sha256)
            import_id = db.execute(
                "INSERT INTO imports (account_id, file_name, file_sha256, format) VALUES (?,?,?,?)",
                (acct, file_name, file_sha256, FORMAT),
            ).lastrowid
            n = dup = 0
            for c in pv.candidates:
                if c.status != "new":
                    dup += 1
                    continue
                t = c.tx
                is_transfer = transfer_account(t.category) is not None
                categorized = bool(t.splits) or bool(_category_name(t.category)) or is_transfer
                tx_id = db.execute(
                    "INSERT INTO transactions (account_id, date, amount_cents, payee, memo, label_clean,"
                    " import_id, status, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                    (acct, t.date.isoformat(), to_cents(t.amount), t.payee, t.memo,
                     clean_label(t.payee or t.memo), import_id,
                     "confirmed" if categorized else "to_categorize", t.raw),
                ).lastrowid
                if t.splits:
                    for sp in t.splits:
                        target = transfer_account(sp.category)
                        db.execute(
                            "INSERT INTO splits (transaction_id, category_id, amount_cents, memo) VALUES (?,?,?,?)",
                            (tx_id, cats.get(_category_name(sp.category), sp.amount > 0), to_cents(sp.amount),
                             sp.memo or (f"Virement : {target}" if target else "")),
                        )
                elif _category_name(t.category):
                    db.execute(
                        "INSERT INTO splits (transaction_id, category_id, amount_cents) VALUES (?,?,?)",
                        (tx_id, cats.get(_category_name(t.category), t.amount > 0), to_cents(t.amount)),
                    )
                if is_transfer:
                    inserted[tx_id] = (t, name)
                n += 1
            report.inserted[name] = n
            report.duplicates[name] = dup

        # Virements : chaque moitié pointe vers l'autre (montant opposé, même date, comptes croisés).
        waiting: dict[tuple, list[int]] = defaultdict(list)
        for tx_id, (t, name) in inserted.items():
            waiting[(name, transfer_account(t.category), t.date, to_cents(t.amount))].append(tx_id)
        for tx_id, (t, name) in inserted.items():
            if db.execute("SELECT transfer_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]:
                continue
            key = (transfer_account(t.category), name, t.date, -to_cents(t.amount))
            other = next((o for o in waiting.get(key, []) if o != tx_id), None)
            if other is None:
                report.transfers_unmatched += 1
                continue
            waiting[key].remove(other)
            db.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (other, tx_id))
            db.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (tx_id, other))
            report.transfers_linked += 1
        # Bénéficiaires mémorisés -> règles (après les tiennes, qui ont priorité).
        before = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        for m in q.memorized:
            name = _category_name(m.category)
            key = categorize.merchant_key(m.payee)
            if m.split or not name or transfer_account(m.category) or not key:
                continue
            categorize.add_rule(db, {"merchant": key}, cats.get(name, bool(m.amount and m.amount > 0)),
                                source="quicken", priority=200)
        report.rules_created = db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] - before
    report.proposed = categorize.apply(db)
    report.warnings += q.warnings
    return report
