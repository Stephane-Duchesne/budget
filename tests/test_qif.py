from datetime import date
from decimal import Decimal

from budget.readers import qif

QUICKEN_EXPORT = """!Option:AutoSwitch
!Account
NChèques
TBank
^
NVisa
TCCard
^
!Clear:AutoSwitch
!Type:Cat
NAlimentation:Épicerie
DÉpicerie
E
^
NSalaire
I
^
!Option:AutoSwitch
!Account
NChèques
TBank
^
!Type:Bank
D1/15'26
T2,500.00
PEmployeur
LSalaire
^
D1/26'26
T-7,000.00
PPaiement Visa
L[Visa]
^
D1/30'26
T-150.00
PCostco
LAlimentation:Épicerie
SAlimentation:Épicerie
EÉpicerie
$-100.00
SMaison
$-50.00
^
!Account
NVisa
TCCard
^
!Type:CCard
D1/3'26
T-42.10
PIGA
LAlimentation:Épicerie
^
!Type:Memorized
KC
PIGA
^
"""


def test_export_quicken_complet():
    r = qif.read_text(QUICKEN_EXPORT)
    assert [a.name for a in r.accounts] == ["Chèques", "Visa"]
    assert [(c.name, c.is_income) for c in r.categories] == [
        ("Alimentation:Épicerie", False), ("Salaire", True)]
    assert [(t.account, t.date, t.amount) for t in r.transactions] == [
        ("Chèques", date(2026, 1, 15), Decimal("2500.00")),
        ("Chèques", date(2026, 1, 26), Decimal("-7000.00")),
        ("Chèques", date(2026, 1, 30), Decimal("-150.00")),
        ("Visa", date(2026, 1, 3), Decimal("-42.10")),
    ]
    assert qif.transfer_account(r.transactions[1].category) == "Visa"
    costco = r.transactions[2]
    assert [(s.category, s.amount, s.memo) for s in costco.splits] == [
        ("Alimentation:Épicerie", Decimal("-100.00"), "Épicerie"),
        ("Maison", Decimal("-50.00"), ""),
    ]
    assert [m.payee for m in r.memorized] == ["IGA"] and not any("Memorized" in w for w in r.warnings)


def test_ordre_des_dates():
    assert qif.detect_date_order(["12-24-2025", "01-02-2026"]) == ("MDY", True)
    assert qif.detect_date_order(["24/12/2025"]) == ("DMY", True)
    assert qif.detect_date_order(["01/02/2026"]) == ("MDY", False)
    assert qif.detect_date_order(["2026-01-02"]) == ("YMD", True)
    assert qif.parse_date("2026-01-02", "YMD") == date(2026, 1, 2)
    assert qif.parse_date("24/12'25", "DMY") == date(2025, 12, 24)


def test_encodage_windows_1252():
    data = "!Type:CCard\nD12-24-2025\nT61.42\nPPAIEMENT AUTORISÉ\n^\n".encode("cp1252")
    r = qif.read_text(qif.decode(data))
    assert r.transactions[0].payee == "PAIEMENT AUTORISÉ"


def test_montants():
    assert qif.parse_amount("1,234.56") == Decimal("1234.56")
    assert qif.parse_amount("1 234,56") == Decimal("1234.56")
    assert qif.parse_amount("-7000.00") == Decimal("-7000.00")



def test_section_ignoree_nomme_le_compte():
    from budget.readers.qif import read_text
    r = read_text("!Account\nNREER\nTInvst\n^\n!Type:Invst\nD01/15/2025\nNBuy\nT100.00\n^\n"
                  "!Type:Prices\n\"XYZ\",1.0,\"1/1/25\"\n^\n")
    assert "Section non prise en charge, ignorée : !Type:Invst (1 enregistrement(s), dont 1 avec date et montant, compte(s) : REER)" in r.warnings
    assert "Section non prise en charge, ignorée : !Type:Prices (1 enregistrement(s))" in r.warnings
