from datetime import date
from decimal import Decimal

from budget.readers import qfx


def test_compte_cheques(fixtures):
    [r] = qfx.read(fixtures / "checking.qfx")
    assert r.account_id == "12345-EOP"
    assert (r.period_start, r.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert r.closing_balance == Decimal("1415.50")
    assert [(t.date, t.amount, t.payee, t.fitid) for t in r.transactions] == [
        (date(2026, 1, 5), Decimal("-84.50"), "HYDRO-QUEBEC", "202601050001"),
        (date(2026, 1, 15), Decimal("2500.00"), "DEPOT PAIE", "202601150001"),
        (date(2026, 1, 26), Decimal("-7000.00"), "PAIEMENT VISA DESJARDINS", "202601260001"),
    ]
    assert r.transactions[0].memo == "Paiement de facture"


def test_detection(fixtures):
    assert qfx.sniff(fixtures / "checking.qfx")
