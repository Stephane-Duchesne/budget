from datetime import date
from decimal import Decimal

from budget import quicken, store
from budget.readers import qif

EXPORT = """!Option:AutoSwitch
!Account
NChèques
TBank
^
NVisa
TCCard
^
NREER
TInvst
$25,000.00
^
!Clear:AutoSwitch
!Type:Cat
NAlimentation:Épicerie
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
D12/31'25
T1,000.00
POpening Balance
L[Chèques]
^
D1/15'26
T2,500.00
PEmployeur
LSalaire
^
D1/26'26
T-500.00
PPaiement Visa
L[Visa]
^
D1/30'26
T-150.00
PCostco
SAlimentation:Épicerie
$-100.00
SMaison:Meubles
$-50.00
^
D1/31'26
T-12.00
PInconnu
^
!Account
NVisa
TCCard
^
!Type:CCard
D1/3'26
T-42.10
PIGA
LAlimentation:Épicerie/Famille
^
D1/26'26
T500.00
PPaiement
L[Chèques]
^
"""


def migrated():
    db = store.connect(":memory:")
    q = qif.read_text(EXPORT)
    return db, quicken.migrate(db, q, "quicken.qif", "h")


def test_apercu():
    rows = {s.name: s for s in quicken.summarize(qif.read_text(EXPORT))}
    assert rows["Chèques"].transactions == 4 and rows["Chèques"].opening == Decimal("1000.00")
    assert rows["Chèques"].balance == Decimal("2838.00")
    assert rows["REER"].investment and rows["REER"].balance == Decimal("25000.00")


def test_reprise_complete():
    db, report = migrated()
    assert report.accounts_created == ["Chèques", "Visa", "REER"]
    assert report.inserted == {"Chèques": 4, "Visa": 2}
    assert report.transfers_linked == 1 and report.transfers_unmatched == 0
    assert any("REER" in w for w in report.warnings)

    bal = {r["name"]: (r["nature"], r["kind"], r["balance_cents"]) for r in store.accounts_with_balance(db)}
    assert bal == {
        "Chèques": ("asset", "imported", 283800),
        "Visa": ("liability", "imported", 45790),
        "REER": ("asset", "manual", 2500000),
    }
    # Arbre des catégories, classe Quicken ignorée
    tree = {(r["name"], r["parent"]) for r in db.execute(
        "SELECT c.name, p.name AS parent FROM categories c LEFT JOIN categories p ON p.id = c.parent_id")}
    assert {("Alimentation", None), ("Épicerie", "Alimentation"), ("Maison", None), ("Meubles", "Maison"),
            ("Salaire", None)} <= tree
    # Ventilations
    costco = db.execute("SELECT id, status FROM transactions WHERE payee = 'Costco'").fetchone()
    assert costco["status"] == "confirmed"
    assert db.execute("SELECT COUNT(*) FROM splits WHERE transaction_id = ?", (costco["id"],)).fetchone()[0] == 2
    # Sans catégorie -> à catégoriser
    assert db.execute("SELECT status FROM transactions WHERE payee = 'Inconnu'").fetchone()[0] == "to_categorize"
    # Virement lié dans les deux sens
    a, b = db.execute("SELECT id, transfer_id FROM transactions WHERE transfer_id IS NOT NULL ORDER BY id").fetchall()
    assert a["transfer_id"] == b["id"] and b["transfer_id"] == a["id"]
    # Solde d'ouverture et sa date
    row = db.execute("SELECT opening_cents, opening_date FROM accounts WHERE name = 'Chèques'").fetchone()
    assert (row["opening_cents"], row["opening_date"]) == (100000, "2026-01-15")


def test_reprise_deux_fois_sans_doublon():
    db, _ = migrated()
    report = quicken.migrate(db, qif.read_text(EXPORT), "quicken.qif", "h")
    assert report.accounts_created == [] and sum(report.inserted.values()) == 0
    assert report.duplicates == {"Chèques": 4, "Visa": 2}


PRET = """!Option:AutoSwitch
!Account
NChèques
TBank
^
NPrêt fenêtres
T
^
!Clear:AutoSwitch
!Account
NChèques
TBank
^
!Type:Bank
D01/01/2025
T1000.00
POpening Balance
L[Chèques]
^
D02/01/2025
T-250.00
PRemboursement
L[Prêt fenêtres]
^
!Account
NPrêt fenêtres
T
^
!Type:
D01/01/2025
T-5000.00
POpening Balance
L[Prêt fenêtres]
^
D02/01/2025
T250.00
PRemboursement
L[Chèques]
^
"""


def test_pret_exporte_sans_type():
    q = qif.read_text(PRET)
    assert any("Prêt fenêtres" in w and "sans type" in w for w in q.warnings)
    assert not any("non prise en charge" in w for w in q.warnings)
    db = store.connect(":memory:")
    quicken.migrate(db, q, "quicken.qif", "h")
    bal = {r["name"]: (r["nature"], r["balance_cents"]) for r in store.accounts_with_balance(db)}
    assert bal["Prêt fenêtres"] == ("liability", -475000)
    assert bal["Chèques"] == ("asset", 75000)
    linked = db.execute("SELECT COUNT(*) FROM transactions WHERE transfer_id IS NOT NULL").fetchone()[0]
    assert linked == 2
