from datetime import date
from decimal import Decimal

from budget import importer, store
from budget.models import ReadResult, Transaction


def tx(d, amount, payee, **kw):
    return Transaction(date=date.fromisoformat(d), amount=Decimal(amount), payee=payee, **kw)


def setup():
    db = store.connect(":memory:")
    return db, store.create_account(db, "Visa", nature="liability")


def test_nouvelles_puis_doublons_exacts():
    db, acct = setup()
    r = ReadResult("qif", [tx("2026-01-03", "-42.10", "IGA EXTRA #8021"), tx("2026-01-04", "-5.00", "CAFE")])
    pv = importer.preview(db, acct, r, "a.qif", "h1")
    assert pv.count("new") == 2
    importer.commit(db, pv)
    pv2 = importer.preview(db, acct, r, "a.qif", "h1")
    assert pv2.already_imported and pv2.count("duplicate") == 2


def test_achats_identiques_le_meme_jour_conserves():
    db, acct = setup()
    r = ReadResult("qif", [tx("2026-01-03", "-3.00", "CAFE"), tx("2026-01-03", "-3.00", "CAFE")])
    importer.commit(db, importer.preview(db, acct, r, "a", "h"))
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    # Un 3e café identique dans un autre fichier : les deux premiers sont des doublons,
    # le 3e est signalé comme doublon probable et l'utilisateur tranche.
    r2 = ReadResult("qif", r.transactions + [tx("2026-01-03", "-3.00", "CAFE")])
    pv = importer.preview(db, acct, r2, "b", "h2")
    assert (pv.count("duplicate"), pv.count("probable"), pv.count("new")) == (2, 1, 0)


def test_doublon_probable_entre_pdf_et_qif():
    db, acct = setup()
    pdf = ReadResult("visa", [tx("2025-12-09", "-73.71", "DEPANNEUR MOISE-VINCENT", posted_date=date(2025, 12, 10))])
    importer.commit(db, importer.preview(db, acct, pdf, "a.pdf", "h"))
    qif = ReadResult("qif", [tx("2025-12-10", "-73.71", "DEPANNEUR MOISE-VINCENT S")])
    pv = importer.preview(db, acct, qif, "a.qif", "h2")
    assert pv.count("probable") == 1
    importer.commit(db, pv)                 # non coché : pas importé
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_fitid_prioritaire():
    db, acct = setup()
    r = ReadResult("qfx", [tx("2026-01-05", "-84.50", "HYDRO", fitid="X1")])
    importer.commit(db, importer.preview(db, acct, r, "a", "h"))
    r2 = ReadResult("qfx", [tx("2026-01-06", "-84.50", "HYDRO-QUEBEC PAIEMENT", fitid="X1")])
    assert importer.preview(db, acct, r2, "b", "h2").count("duplicate") == 1


def test_annuler_un_import():
    db, acct = setup()
    r = ReadResult("qif", [tx("2026-01-03", "-42.10", "IGA")])
    import_id = importer.commit(db, importer.preview(db, acct, r, "a", "h"))
    assert importer.undo(db, import_id) == 1
    assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_solde_avec_solde_ouverture():
    db = store.connect(":memory:")
    acct = store.create_account(db, "Visa", nature="liability", opening=Decimal("-61.42"))
    r = ReadResult("qif", [tx("2026-01-03", "-100.00", "IGA"), tx("2026-01-04", "61.42", "PAIEMENT")])
    importer.commit(db, importer.preview(db, acct, r, "a", "h"))
    [row] = store.accounts_with_balance(db)
    assert row["balance_cents"] == -10000
