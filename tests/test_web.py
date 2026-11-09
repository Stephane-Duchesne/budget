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
