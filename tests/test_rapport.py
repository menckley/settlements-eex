"""Tests hors ligne : réponses EEX simulées au format réel (header / data). Lancer : python -m pytest"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive  # noqa: E402
import rapport_settlements as rs  # noqa: E402

HEADER = ["shortCode", "maturityDate", "tradeDate", "totVolTrdd", "grossOpenInt",
          "grossOpenIntSz", "netOpenInt", "netOpenIntSz", "settlPx"]
TODAY = date(2026, 10, 14)
SESSION = date(2026, 10, 13)
SEANCES = ["2026-09-30", "2026-10-01", "2026-10-12", "2026-10-13"]


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self.ok, self._body, self.text, self.headers = status, status < 400, body, str(body), {}

    def json(self):
        return self._body


def fake_get(missing=(), wrong_contract=False, calls=None):
    def get(url, params, headers, timeout):
        code, mat = params["shortCode"], int(params["maturity"])
        if calls is not None:
            calls.append((code, mat))
        base = 2.0 if params["commodity"] == "GO" else 150.0
        dates = SEANCES[:-1] if (code, mat) in missing else SEANCES
        rows = [[code, mat, d, None, 0, 0, None, None, base + i / 10] for i, d in enumerate(dates)]
        if wrong_contract:
            rows[0][1] = mat + 1
        return Resp({"header": HEADER, "data": rows, "currency": "EUR", "uOM": "MWh"})
    return get


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(rs, "PAUSE", 0)


def test_contrats_suivis():
    cs = rs.contracts_for(SESSION)
    assert len(cs) == 3 * 3 + 3 * rs.GO_MATURITIES
    egos = {(c.maturity, c.contrat) for c in cs if c.short_code == "EGOS"}
    assert egos == {("202701", "Prod. 2026"), ("202801", "Prod. 2027"), ("202901", "Prod. 2028")}
    p = next(c for c in cs if c.short_code == "EGOE").params(date(2026, 10, 12), date(2026, 10, 16))
    assert (p["product"], p["area"], p["maturityType"], p["maturity"]) == ("Renewable Power", "EU", "Month", "202701")


def test_historique_complet_en_un_lancement(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(rs.requests, "get", fake_get(calls=calls))
    history, fetched = rs.fetch_window(TODAY)
    assert len(calls) == len(set(calls))                         # une requête par contrat
    path, added, total = archive.upsert(history, tmp_path / "settlements.csv")
    df = archive.load_settlements(path=path)
    assert set(df["session"]) == {date.fromisoformat(d) for d in SEANCES}
    # Month-Ahead : celui du mois suivant chaque séance
    ma = df[df["contrat"] == "Month-Ahead"]
    assert set(ma.loc[ma["session"] == date(2026, 9, 30), "maturity"]) == {"202610"}
    assert set(ma.loc[ma["session"] == SESSION, "maturity"]) == {"202611"}
    assert len(df[df["session"] == SESSION]) == 18 and added == total == len(df)


def test_relance_sans_doublon_et_historique_conserve(monkeypatch, tmp_path):
    monkeypatch.setattr(rs.requests, "get", fake_get())
    f = tmp_path / "settlements.csv"
    old_row = {**rs.contracts_for(date(2026, 6, 1))[0].__dict__, "session": date(2026, 6, 1),
               "settlement": 90.0, "devise": "EUR", "unite": "MWh"}
    archive.upsert([old_row], f)                                  # séance ancienne déjà stockée
    history, _ = rs.fetch_window(TODAY)
    _, added1, total1 = archive.upsert(history, f)
    _, added2, total2 = archive.upsert(history, f)                # même lancement rejoué
    assert added2 == 0 and total2 == total1 == len(history) + 1
    assert archive.load_settlements(end="2026-06-30", path=f)["settlement"].tolist() == [90.0]


def test_mail_et_donnees_manquantes(monkeypatch):
    monkeypatch.setattr(rs.requests, "get", fake_get(missing={("EGOH", 202901)}))
    _, fetched = rs.fetch_window(TODAY)
    rows, errors = rs.report_rows(SESSION, fetched)
    assert len(errors) == 1 and "GO Hydraulique (EU) Prod. 2028" in errors[0] and "12/10/2026" in errors[0]
    egos = next(r for r in rows if r["short_code"] == "EGOS" and r["maturity"] == "202701")
    assert (egos["settlement"], egos["prix_prec"]) == (2.3, 2.2)
    subject, html, _ = rs.build_report(rows, errors, SESSION)
    assert subject.startswith("[INCOMPLET]") and "Garanties d'origine" in html and "EUR/MWh" in html


def test_reponse_d_un_autre_contrat_signalee(monkeypatch):
    monkeypatch.setattr(rs.requests, "get", fake_get(wrong_contract=True))
    history, fetched = rs.fetch_window(TODAY)
    rows, errors = rs.report_rows(SESSION, fetched)
    assert history == [] and all("autre contrat" in e for e in errors)


def test_colonnes_absentes_signalees():
    with pytest.raises(rs.DataNotFoundError, match="colonnes absentes"):
        rs.parse_history({"header": ["shortCode", "tradeDate"], "data": []}, rs.contracts_for(SESSION)[0])


def test_historique_refuse_les_valeurs_manquantes(tmp_path):
    row = {c: "x" for c in archive.COLUMNS} | {"session": SESSION, "settlement": None}
    with pytest.raises(ValueError):
        archive.upsert([row], tmp_path / "settlements.csv")
