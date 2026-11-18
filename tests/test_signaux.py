"""Tests hors ligne des signaux, spreads et courbe (signaux.py) et de leur branchement optionnel."""
import struct
import sys
import zlib
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_rapport import SESSION, TODAY, fake_get  # noqa: E402

import rapport_settlements as rs  # noqa: E402
import signaux  # noqa: E402


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(rs, "PAUSE", 0)


def row(session, marche, mtype, maturity, price, unite="MWh"):
    return {"session": session, "marche": marche, "contrat": "x", "echeance": "x", "commodity": "x",
            "area": "x", "product": "x", "short_code": "x", "maturity": maturity, "maturity_type": mtype,
            "settlement": price, "devise": "EUR", "unite": unite}


def sessions(n, start=date(2026, 8, 3)):
    return [d.date() for d in pd.bdate_range(start, periods=n)]


def osc(n):
    return [30 + (0.5 if i % 2 else -0.5) for i in range(n)]


def test_z_score_alerte_sur_un_saut():
    prices = osc(40) + [36.0]
    idx = sessions(len(prices))
    s = pd.Series(prices, index=idx)
    sig = signaux.compute_signal(s, idx[-1])
    assert sig["alerte"] and sig["z"] > signaux.Z_SEUIL
    assert sig["var5"][0] == pytest.approx(36.0 - s.iloc[-6])
    assert sig["position"] == pytest.approx(100.0)


def test_pas_d_alerte_sur_variation_normale_ni_sans_historique():
    idx = sessions(41)
    sig = signaux.compute_signal(pd.Series(osc(41), index=idx), idx[-1])
    assert sig["z"] is not None and not sig["alerte"]
    short = signaux.compute_signal(pd.Series([1.0, 2.0, 9.0], index=sessions(3)), sessions(3)[-1])
    assert short["z"] is None and not short["alerte"] and short["var5"] is None


def test_serie_absente_a_la_seance_donne_des_valeurs_vides():
    sig = signaux.compute_signal(pd.Series([1.0], index=[date(2026, 8, 3)]), date(2026, 8, 4))
    assert sig["prix"] is None and not sig["alerte"]


def test_roll_du_month_ahead_sans_saut_artificiel():
    # Fin octobre : Month-Ahead = 202611 ; en novembre, Month-Ahead = 202612 (prix très différent).
    rows = []
    for d in [date(2026, 10, 29), date(2026, 10, 30)]:
        rows += [row(d, "Gaz TTF", "Month", "202611", 30.0), row(d, "Gaz TTF", "Month", "202612", 50.0)]
    d = date(2026, 11, 2)
    rows += [row(d, "Gaz TTF", "Month", "202612", 51.0), row(d, "Gaz TTF", "Month", "202701", 60.0)]
    df = signaux.add_rank(pd.DataFrame(rows))
    mat, s = signaux.contract_series(df, "Gaz TTF", "Month", 1, d)
    assert mat == "202612" and list(s) == [50.0, 50.0, 51.0]


def test_spreads_valeurs_et_unites():
    d = date(2026, 10, 13)
    rows = [row(d, "Gaz TTF", "Month", "202611", 30.0), row(d, "Gaz PEG", "Month", "202611", 28.0),
            row(d, "Gaz TTF", "Year", "202701", 33.0), row(d, "Gaz PEG", "Year", "202701", 32.0),
            row(d, "Power Base France", "Year", "202701", 80.0)]
    sp = {s["nom"]: s for s in signaux.spreads(signaux.add_rank(pd.DataFrame(rows)), d)}
    assert sp["TTF – PEG, Month-Ahead"]["valeur"] == pytest.approx(2.0)
    assert sp["TTF – PEG, Cal+1"]["valeur"] == pytest.approx(1.0)
    assert sp["TTF Cal+1 – Month-Ahead"]["valeur"] == pytest.approx(3.0)
    assert sp["TTF Cal+1 – Month-Ahead"]["lecture"] == "contango"
    assert sp["Spark spread FR Cal+1 (hors CO2)"]["valeur"] == pytest.approx(80.0 - 33.0 / 0.5)
    rows[0] = row(d, "Gaz TTF", "Month", "202611", 30.0, unite="therm")      # unités différentes : omis
    sp = {s["nom"] for s in signaux.spreads(signaux.add_rank(pd.DataFrame(rows)), d)}
    assert "TTF – PEG, Month-Ahead" not in sp and "TTF Cal+1 – Month-Ahead" not in sp
    assert "TTF – PEG, Cal+1" in sp


def test_png_valide():
    png = signaux.curve_png({"jour": [30.0, 31, None, 33, 34, 35, 36, 37, 38], "J-7": [29.0, 30, 31, 32, 33, 34, 35,
                                                                                     36, 37]})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (640, 240)
    idat_len = struct.unpack(">I", png[33:37])[0]
    assert len(zlib.decompress(png[41:41 + idat_len])) == h * (1 + 3 * w)
    assert signaux.curve_png({"jour": [30.0] + [None] * 8}) is None


def run_signaux_main(monkeypatch, enabled):
    sent = []
    monkeypatch.setattr(rs.requests, "get", fake_get())
    monkeypatch.setattr(rs, "target_session", lambda: SESSION)
    monkeypatch.setattr(rs, "APERCU", True)
    monkeypatch.setattr(rs, "SIGNAUX", enabled)
    monkeypatch.setattr(rs, "deliver", lambda *m: sent.append(m))
    monkeypatch.setattr(rs.archive, "load_settlements", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(rs, "date", type("D", (date,), {"today": classmethod(lambda cls: TODAY)}))
    rs.main()
    return sent[0]


def test_mail_inchange_sans_option(monkeypatch):
    subject, html, text, images = run_signaux_main(monkeypatch, False)
    assert "Signaux" not in html and "Signaux" not in text and images == {}
    assert html == rs.build_report(*rs.report_rows(SESSION, rs.fetch_window(TODAY)[1]), SESSION)[1]


def test_mail_avec_signaux_et_image_inline(monkeypatch):
    subject, html, text, images = run_signaux_main(monkeypatch, True)
    assert "Signaux" in html and "Spreads" in html and "cid:courbe" in html
    assert images["courbe"][:4] == b"\x89PNG" and "Signaux" in text


def test_un_echec_des_signaux_n_empeche_pas_le_mail(monkeypatch):
    def boom(*a):
        raise RuntimeError("boom")
    monkeypatch.setattr(rs.signaux, "build_signals", boom)
    subject, html, text, images = run_signaux_main(monkeypatch, True)
    assert "Signaux" not in html and images == {} and "Settlements EEX" in subject


def test_send_email_joint_l_image_inline(monkeypatch):
    captured = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg, *a):
            captured.append(msg)

    monkeypatch.setattr(rs.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(rs, "SMTP_PORT", 465)
    monkeypatch.setattr(rs, "SMTP_USER", "u")
    monkeypatch.setattr(rs, "SMTP_PASSWORD", "p")
    monkeypatch.setattr(rs, "EMAIL_TO", ["a@example.com"])
    rs.send_email("s", "<img src='cid:courbe'>", "t", {"courbe": b"\x89PNG\r\n\x1a\nxx"})
    parts = [p.get_content_type() for p in captured[0].walk()]
    assert "image/png" in parts and captured[0].get_payload()[1].get_content_type() == "multipart/related"
