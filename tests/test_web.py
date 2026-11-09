from fastapi.testclient import TestClient

from budget.web.app import create_app, money


def test_parcours_import(tmp_path, fixtures):
    client = TestClient(create_app(tmp_path / "test.sqlite"))
    assert "Aucun compte" in client.get("/").text

    r = client.post("/comptes/nouveau", data={"name": "Chèques", "kind": "imported", "nature": "asset",
                                              "opening": "100,00", "opening_date": ""})
    assert r.status_code == 200 and "Chèques" in r.text

    qfx = (fixtures / "checking.qfx").read_bytes()
    preview = client.post("/importer", data={"account_id": "1"}, files={"file": ("cheques.qfx", qfx)})
    assert "3</b>nouvelles" in preview.text and "Solde du relevé" in preview.text
    token = preview.text.split('name="token" value="')[1].split('"')[0]

    done = client.post("/importer/confirmer", data={"token": token})
    assert "HYDRO-QUEBEC" in done.text
    assert "-4\u00a0484,50\u00a0$" in client.get("/").text   # 100 - 84,50 + 2500 - 7000

    again = client.post("/importer", data={"account_id": "1"}, files={"file": ("cheques.qfx", qfx)})
    assert "déjà été importé" in again.text and "3</b>déjà présentes" in again.text


def test_fichier_illisible(tmp_path):
    client = TestClient(create_app(tmp_path / "test.sqlite"))
    client.post("/comptes/nouveau", data={"name": "X", "kind": "imported", "nature": "asset", "opening": "0"})
    r = client.post("/importer", data={"account_id": "1"}, files={"file": ("notes.txt", b"bonjour")})
    assert "Lecture impossible" in r.text


def test_format_monetaire():
    assert money("-1234.5") == "-1 234,50 $"


def test_reprise_quicken(tmp_path):
    from test_quicken import EXPORT

    client = TestClient(create_app(tmp_path / "test.sqlite"))
    data = EXPORT.encode("cp1252")
    preview = client.post("/quicken", files={"file": ("quicken.qif", data)})
    assert "Aperçu de la reprise" in preview.text and "2 838,00 $" in preview.text
    token = preview.text.split('name="token" value="')[1].split('"')[0]
    # Changer l'ordre des dates renvoie un nouvel aperçu
    preview = client.post("/quicken/ordre", data={"token": token, "date_order": "MDY"})
    token = preview.text.split('name="token" value="')[1].split('"')[0]
    done = client.post("/quicken/confirmer", data={"token": token})
    assert "Historique repris" in done.text and "1 virements liés" in done.text
    home = client.get("/").text
    assert "Chèques" in home and "REER" in home


def test_modifier_compte_placement_et_carte(tmp_path):
    client = TestClient(create_app(tmp_path / "t.sqlite"))
    client.post("/comptes/nouveau", data={"name": "ETrade", "kind": "manual", "nature": "asset",
                                          "opening": "0", "opening_date": ""})
    client.post("/comptes/nouveau", data={"name": "Infinite", "kind": "imported", "nature": "asset",
                                          "opening": "-150", "opening_date": ""})
    r = client.post("/comptes/1", data={"name": "ETrade", "nature": "asset", "currency": "USD",
                                        "value": "121 056,00", "value_date": "2026-11-01"})
    assert "121\u00a0056,00\u00a0$\u00a0US" in r.text and "Valeur au 2026-11-01" in r.text
    r = client.post("/comptes/2", data={"name": "Infinite", "nature": "liability", "currency": "CAD",
                                        "value": "", "value_date": ""})
    assert "150,00\u00a0$</div><div class=\"meta\">Solde dû" in r.text
    # Valeur nette en $ CA seulement : -150 ; le compte en $ US est affiché à part.
    assert "<strong>-150,00\u00a0$</strong>" in r.text
    r = client.post("/comptes/1", data={"name": "ETrade", "nature": "asset", "currency": "USD",
                                        "value": "10", "value_date": ""})
    assert "Indiquez la date" in r.text
