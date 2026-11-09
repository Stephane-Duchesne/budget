from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from budget import categorize, importer, quicken, store
from budget.models import ReadResult
from budget.readers import qfx, qif, visa_desjardins_pdf

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


def money(value, cents: bool = False) -> str:
    """Montant au format canadien français : « -1 234,56 $ »."""
    v = store.from_cents(value) if cents else Decimal(value)
    s = f"{abs(v):,.2f}".replace(",", " ").replace(".", ",")
    return f"{'-' if v < 0 else ''}{s} $"


TEMPLATES.env.filters["money"] = money
TEMPLATES.env.filters["cents"] = lambda v, cur="CAD": money(v, cents=True) + ("" if cur == "CAD" else f"\u00a0{cur[:2]}")

NATURES = {"asset": "Actif", "liability": "Passif (carte de crédit, prêt)"}
KINDS = {"imported": "Importé (relevés)", "manual": "Manuel (sans relevé)"}
CURRENCIES = {"CAD": "Dollar canadien", "USD": "Dollar américain"}
STATUS = {"new": "Nouvelle", "duplicate": "Déjà présente", "probable": "Doublon probable"}


def default_db_path() -> Path:
    if os.environ.get("BUDGET_DB"):
        return Path(os.environ["BUDGET_DB"])
    folder = Path.home() / "Budget"
    folder.mkdir(exist_ok=True)
    return folder / "budget.sqlite"


def read_file(path: Path) -> list[ReadResult]:
    if visa_desjardins_pdf.sniff(path):
        return [visa_desjardins_pdf.read(path)]
    if qfx.sniff(path):
        return qfx.read(path)
    if qif.sniff(path):
        return [qif.read(path)]
    raise ValueError("Format non reconnu : PDF Visa Desjardins, QIF ou QFX attendus.")


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Budget", docs_url=None, redoc_url=None)
    db = store.connect(db_path or default_db_path())
    pending: dict[str, importer.Preview] = {}

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("accounts", store.accounts_with_balance(db))
        return TEMPLATES.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        accounts = store.accounts_with_balance(db)
        assets = sum(a["balance_cents"] for a in accounts if a["nature"] == "asset" and a["currency"] == "CAD")
        liabilities = sum(a["balance_cents"] for a in accounts if a["nature"] == "liability" and a["currency"] == "CAD")
        foreign: dict[str, int] = {}
        for a in accounts:
            if a["currency"] != "CAD":
                foreign[a["currency"]] = foreign.get(a["currency"], 0) + a["balance_cents"]
        imports = db.execute(
            "SELECT i.*, a.name AS account, COUNT(t.id) AS n FROM imports i JOIN accounts a ON a.id = i.account_id"
            " LEFT JOIN transactions t ON t.import_id = i.id GROUP BY i.id ORDER BY i.id DESC LIMIT 8"
        ).fetchall()
        return page(request, "dashboard.html", accounts=accounts, net=assets + liabilities, foreign=foreign,
                    imports=imports)

    @app.get("/comptes/nouveau", response_class=HTMLResponse)
    def new_account(request: Request):
        return page(request, "account_form.html", natures=NATURES, kinds=KINDS, error=None, form={})

    @app.post("/comptes/nouveau")
    def create_account(request: Request, name: str = Form(...), kind: str = Form("imported"),
                       nature: str = Form("asset"), opening: str = Form("0"), opening_date: str = Form("")):
        form = dict(name=name, kind=kind, nature=nature, opening=opening, opening_date=opening_date)
        try:
            amount = Decimal(opening.replace(" ", "").replace(" ", "").replace(",", ".") or "0")
            store.create_account(db, name.strip(), kind, nature, amount, opening_date or None)
        except (InvalidOperation, ValueError):
            return page(request, "account_form.html", natures=NATURES, kinds=KINDS, form=form,
                        error="Solde d'ouverture invalide.")
        except Exception:
            return page(request, "account_form.html", natures=NATURES, kinds=KINDS, form=form,
                        error="Un compte porte déjà ce nom.")
        return RedirectResponse("/", status_code=303)

    @app.get("/comptes/{account_id}", response_class=HTMLResponse)
    def edit_account(request: Request, account_id: int):
        acct = next((a for a in store.accounts_with_balance(db) if a["id"] == account_id), None)
        if acct is None:
            return RedirectResponse("/", status_code=303)
        return page(request, "account_edit.html", acct=acct, natures=NATURES, currencies=CURRENCIES, error=None)

    @app.post("/comptes/{account_id}")
    def save_account(request: Request, account_id: int, name: str = Form(...), nature: str = Form("asset"),
                     currency: str = Form("CAD"), value: str = Form(""), value_date: str = Form("")):
        acct = next((a for a in store.accounts_with_balance(db) if a["id"] == account_id), None)
        if acct is None:
            return RedirectResponse("/", status_code=303)
        def fail(msg):
            return page(request, "account_edit.html", acct=acct, natures=NATURES, currencies=CURRENCIES, error=msg)
        if nature not in NATURES or currency not in CURRENCIES:
            return fail("Valeur invalide.")
        amount = None
        if value.strip():
            try:
                amount = Decimal(value.replace(" ", "").replace("\u00a0", "").replace("$", "").replace(",", ".") )
            except InvalidOperation:
                return fail("Valeur invalide.")
            if not value_date:
                return fail("Indiquez la date de la valeur.")
        try:
            store.update_account(db, account_id, name.strip(), nature, currency, amount, value_date or None)
        except Exception:
            return fail("Un compte porte déjà ce nom.")
        return RedirectResponse("/", status_code=303)

    @app.get("/importer", response_class=HTMLResponse)
    def import_form(request: Request, compte: int | None = None):
        return page(request, "import_form.html", selected=compte, error=None)

    @app.post("/importer", response_class=HTMLResponse)
    async def import_preview(request: Request, account_id: int = Form(...), file: UploadFile = File(...)):
        suffix = Path(file.filename or "releve").suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            path = Path(tmp.name)
        try:
            results = read_file(path)
            digest = importer.sha256(path)
        except Exception as exc:  # fichier illisible : on l'explique au lieu d'échouer
            return page(request, "import_form.html", selected=account_id, error=f"Lecture impossible : {exc}")
        finally:
            path.unlink(missing_ok=True)
        if len(results) != 1:
            return page(request, "import_form.html", selected=account_id,
                        error=f"Ce fichier contient {len(results)} comptes ; l'import multi-comptes arrive bientôt.")
        pv = importer.preview(db, account_id, results[0], file.filename or "relevé", digest)
        token = secrets.token_urlsafe(16)
        pending[token] = pv
        account = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return page(request, "import_preview.html", pv=pv, token=token, account=account, status=STATUS)

    @app.post("/importer/confirmer")
    async def import_commit(request: Request):
        form = await request.form()
        pv = pending.pop(str(form.get("token")), None)
        if pv is None:
            return RedirectResponse("/importer", status_code=303)
        keep = {int(k.split("_", 1)[1]) for k in form.keys() if k.startswith("keep_")}
        import_id = importer.commit(db, pv, keep)
        return RedirectResponse(f"/transactions?import_id={import_id}", status_code=303)

    @app.post("/imports/{import_id}/annuler")
    def import_undo(import_id: int):
        importer.undo(db, import_id)
        return RedirectResponse("/", status_code=303)

    pending_quicken: dict[str, tuple] = {}

    @app.get("/quicken", response_class=HTMLResponse)
    def quicken_form(request: Request):
        return page(request, "quicken_form.html", error=None)

    @app.post("/quicken", response_class=HTMLResponse)
    async def quicken_preview(request: Request, file: UploadFile = File(...), date_order: str = Form("")):
        data = await file.read()
        try:
            parsed = qif.read_text(qif.decode(data), date_order=date_order or None)
        except Exception as exc:
            return page(request, "quicken_form.html", error=f"Lecture impossible : {exc}")
        if not parsed.transactions:
            return page(request, "quicken_form.html", error="Aucune transaction trouvée : est-ce bien un export QIF de Quicken ?")
        token = secrets.token_urlsafe(16)
        pending_quicken[token] = (parsed, file.filename or "quicken.qif", hashlib.sha256(data).hexdigest(), data)
        return page(request, "quicken_preview.html", q=parsed, token=token, summary=quicken.summarize(parsed),
                    file_name=file.filename)

    @app.post("/quicken/ordre", response_class=HTMLResponse)
    def quicken_reorder(request: Request, token: str = Form(...), date_order: str = Form(...)):
        item = pending_quicken.pop(token, None)
        if item is None:
            return RedirectResponse("/quicken", status_code=303)
        _, name, digest, data = item
        parsed = qif.read_text(qif.decode(data), date_order=date_order)
        token = secrets.token_urlsafe(16)
        pending_quicken[token] = (parsed, name, digest, data)
        return page(request, "quicken_preview.html", q=parsed, token=token, summary=quicken.summarize(parsed),
                    file_name=name)

    @app.post("/quicken/confirmer", response_class=HTMLResponse)
    def quicken_commit(request: Request, token: str = Form(...)):
        item = pending_quicken.pop(token, None)
        if item is None:
            return RedirectResponse("/quicken", status_code=303)
        parsed, name, digest, _ = item
        report = quicken.migrate(db, parsed, name, digest)
        return page(request, "quicken_report.html", report=report, summary=quicken.summarize(parsed))

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, compte: int | None = None, q: str = "", import_id: int | None = None,
                     statut: str = ""):
        where, args = [], []
        if statut == "a_verifier":
            where.append("t.status != 'confirmed' AND t.transfer_id IS NULL")
        if compte:
            where.append("t.account_id = ?"); args.append(compte)
        if import_id:
            where.append("t.import_id = ?"); args.append(import_id)
        if q:
            where.append("(t.payee LIKE ? OR t.memo LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
        sql = ("SELECT t.*, a.name AS account,"
               " (SELECT COUNT(*) FROM splits s WHERE s.transaction_id = t.id) AS n_splits,"
               " (SELECT COALESCE(p.name || ' : ', '') || c.name FROM splits s JOIN categories c ON c.id = s.category_id"
               "  LEFT JOIN categories p ON p.id = c.parent_id WHERE s.transaction_id = t.id LIMIT 1) AS category,"
               " (SELECT a2.name FROM transactions o JOIN accounts a2 ON a2.id = o.account_id"
               "  WHERE o.id = t.transfer_id) AS transfer_with"
               " FROM transactions t JOIN accounts a ON a.id = t.account_id"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY t.date DESC, t.id DESC LIMIT 500")
        rows = db.execute(sql, args).fetchall()
        total = sum(r["amount_cents"] for r in rows)
        to_review = db.execute("SELECT COUNT(*) FROM transactions WHERE status != 'confirmed'"
                               " AND transfer_id IS NULL").fetchone()[0]
        return page(request, "transactions.html", rows=rows, total=total, compte=compte, q=q, import_id=import_id,
                    statut=statut, to_review=to_review, sources=categorize.SOURCES,
                    back=str(request.url.path) + ("?" + request.url.query if request.url.query else ""))

    def _safe_back(back: str) -> str:
        return back if back.startswith("/transactions") else "/transactions"

    @app.post("/transactions/confirmer")
    async def confirm_shown(request: Request):
        form = await request.form()
        ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
        categorize.confirm(db, ids)
        return RedirectResponse(_safe_back(str(form.get("back", ""))), status_code=303)

    @app.post("/categoriser")
    def run_categorizer(back: str = Form("/transactions")):
        categorize.apply(db)
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.get("/transactions/{tx_id}", response_class=HTMLResponse)
    def edit_transaction(request: Request, tx_id: int, back: str = "/transactions"):
        t = db.execute("SELECT t.*, a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id"
                       " WHERE t.id = ?", (tx_id,)).fetchone()
        if t is None:
            return RedirectResponse("/transactions", status_code=303)
        splits = db.execute("SELECT * FROM splits WHERE transaction_id = ?", (tx_id,)).fetchall()
        key = categorize.merchant_key(t["payee"], t["memo"])
        similar = [r["id"] for r in db.execute(
            "SELECT id, payee, memo FROM transactions WHERE status != 'confirmed' AND transfer_id IS NULL AND id != ?",
            (tx_id,)) if categorize.same_merchant(key, categorize.merchant_key(r["payee"], r["memo"]))]
        paths = categorize.category_paths(db)
        return page(request, "transaction_edit.html", t=t, splits=splits, key=key, similar=len(similar),
                    categories=sorted(paths.items(), key=lambda kv: kv[1].lower()),
                    current=splits[0]["category_id"] if len(splits) == 1 else None,
                    sources=categorize.SOURCES, back=_safe_back(back))

    @app.post("/transactions/{tx_id}")
    def save_transaction(tx_id: int, category_id: str = Form(""), remember: str = Form(""),
                         similar: str = Form(""), back: str = Form("/transactions")):
        t = db.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if t is None or not category_id.isdigit():
            return RedirectResponse(_safe_back(back), status_code=303)
        cat = int(category_id)
        categorize.set_category(db, tx_id, cat)
        key = categorize.merchant_key(t["payee"], t["memo"])
        if remember and key:
            with db:
                categorize.add_rule(db, {"merchant": key}, cat)
        if similar and key:
            for r in db.execute("SELECT id, payee, memo FROM transactions WHERE status != 'confirmed'"
                                " AND transfer_id IS NULL").fetchall():
                if categorize.same_merchant(key, categorize.merchant_key(r["payee"], r["memo"])):
                    categorize.set_category(db, r["id"], cat)
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.post("/categories/depart")
    def starter_categories(back: str = Form("/transactions")):
        categorize.create_starter_categories(db)
        return RedirectResponse(back if back.startswith("/") else "/", status_code=303)

    @app.get("/regles", response_class=HTMLResponse)
    def rules(request: Request):
        paths = categorize.category_paths(db)
        rows = []
        for r in db.execute("SELECT * FROM rules ORDER BY priority, id"):
            cond, action = json.loads(r["conditions"]), json.loads(r["action"])
            rows.append(dict(id=r["id"], merchant=cond.get("merchant") or cond.get("contains", ""),
                             category=paths.get(action.get("category_id"), "?"),
                             source=categorize.SOURCES.get(r["source"], r["source"])))
        return page(request, "rules.html", rules=rows)

    @app.post("/regles/{rule_id}/supprimer")
    def delete_rule(rule_id: int):
        with db:
            db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return RedirectResponse("/regles", status_code=303)

    return app


def main() -> None:  # pragma: no cover - lancement manuel
    import threading
    import webbrowser

    import uvicorn

    port = int(os.environ.get("BUDGET_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    print(f"Budget : {url}  (données : {default_db_path()})")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 : l'application n'est accessible que depuis cet ordinateur.
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
