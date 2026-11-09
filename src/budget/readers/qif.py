"""Lecteur QIF : relevés Desjardins, sorties du script StatementToQif.ps1 et exports complets de Quicken.

Champs de transaction reconnus : D (date), T/U (montant), P (bénéficiaire),
M (mémo), L (catégorie ou [compte] pour un virement), N (numéro), C (pointage),
S/E/$ (ventilations). Les blocs !Account et !Type:Cat d'un export Quicken
complet sont lus aussi ; les sections non bancaires (placements, listes de
classes, bénéficiaires mémorisés) sont ignorées avec un avertissement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from budget.models import ReadResult, Split, Transaction

FORMAT = "qif"

# Types de section qui contiennent des transactions de compte.
TRANSACTION_TYPES = {"bank", "cash", "ccard", "oth a", "oth l"}

DATE_RE = re.compile(r"^\s*(\d{1,4})\s*[/\-.]\s*(\d{1,2})\s*(?:[/\-.']\s*)?(\d{2,4})\s*$")


@dataclass
class QifAccount:
    name: str
    type: str = ""
    description: str = ""
    balance: Decimal | None = None    # champ $ de la liste des comptes, quand Quicken l'exporte


@dataclass
class QifCategory:
    name: str
    description: str = ""
    is_income: bool = False


@dataclass
class QifFile(ReadResult):
    """Résultat de lecture enrichi des listes d'un export Quicken complet."""
    accounts: list[QifAccount] = field(default_factory=list)
    categories: list[QifCategory] = field(default_factory=list)
    date_order: str = "MDY"
    date_order_certain: bool = True


def decode(data: bytes) -> str:
    """UTF-8 si le fichier en est, sinon Windows-1252 (ce que Quicken et le script produisent)."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_amount(text: str) -> Decimal:
    t = text.strip().replace(" ", "").replace(" ", "").replace("$", "")
    # « 1,234.56 » (Quicken) ou « 1 234,56 » (français)
    if "," in t and "." in t:
        t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return Decimal(t)
    except InvalidOperation as exc:
        raise ValueError(f"Montant illisible : {text!r}") from exc


def _year(y: str) -> int:
    n = int(y)
    if len(y) == 4:
        return n
    return 2000 + n if n < 70 else 1900 + n   # Quicken écrit 1/5'26 pour 2026


def detect_date_order(raw_dates: list[str]) -> tuple[str, bool]:
    """Devine l'ordre jour/mois. Renvoie (ordre, certitude).

    « YMD » si l'année vient en premier ; sinon « DMY » dès qu'un premier nombre
    dépasse 12, « MDY » dès qu'un second nombre dépasse 12. Si rien ne tranche,
    « MDY » (format Quicken par défaut) sans certitude : l'interface fera confirmer.
    """
    first_gt12 = second_gt12 = False
    for raw in raw_dates:
        m = DATE_RE.match(raw)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if len(a) == 4:
            return "YMD", True
        first_gt12 |= int(a) > 12
        second_gt12 |= int(b) > 12
    if first_gt12 and not second_gt12:
        return "DMY", True
    if second_gt12 and not first_gt12:
        return "MDY", True
    return "MDY", False


def parse_date(raw: str, order: str) -> date:
    m = DATE_RE.match(raw)
    if not m:
        raise ValueError(f"Date illisible : {raw!r}")
    a, b, c = m.groups()
    if order == "YMD":
        return date(int(a), int(b), int(c))
    if order == "DMY":
        return date(_year(c), int(b), int(a))
    return date(_year(c), int(a), int(b))


def _records(lines: list[str]):
    """Découpe le fichier en (en-tête de section, enregistrement)."""
    header, record = "", []
    for line in lines:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("!"):
            if record:
                yield header, record
                record = []
            if not line.lower().startswith("!option") and not line.lower().startswith("!clear"):
                header = line.strip()
            continue
        if line.strip() == "^":
            yield header, record
            record = []
            continue
        record.append(line)
    if record:
        yield header, record


def read_text(text: str, date_order: str | None = None) -> QifFile:
    lines = text.splitlines()
    raw_dates = [ln[1:] for ln in lines if ln.startswith("D")]
    order, certain = (date_order, True) if date_order else detect_date_order(raw_dates)
    result = QifFile(format=FORMAT, date_order=order, date_order_certain=certain)
    if not certain:
        result.warnings.append(
            "Ordre jour/mois impossible à déterminer (aucun jour > 12) : mois/jour supposé, à confirmer."
        )

    current_account: str | None = None
    skipped: set[str] = set()
    for header, rec in _records(lines):
        kind = header.lower()
        if kind == "!account":
            fields = {ln[0]: ln[1:].strip() for ln in rec}
            if "N" in fields:
                current_account = fields["N"]
                if not any(a.name == current_account for a in result.accounts):
                    balance = None
                    if fields.get("$"):
                        try:
                            balance = parse_amount(fields["$"])
                        except ValueError:
                            pass
                    result.accounts.append(QifAccount(fields["N"], fields.get("T", ""), fields.get("D", ""), balance))
            continue
        if kind == "!type:cat":
            fields = {ln[0]: ln[1:].strip() for ln in rec}
            if "N" in fields:
                result.categories.append(QifCategory(fields["N"], fields.get("D", ""), "I" in fields))
            continue
        if not kind.startswith("!type:") or kind[6:] not in TRANSACTION_TYPES:
            skipped.add(header or "(sans en-tête)")
            continue

        values: dict[str, str] = {}
        splits: list[Split] = []
        split_cat = split_memo = None
        for ln in rec:
            code, value = ln[0], ln[1:]
            if code == "S":
                split_cat, split_memo = value.strip(), ""
            elif code == "E" and split_cat is not None:
                split_memo = value.strip()
            elif code == "$" and split_cat is not None:
                splits.append(Split(split_cat, parse_amount(value), split_memo or ""))
                split_cat = None
            else:
                values.setdefault(code, value)
        try:
            when = parse_date(values["D"], order)
            amount = parse_amount(values.get("T") or values.get("U") or "0")
        except (KeyError, ValueError) as exc:
            result.warnings.append(f"Enregistrement ignoré ({exc}) : {' | '.join(rec)}")
            continue
        result.transactions.append(Transaction(
            date=when,
            amount=amount,
            payee=values.get("P", "").strip(),
            memo=values.get("M", "").strip(),
            category=values.get("L", "").strip() or None,
            splits=tuple(splits),
            account=current_account,
            raw=" | ".join(rec),
        ))
    for h in sorted(skipped):
        result.warnings.append(f"Section non prise en charge, ignorée : {h}")
    return result


def read(path: str | Path, date_order: str | None = None) -> QifFile:
    return read_text(decode(Path(path).read_bytes()), date_order=date_order)


def sniff(path: str | Path) -> bool:
    if Path(path).suffix.lower() == ".qif":
        return True
    head = Path(path).read_bytes()[:200].lstrip(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b"!Type:") or head.startswith(b"!Account") or head.startswith(b"!Option")


def transfer_account(category: str | None) -> str | None:
    """« [Épargne] » -> « Épargne » : dans Quicken, une catégorie entre crochets est un virement."""
    if category and category.startswith("[") and "]" in category:
        return category[1:category.index("]")]
    return None
