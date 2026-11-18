"""Signaux, spreads et courbe gaz calculés depuis l'historique stocké (aucun appel EEX).

Usage interne uniquement (licence EEX). Voir docs/specs/alertes-seuils-et-courbe-gaz.md.
Fonctions pures : elles prennent un DataFrame au format `archive.COLUMNS` (colonne `session` en date).
"""
from __future__ import annotations

import struct
import zlib
from datetime import date, timedelta

import numpy as np
import pandas as pd

POWER, TTF, PEG = "Power Base France", "Gaz TTF", "Gaz PEG"
SERIES = [(POWER, "Month", 1, "Month-Ahead"), (POWER, "Year", 1, "Cal+1"),
          (TTF, "Month", 1, "Month-Ahead"), (TTF, "Year", 1, "Cal+1"),
          (PEG, "Month", 1, "Month-Ahead"), (PEG, "Year", 1, "Cal+1")]

Z_SEUIL = 2.5          # alerte si |z| dépasse ce seuil
Z_FENETRE = 60         # variations quotidiennes précédentes servant à l'écart-type
Z_MIN_OBS = 20         # historique minimal pour un z-score
AMPLITUDE_JOURS = 365  # amplitude « 52 semaines »
RENDEMENT_CCGT = 0.50  # rendement (PCI) du CCGT de référence pour le spark spread approché
COURBE_COULEURS = {"jour": (31, 78, 121), "J-7": (237, 125, 49), "J-30": (127, 127, 127)}


def add_rank(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute `rang` : écart en mois (Month) ou en années (Year) entre la maturité et la séance."""
    out = df.copy()
    if out.empty:
        out["rang"] = pd.Series(dtype=int)
        return out
    sess = pd.to_datetime(out["session"])
    mat = out["maturity"].astype(str)
    year, month = mat.str[:4].astype(int), mat.str[4:6].astype(int)
    months = (year - sess.dt.year) * 12 + (month - sess.dt.month)
    out["rang"] = np.where(out["maturity_type"] == "Month", months, year - sess.dt.year)
    return out


def contract_series(df: pd.DataFrame, marche: str, mtype: str, rank: int, session: date):
    """(maturité, série des settlements de CE contrat jusqu'à la séance) pour le rang voulu à `session`."""
    d = df[(df["marche"] == marche) & (df["maturity_type"] == mtype)]
    d = d[d["session"] <= session]
    at = d[(d["session"] == session) & (d["rang"] == rank)]
    if at.empty:
        return None, pd.Series(dtype=float)
    mat = str(at["maturity"].iloc[0])
    s = d[d["maturity"].astype(str) == mat].set_index("session")["settlement"].astype(float).sort_index()
    return mat, s[~s.index.duplicated(keep="last")]


def compute_signal(s: pd.Series, session: date) -> dict:
    """Variations 5/20 séances, position dans l'amplitude 52 semaines, z-score du jour."""
    out = {"prix": None, "var5": None, "var20": None, "position": None, "n_amplitude": 0,
           "z": None, "alerte": False}
    if s.empty or s.index[-1] != session:
        return out
    p = float(s.iloc[-1])
    out["prix"] = p
    for n in (5, 20):
        if len(s) > n:
            q = float(s.iloc[-1 - n])
            out[f"var{n}"] = (p - q, (p - q) / q * 100 if q else None)
    recent = s[[i >= session - timedelta(days=AMPLITUDE_JOURS) for i in s.index]]
    out["n_amplitude"] = len(recent)
    if len(recent) >= 2 and recent.max() > recent.min():
        out["position"] = (p - recent.min()) / (recent.max() - recent.min()) * 100
    d = s.diff().dropna()
    hist = d.iloc[:-1].tail(Z_FENETRE)
    if len(d) and len(hist) >= Z_MIN_OBS and hist.std() > 0:
        out["z"] = float((d.iloc[-1] - hist.mean()) / hist.std())
        out["alerte"] = abs(out["z"]) > Z_SEUIL
    return out


def signals_table(df: pd.DataFrame, session: date) -> list[dict]:
    rows = []
    for marche, mtype, rank, label in SERIES:
        mat, s = contract_series(df, marche, mtype, rank, session)
        if mat is None:
            continue
        rows.append({"marche": marche, "contrat": label, "maturity": mat, **compute_signal(s, session)})
    return rows


def _price(df: pd.DataFrame, marche: str, mtype: str, rank: int, session: date):
    d = df[(df["session"] == session) & (df["marche"] == marche)
           & (df["maturity_type"] == mtype) & (df["rang"] == rank)]
    if d.empty:
        return None
    return float(d["settlement"].iloc[0]), f"{d['devise'].iloc[0]}/{d['unite'].iloc[0]}"


def spreads(df: pd.DataFrame, session: date, rendement: float = RENDEMENT_CCGT) -> list[dict]:
    """Spreads de la séance ; un spread n'est calculé que si les deux unités publiées sont identiques."""
    g = {(m, t, r): _price(df, m, t, r, session) for m in (POWER, TTF, PEG) for t in ("Month", "Year")
         for r in (1,)}
    out = []

    def add(nom, a, b, formule, coef=1.0):
        if a is None or b is None or a[1] != b[1]:
            return
        out.append({"nom": nom, "valeur": a[0] - b[0] * coef, "unite": a[1], "formule": formule})

    add("TTF – PEG, Month-Ahead", g[(TTF, "Month", 1)], g[(PEG, "Month", 1)], "TTF M+1 − PEG M+1")
    add("TTF – PEG, Cal+1", g[(TTF, "Year", 1)], g[(PEG, "Year", 1)], "TTF Cal+1 − PEG Cal+1")
    add("TTF Cal+1 – Month-Ahead", g[(TTF, "Year", 1)], g[(TTF, "Month", 1)], "TTF Cal+1 − TTF M+1")
    add("Spark spread FR Cal+1 (hors CO2)", g[(POWER, "Year", 1)], g[(TTF, "Year", 1)],
        f"Power FR Cal+1 − TTF Cal+1 / {fr(rendement)}", 1 / rendement)
    for r in out:
        if r["nom"].startswith("TTF Cal+1"):
            r["lecture"] = "contango" if r["valeur"] > 0 else "backwardation" if r["valeur"] < 0 else "plat"
    return out


def fr(x: float, decimals: int = 2) -> str:
    return f"{x:,.{decimals}f}".replace(",", " ").replace(".", ",")


def _signed(x: float, decimals: int = 2) -> str:
    return ("+" if x > 0 else "−" if x < 0 else "") + fr(abs(x), decimals)


def _var_txt(v) -> str:
    if v is None:
        return "n.d."
    diff, pct = v
    return f"{_signed(diff)} ({_signed(pct, 1)} %)" if pct is not None else _signed(diff)


# --- Courbe -----------------------------------------------------------------------------------
def curve_points(df: pd.DataFrame, marche: str, session: date) -> list[float | None]:
    """Prix de M+1…M+6 puis Cal+1…Cal+3 (None si absent) pour une séance."""
    d = df[(df["session"] == session) & (df["marche"] == marche)]
    pts = []
    for mtype, ranks in (("Month", range(1, 7)), ("Year", range(1, 4))):
        for r in ranks:
            x = d[(d["maturity_type"] == mtype) & (d["rang"] == r)]
            pts.append(float(x["settlement"].iloc[0]) if len(x) else None)
    return pts


def curve_dates(df: pd.DataFrame, marche: str, session: date) -> dict[str, date]:
    """Séances comparées : jour, puis la dernière séance stockée au plus tard à J-7 et J-30."""
    sessions = sorted(set(df[df["marche"] == marche]["session"]))
    out = {"jour": session}
    for label, n in (("J-7", 7), ("J-30", 30)):
        before = [s for s in sessions if s <= session - timedelta(days=n)]
        if before:
            out[label] = before[-1]
    return out


def _png(rgb: np.ndarray) -> bytes:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def curve_png(curves: dict[str, list[float | None]], width: int = 640, height: int = 240) -> bytes | None:
    """PNG (sans texte) des courbes ; None s'il n'y a pas au moins 2 points sur la courbe du jour."""
    vals = [v for c in curves.values() for v in c if v is not None]
    if sum(v is not None for v in curves.get("jour", [])) < 2:
        return None
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1.0
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    left, right, top, bottom = 16, width - 16, 16, height - 16
    img[bottom, left:right + 1] = (200, 200, 200)
    img[top:bottom + 1, left] = (200, 200, 200)

    def xy(i: int, v: float) -> tuple[float, float]:
        n = len(curves["jour"])
        return left + (right - left) * i / (n - 1), bottom - (bottom - top) * (v - lo) / (hi - lo)

    def dot(x: float, y: float, col, r: int = 1) -> None:
        xi, yi = int(round(x)), int(round(y))
        img[max(yi - r, 0):yi + r + 1, max(xi - r, 0):xi + r + 1] = col

    for label in ("J-30", "J-7", "jour"):      # le jour est dessiné en dernier, au premier plan
        pts = curves.get(label)
        if not pts:
            continue
        col = COURBE_COULEURS[label]
        prev = None
        for i, v in enumerate(pts):
            if v is None:
                prev = None
                continue
            cur = xy(i, v)
            if prev is not None:
                steps = int(max(abs(cur[0] - prev[0]), abs(cur[1] - prev[1]))) + 1
                for k in range(steps + 1):
                    dot(prev[0] + (cur[0] - prev[0]) * k / steps, prev[1] + (cur[1] - prev[1]) * k / steps, col)
            prev = cur
        for i, v in enumerate(pts):
            if v is not None:
                dot(*xy(i, v), col, 3)
    return _png(img)


# --- Bloc e-mail -------------------------------------------------------------------------------
def build_signals(df: pd.DataFrame, session: date) -> tuple[str, str, bytes | None] | None:
    """(html, texte, png) du bloc « Signaux » ; None si l'historique ne contient rien pour la séance."""
    df = add_rank(df)
    table = signals_table(df, session)
    if not table:
        return None
    sp = spreads(df, session)
    td = "padding:6px 10px;border-bottom:1px solid #e5e7eb;"
    th = "padding:6px 10px;text-align:left;background:#f3f4f6;border-bottom:2px solid #d1d5db;"
    html_rows, text = [], ["\nSignaux (variations en séances, historique stocké)"]
    alerts = []
    for r in table:
        pos = f"{fr(r['position'], 0)} % ({r['n_amplitude']} obs.)" if r["position"] is not None else "n.d."
        z = fr(r["z"]) if r["z"] is not None else "n.d."
        flag = r["alerte"]
        if flag:
            alerts.append(f"{r['marche']} {r['contrat']} : variation du jour atypique (z = {z})")
        style = td + ("background:#fff4e5;font-weight:600;" if flag else "")
        html_rows.append(
            f'<tr><td style="{style}">{r["marche"]} {r["contrat"]}</td>'
            f'<td style="{style}text-align:right;">{_var_txt(r["var5"])}</td>'
            f'<td style="{style}text-align:right;">{_var_txt(r["var20"])}</td>'
            f'<td style="{style}text-align:right;">{pos}</td>'
            f'<td style="{style}text-align:right;">{z}{" ⚠" if flag else ""}</td></tr>')
        text.append(f"{r['marche'] + ' ' + r['contrat']:<32} 5s {_var_txt(r['var5'])} | 20s {_var_txt(r['var20'])}"
                    f" | amplitude {pos} | z {z}{' ALERTE' if flag else ''}")
    html = ('<h3 style="margin:20px 0 8px 0;font-size:16px;">Signaux</h3>'
            f'<table style="border-collapse:collapse;font-size:13px;"><tr><th style="{th}">Contrat</th>'
            f'<th style="{th}text-align:right;">Var. 5 séances</th>'
            f'<th style="{th}text-align:right;">Var. 20 séances</th>'
            f'<th style="{th}text-align:right;">Position amplitude 52 sem.</th>'
            f'<th style="{th}text-align:right;">z-score jour</th></tr>{"".join(html_rows)}</table>'
            f'<p style="font-size:12px;color:#555;margin:6px 0 0 0;">Alerte (⚠) si |z| &gt; {fr(Z_SEUIL, 1)} '
            f'(variation du jour vs {Z_FENETRE} variations précédentes du même contrat).</p>')
    if alerts:
        html += ('<div style="margin-top:10px;padding:10px 14px;background:#fff4e5;border-left:4px solid #f59e0b;">'
                 + "<br>".join(f"<b>Alerte :</b> {a}" for a in alerts) + "</div>")
        text += [f"ALERTE : {a}" for a in alerts]
    if sp:
        items = "".join(
            f'<tr><td style="{td}">{s["nom"]}</td><td style="{td}text-align:right;font-weight:600;">'
            f'{_signed(s["valeur"])} {s["unite"]}{" · " + s["lecture"] if "lecture" in s else ""}</td>'
            f'<td style="{td}color:#555;">{s["formule"]}</td></tr>' for s in sp)
        html += (f'<h3 style="margin:20px 0 8px 0;font-size:16px;">Spreads</h3>'
                 f'<table style="border-collapse:collapse;font-size:13px;">{items}</table>'
                 '<p style="font-size:12px;color:#555;margin:6px 0 0 0;">Spark spread indicatif : rendement '
                 f'{fr(RENDEMENT_CCGT)} (PCI), sans coût CO2 (non disponible).</p>')
        text.append("\nSpreads")
        text += [f"{s['nom']:<36} {_signed(s['valeur'])} {s['unite']}"
                 f"{' (' + s['lecture'] + ')' if 'lecture' in s else ''}" for s in sp]

    png = None
    dates = curve_dates(df, TTF, session)
    curves = {k: curve_points(df, TTF, d) for k, d in dates.items()}
    if sum(v is not None for v in curves["jour"]) >= 2:
        png = curve_png(curves)
    if png:
        legend = " · ".join(f'<span style="color:rgb{COURBE_COULEURS[k]};">■ {k} ({d:%d/%m})</span>'
                            for k, d in dates.items())
        html += ('<h3 style="margin:20px 0 8px 0;font-size:16px;">Courbe TTF</h3>'
                 '<img src="cid:courbe" alt="Courbe TTF" style="max-width:100%;">'
                 f'<p style="font-size:12px;color:#555;margin:4px 0 0 0;">{legend}<br>Abscisse : M+1…M+6 puis '
                 f'Cal+1…Cal+3 ; ordonnée de {fr(min(v for c in curves.values() for v in c if v is not None))} '
                 f'à {fr(max(v for c in curves.values() for v in c if v is not None))}.</p>')
        text.append("\nCourbe TTF : voir le graphique de la version HTML")
    return html, "\n".join(text), png
