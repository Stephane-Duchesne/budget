"""Lecteur QFX / OFX (compte chèques Desjardins, exports Quicken).

Lit l'OFX 1.x (SGML) et 2.x (XML) avec ofxtools. Chaque transaction porte
l'identifiant unique de la banque (FITID), utilisé en priorité pour le
dédoublonnage ; le solde du relevé (LEDGERBAL) sert au rapprochement.
"""
from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ofxtools.Parser import OFXTree

from budget.models import ReadResult, Transaction

FORMAT = "qfx"


def _local_date(value):
    """Les dates OFX sont en UTC dans ofxtools ; on garde la date civile du relevé."""
    if isinstance(value, datetime):
        return value.date()
    return value


def read_bytes(data: bytes) -> list[ReadResult]:
    """Un fichier QFX peut contenir plusieurs comptes : un résultat par relevé."""
    tree = OFXTree()
    tree.parse(io.BytesIO(data))
    ofx = tree.convert()

    results: list[ReadResult] = []
    for stmt in ofx.statements:
        result = ReadResult(format=FORMAT)
        acct = stmt.account
        result.account_id = getattr(acct, "acctid", None)
        tranlist = stmt.banktranlist
        if tranlist is not None:
            result.period_start = _local_date(tranlist.dtstart)
            result.period_end = _local_date(tranlist.dtend)
            for tx in tranlist:
                payee = (tx.name or "") or (tx.payee.name if getattr(tx, "payee", None) else "")
                result.transactions.append(Transaction(
                    date=_local_date(tx.dtposted),
                    amount=Decimal(tx.trnamt),
                    payee=payee.strip(),
                    memo=(tx.memo or "").strip(),
                    fitid=tx.fitid,
                    raw=f"{tx.trntype} {tx.fitid}",
                ))
        if stmt.ledgerbal is not None:
            result.closing_balance = Decimal(stmt.ledgerbal.balamt)
            result.statement_date = _local_date(stmt.ledgerbal.dtasof)
        results.append(result)
    return results


def read(path: str | Path) -> list[ReadResult]:
    return read_bytes(Path(path).read_bytes())


def sniff(path: str | Path) -> bool:
    if Path(path).suffix.lower() in (".qfx", ".ofx"):
        return True
    head = Path(path).read_bytes()[:400].upper()
    return b"OFXHEADER" in head or b"<OFX>" in head
