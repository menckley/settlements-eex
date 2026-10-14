"""Tests hors ligne : réponses EEX simulées au format réel (header / data). Lancer : python -m pytest"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive  # noqa: E402
import rapport_settlements as rs  # noqa: E402

HEADER = ["shortCode", "maturityDate", "tradeDate", "totVolTrdd", "grossOpenInt",
          "grossOpenIntSz", "netOpenInt", "netOpenIntSz", "settlPx"]
SESSION = date(2026, 10, 13)


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self.ok, self._body, self.text, self.headers = status, status < 400, body, str(body), {}

    def json(self):
        return self._body


def fake_get(missing=(), wrong_contract=False):
    def get(url, params, headers, timeout):
        code, mat = params["shortCode"], int(params["maturity"])
        if (code, mat) in missing:
            rows = [[code, mat, "2026-10-12", None, 0, 0, None, None, 1.5]]
        else:
            px = 2.10 if params["commodity"] == "GO" else 150.0
            rows = [[code, mat, "2026-10-12", None, 0, 0, None, None, px - 0.1],
                    [code, mat, "2026-10-13", None, 0, 0, None, None, px]]
        if wrong_contract:
            rows[0][1] = mat + 1
        unit = {"currency": "EUR", "uOM": "MWh"}
        return Resp({"header": HEADER, "data": rows, **unit})
    return get


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(rs, "PAUSE", 0)


def test_contrats_suivis():
    cs = rs.contracts_for(SESSION)
    assert len(cs) == 3 * 3 + 3 * rs.GO_MATURITIES
    go = [c for c in cs if c.commodity == "GO"]
    assert {(c.short_code, c.maturity, c.contrat) for c in go if c.short_code == "EGOS"} == {
        ("EGOS", "202701", "Prod. 2026"), ("EGOS", "202801", "Prod. 2027"), ("EGOS", "202901", "Prod. 2028")}
    egoe = next(c for c in go if c.short_code == "EGOE").params(SESSION)
    assert egoe["product"] == "Renewable Power" and egoe["area"] == "EU" and egoe["maturityType"] == "Month"


def test_collecte_archive_et_mail(monkeypatch, tmp_path):
    monkeypatch.setattr(rs.requests, "get", fake_get(missing={("EGOH", 202901)}))
    rows, errors = rs.collect(SESSION)
    assert len(errors) == 1 and "GO Hydraulique (EU) Prod. 2028" in errors[0]
    found = [r for r in rows if r["settlement"] is not None]
    path = archive.save_session(found, SESSION, tmp_path)
    df = archive.load_settlements(root=tmp_path)
    assert len(df) == len(found) == 17 and set(df["unite"]) == {"MWh"}
    assert df.query("short_code == 'EGOS' and maturity == '202701'")["settlement"].item() == 2.10
    assert archive.load_settlements(marche="GO Solaire (EU)", root=tmp_path)["contrat"].tolist() == \
        ["Prod. 2026", "Prod. 2027", "Prod. 2028"]
    subject, html, text = rs.build_report(rows, errors, SESSION)
    assert subject.startswith("[INCOMPLET]") and "Garanties d'origine" in html and "EUR/MWh" in html
    assert path.name == "session=2026-10-13.csv"


def test_reponse_d_un_autre_contrat_signalee(monkeypatch):
    monkeypatch.setattr(rs.requests, "get", fake_get(wrong_contract=True))
    rows, errors = rs.collect(SESSION)
    assert all(r["settlement"] is None for r in rows)
    assert all("autre contrat" in e for e in errors)


def test_colonnes_absentes_signalees():
    c = rs.contracts_for(SESSION)[0]
    with pytest.raises(rs.DataNotFoundError, match="colonnes absentes"):
        rs.parse_history({"header": ["shortCode", "tradeDate"], "data": []}, c)


def test_archive_refuse_les_lignes_sans_settlement(tmp_path):
    row = {c: "x" for c in archive.COLUMNS} | {"session": SESSION, "settlement": None}
    with pytest.raises(ValueError):
        archive.save_session([row], SESSION, tmp_path)
