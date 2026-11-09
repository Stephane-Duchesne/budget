"""Import d'un relevé dans la base : aperçu, dédoublonnage, validation, annulation."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from budget.models import ReadResult, Transaction
from budget.store import to_cents
from budget.text import clean_label, similarity

PROBABLE_DAYS = 3          # écart de dates toléré pour un doublon probable
PROBABLE_SIMILARITY = 0.5  # part de mots communs requise


@dataclass
class Candidate:
    tx: Transaction
    status: str                      # 'new', 'duplicate', 'probable'
    match_id: int | None = None      # transaction existante correspondante
    match_label: str = ""


@dataclass
class Preview:
    account_id: int
    file_name: str
    file_sha256: str
    result: ReadResult
    candidates: list[Candidate] = field(default_factory=list)
    already_imported: bool = False

    def count(self, status: str) -> int:
        return sum(c.status == status for c in self.candidates)


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _key(account_id: int, t: Transaction) -> tuple:
    return (account_id, t.date.isoformat(), to_cents(t.amount), clean_label(t.payee or t.memo))


def preview(db, account_id: int, result: ReadResult, file_name: str, file_sha256: str) -> Preview:
    """Classe chaque transaction du fichier : nouvelle, doublon certain ou doublon probable."""
    pv = Preview(account_id, file_name, file_sha256, result)
    pv.already_imported = db.execute(
        "SELECT 1 FROM imports WHERE account_id = ? AND file_sha256 = ?", (account_id, file_sha256)
    ).fetchone() is not None

    # Doublons exacts, comptés : deux cafés identiques le même jour dans le fichier restent
    # deux transactions, sauf s'ils sont déjà deux dans la base.
    existing = Counter()
    rows = {}
    if result.transactions:
        lo = min(t.date for t in result.transactions) - timedelta(days=PROBABLE_DAYS)
        hi = max(t.date for t in result.transactions) + timedelta(days=PROBABLE_DAYS)
        for r in db.execute(
            "SELECT id, date, posted_date, amount_cents, payee, memo, label_clean, fitid FROM transactions"
            " WHERE account_id = ? AND date BETWEEN ? AND ?", (account_id, lo.isoformat(), hi.isoformat())
        ):
            rows[r["id"]] = r
            existing[(account_id, r["date"], r["amount_cents"], r["label_clean"])] += 1
    fitids = {r["fitid"]: r["id"] for r in rows.values() if r["fitid"]}
    used: set[int] = set()

    for t in result.transactions:
        if t.fitid and t.fitid in fitids:
            pv.candidates.append(Candidate(t, "duplicate", fitids[t.fitid]))
            continue
        k = _key(account_id, t)
        if existing[k] > 0:
            existing[k] -= 1
            pv.candidates.append(Candidate(t, "duplicate"))
            continue
        # Doublon probable : même montant, date proche, libellé semblable (ex. PDF puis QIF du même mois).
        cents = to_cents(t.amount)
        match = None
        for r in rows.values():
            if r["id"] in used or r["amount_cents"] != cents:
                continue
            dates = [date.fromisoformat(r["date"])]
            if r["posted_date"]:
                dates.append(date.fromisoformat(r["posted_date"]))
            if t.posted_date:
                dates.append(t.posted_date)
            if min(abs((d - t.date).days) for d in dates) > PROBABLE_DAYS:
                continue
            if similarity(t.payee or t.memo, r["payee"] or r["memo"]) >= PROBABLE_SIMILARITY:
                match = r
                break
        if match is not None:
            used.add(match["id"])
            pv.candidates.append(Candidate(t, "probable", match["id"], f'{match["date"]} {match["payee"]}'))
        else:
            pv.candidates.append(Candidate(t, "new"))
    return pv


def commit(db, pv: Preview, keep_probable: set[int] | None = None) -> int:
    """Enregistre l'import. `keep_probable` : index des doublons probables à importer quand même."""
    keep_probable = keep_probable or set()
    r = pv.result
    with db:
        import_id = db.execute(
            "INSERT INTO imports (account_id, file_name, file_sha256, format, statement_date,"
            " period_start, period_end, closing_cents, reconciled) VALUES (?,?,?,?,?,?,?,?,?)",
            (pv.account_id, pv.file_name, pv.file_sha256, r.format,
             r.statement_date and r.statement_date.isoformat(),
             r.period_start and r.period_start.isoformat(),
             r.period_end and r.period_end.isoformat(),
             to_cents(r.closing_balance) if r.closing_balance is not None else None,
             int(r.reconciled) if r.checks else None),
        ).lastrowid
        for i, c in enumerate(pv.candidates):
            if c.status == "duplicate" or (c.status == "probable" and i not in keep_probable):
                continue
            t = c.tx
            db.execute(
                "INSERT INTO transactions (account_id, date, posted_date, amount_cents, payee, memo,"
                " label_clean, fitid, import_id, raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pv.account_id, t.date.isoformat(), t.posted_date and t.posted_date.isoformat(),
                 to_cents(t.amount), t.payee, t.memo, clean_label(t.payee or t.memo),
                 t.fitid, import_id, t.raw),
            )
    return import_id


def undo(db, import_id: int) -> int:
    """Annule un import d'un bloc. Renvoie le nombre de transactions retirées."""
    with db:
        n = db.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,)).rowcount
        db.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    return n
