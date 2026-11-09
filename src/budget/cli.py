"""Ligne de commande provisoire, en attendant l'interface.

    python -m budget.cli lire releve.pdf            # affiche les transactions et le contrôle des totaux
    python -m budget.cli qif releve.pdf [...]       # écrit releve.qif à côté, comme StatementToQif.ps1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from budget.models import ReadResult
from budget.readers import qfx, qif, visa_desjardins_pdf


def read_any(path: Path) -> list[ReadResult]:
    if visa_desjardins_pdf.sniff(path):
        return [visa_desjardins_pdf.read(path)]
    if qfx.sniff(path):
        return qfx.read(path)
    if qif.sniff(path):
        return [qif.read(path)]
    raise SystemExit(f"{path} : format non reconnu")


def write_qif(result: ReadResult, target: Path, encoding: str = "cp1252") -> None:
    lines = ["!Type:CCard"]
    for t in result.transactions:
        lines += [f"D{t.date:%m-%d-%Y}", f"T{t.amount:.2f}", f"P{t.payee}", f"M{t.memo}", "^"]
    target.write_text("\r\n".join(lines) + "\r\n", encoding=encoding, errors="strict")


def money(v) -> str:
    """1234.5 -> « 1 234,50 »"""
    return f"{v:,.2f}".replace(",", "\u00a0").replace(".", ",")


def report(path: Path, result: ReadResult) -> bool:
    print(f"{path.name} : {len(result.transactions)} transactions ({result.format})")
    for w in result.warnings:
        print(f"  ATTENTION {w}")
    for c in result.checks:
        if c.expected is None:
            print(f"  {c.label:<20} {money(c.computed):>12}   ligne du sommaire introuvable")
        elif c.ok:
            print(f"  {c.label:<20} {money(c.computed):>12}   OK")
        else:
            print(f"  {c.label:<20} {money(c.computed):>12}   ÉCART : le relevé indique {money(c.expected)}")
    if result.closing_balance is not None:
        print(f"  Solde du relevé      {money(result.closing_balance):>12}")
    return all(c.ok for c in result.checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="budget")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_read = sub.add_parser("lire", help="affiche les transactions d'un relevé")
    p_read.add_argument("files", nargs="+", type=Path)
    p_qif = sub.add_parser("qif", help="convertit un relevé PDF Visa en QIF")
    p_qif.add_argument("files", nargs="+", type=Path)
    p_qif.add_argument("--utf8", action="store_true", help="écrire en UTF-8 plutôt qu'en Windows-1252")
    args = parser.parse_args(argv)

    all_ok = True
    for path in args.files:
        for result in read_any(path):
            all_ok &= report(path, result)
            if args.cmd == "lire":
                for t in result.transactions:
                    print(f"  {t.date}  {money(t.amount):>10}  {t.payee:<28} {t.memo}")
            elif args.cmd == "qif":
                target = path.with_suffix(".qif")
                write_qif(result, target, "utf-8" if args.utf8 else "cp1252")
                print(f"  -> {target}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
