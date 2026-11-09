from datetime import date
from decimal import Decimal

from budget import categorize, importer, quicken, store
from budget.models import ReadResult, Transaction
from budget.readers import qif

EXPORT = """!Account
NVisa
TCCard
^
!Type:CCard
D6/ 5'25
T-97.14
PIGA EXTRA #8021          CHAMBLY      QC
LDÉPENSES VARIABLES:Epicerie
^
D6/ 9'25
T-29.39
PJEAN COUTU 0321          CHAMBLY      QC
LDÉPENSES VARIABLES:Pharmacie
^
D6/12'25
T-12.00
PCAFE INCONNU             CHAMBLY      QC
^
!Type:Memorized
KC
PMicrosoft*Microsoft 365 F425-7038358  ON
LDÉPENSES FIXES:Office365
T-166.71
^
KC
PPaiement carte
L[Chèques]
^
"""


def migrated():
    db = store.connect(":memory:")
    report = quicken.migrate(db, qif.read_text(EXPORT), "q.qif", "h")
    return db, report


def cat_of(db, tx_id):
    paths = categorize.category_paths(db)
    row = db.execute("SELECT category_id FROM splits WHERE transaction_id = ?", (tx_id,)).fetchone()
    return paths.get(row["category_id"]) if row else None


def test_memorises_quicken_deviennent_regles():
    db, report = migrated()
    assert report.rules_created == 1          # le virement mémorisé est ignoré
    cz = categorize.Categorizer(db)
    s = cz.suggest("Microsoft*Microsoft 365 F425-7038358  ON", "", -16671)
    assert s.source == "quicken"
    assert categorize.category_paths(db)[s.category_id] == "DÉPENSES FIXES : Office365"


def test_import_propose_marchand_connu_et_regle():
    db, _ = migrated()
    acct = db.execute("SELECT id FROM accounts WHERE name = 'Visa'").fetchone()["id"]
    pdf = ReadResult("visa-pdf", [
        Transaction(date(2026, 1, 3), Decimal("-55.10"), "IGA EXTRA #8021"),      # marchand connu
        Transaction(date(2026, 1, 4), Decimal("-166.71"), "Microsoft*Microsoft 365"),  # règle Quicken
        Transaction(date(2026, 1, 5), Decimal("-9.99"), "NOUVEAU COMMERCE"),       # rien
    ])
    importer.commit(db, importer.preview(db, acct, pdf, "v.pdf", "x"))
    rows = {r["payee"]: r for r in db.execute("SELECT * FROM transactions WHERE import_id IS NOT NULL"
                                              " AND date >= '2026-01-01'")}
    assert rows["IGA EXTRA #8021"]["status"] == "proposed"
    assert rows["IGA EXTRA #8021"]["category_source"] == "merchant"
    assert cat_of(db, rows["IGA EXTRA #8021"]["id"]) == "DÉPENSES VARIABLES : Epicerie"
    assert rows["Microsoft*Microsoft 365"]["category_source"] == "quicken"
    assert rows["NOUVEAU COMMERCE"]["status"] == "to_categorize"


def test_correction_cree_regle_prioritaire():
    db, _ = migrated()
    tx = db.execute("SELECT id FROM transactions WHERE payee LIKE 'CAFE%'").fetchone()["id"]
    pharm = db.execute("SELECT id FROM categories WHERE name = 'Pharmacie'").fetchone()["id"]
    categorize.set_category(db, tx, pharm)
    with db:
        categorize.add_rule(db, {"merchant": categorize.merchant_key("CAFE INCONNU")}, pharm)
    s = categorize.Categorizer(db).suggest("CAFE INCONNU", "", -500)
    assert s.source == "rule" and s.category_id == pharm


def test_statistique_prudente():
    db = store.connect(":memory:")
    acct = store.create_account(db, "Visa")
    categorize.create_starter_categories(db)
    epic = db.execute("SELECT id FROM categories WHERE name = 'Épicerie'").fetchone()["id"]
    resto = db.execute("SELECT id FROM categories WHERE name = 'Restaurant'").fetchone()["id"]
    with db:
        for i in range(15):
            for payee, cat in ((f"MARCHE FRAIS {chr(65 + i)}", epic), (f"RESTO BISTRO {chr(65 + i)}", resto)):
                tid = db.execute("INSERT INTO transactions (account_id, date, amount_cents, payee, status)"
                                 " VALUES (?, '2025-01-01', -1000, ?, 'confirmed')", (acct, payee)).lastrowid
                db.execute("INSERT INTO splits (transaction_id, category_id, amount_cents) VALUES (?,?,-1000)",
                           (tid, cat))
    cz = categorize.Categorizer(db)
    s = cz.suggest("MARCHE FRAIS DU COIN", "", -2000)
    assert s.source == "stat" and s.category_id == epic
    assert cz.suggest("QUINCAILLERIE", "", -2000) is None
