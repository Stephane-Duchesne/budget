from datetime import date
from decimal import Decimal

from budget.readers import qif, visa_desjardins_pdf as visa

from conftest import private_file

# Différences voulues avec StatementToQif.ps1 : le script coupait le marchand à
# 25 caractères de texte et y laissait la première lettre de la ville.
SCRIPT_PAYEE_FIXES = {
    "DEPANNEUR MOISE-VINCENT S": "DEPANNEUR MOISE-VINCENT",
    "BEN & FLORENTINE - 2781 C": "BEN & FLORENTINE - 2781",
    "ULTRAMAR SAINTE-EULALIE S": "ULTRAMAR SAINTE-EULALIE",
}


def test_janvier_2026_identique_au_script():
    result = visa.read(private_file("visa-2026-01.pdf"))
    expected = qif.read(private_file("visa-2026-01-script.qif")).transactions

    assert result.statement_date == date(2026, 1, 6)
    assert result.warnings == []
    assert result.reconciled
    assert [(c.label, c.expected) for c in result.checks] == [
        ("Achats / débits", Decimal("9332.20")),
        ("Paiements / crédits", Decimal("7185.59")),
    ]
    assert len(result.transactions) == len(expected) == 122
    for got, want in zip(result.transactions, expected):
        assert (got.date, got.amount, got.memo) == (want.date, want.amount, want.memo)
        assert got.payee == SCRIPT_PAYEE_FIXES.get(want.payee, want.payee)


def test_janvier_2026_details():
    txs = visa.read(private_file("visa-2026-01.pdf")).transactions
    by_payee = {t.payee: t for t in txs}
    # Crédit marqué CR -> montant positif
    assert by_payee["PAIEMENT CAISSE"].amount == Decimal("7000.00")
    # Achat de décembre sur un relevé de janvier -> année précédente
    assert txs[0].date == date(2025, 12, 3) and txs[0].posted_date == date(2025, 12, 4)
    # Accents conservés, nom collé à la ville coupé à la largeur du champ
    assert "PAIEMENT AUTORISÉ - PRÉLÈVEMENT EFFECTUÉ" in by_payee
    assert "WAL-MART SUPERCENTER#3140" in by_payee
    assert "RESTAURANT LA COCHONNE RI" in by_payee
    # Ville décalée d'un cran : pas dans le marchand
    assert "TORBOX" in by_payee


def test_lignes_synthetiques():
    lines = [visa.Line(t, []) for t in [
        "DATE DU RELEVÉ   Jour 05 Mois 02 Année 2025",
        "Achats / débits                +      30,00",
        "Paiements / crédits            -      10,00",
        "  28  01   29  01   CAFE DU COIN             MONTREAL     QC   1,00 %   30,00",
        "  30  01   30  01   PAIEMENT CAISSE                                     10,00CR",
        "  31  01   31  01   LIGNE MAL FORMEE",
    ]]
    r = visa.parse_lines(lines)
    assert [(t.date, t.amount) for t in r.transactions] == [
        (date(2025, 1, 28), Decimal("-30.00")),
        (date(2025, 1, 30), Decimal("10.00")),
    ]
    assert r.reconciled
    assert len(r.warnings) == 1 and "LIGNE MAL FORMEE" in r.warnings[0]


def test_totaux_qui_ne_concordent_pas():
    lines = [visa.Line(t, []) for t in [
        "DATE DU RELEVÉ   Jour 05 Mois 02 Année 2025",
        "Achats / débits                +      99,00",
        "Paiements / crédits            -       0,00",
        "  28  01   29  01   CAFE DU COIN             MONTREAL     QC   1,00 %   30,00",
    ]]
    r = visa.parse_lines(lines)
    assert not r.reconciled
    assert r.checks[0].computed == Decimal("30.00") and r.checks[0].expected == Decimal("99.00")
