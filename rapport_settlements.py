#!/usr/bin/env python3
"""
Rapport quotidien des settlements EEX envoyé par e-mail — VERSION GITHUB ACTIONS.

Même logique que la version locale ; les paramètres d'envoi sont lus dans les
variables d'environnement (alimentées par les Secrets du dépôt GitHub) :
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
  EMAIL_TO (adresses séparées par des virgules), EMAIL_FROM (optionnel),
  MODE (optionnel : "envoi" par défaut, "apercu" ou "explorer").

Contrats suivis (dernière séance disponible, "la veille") :
  - Power Base France : Month-Ahead, Cal-27, Cal-28
  - Gaz naturel TTF   : Month-Ahead, Cal-27, Cal-28
  - Gaz naturel PEG   : Month-Ahead, Cal-27, Cal-28

Séance récupérée : le jour ouvré précédent (le lundi, celle du vendredi).
Source : l'API publique qui alimente les tableaux de www.eex.com
(non documentée, peut évoluer sans préavis). Les requêtes sont construites
automatiquement à partir des codes produits EEX, au format relevé sur eex.com
(une requête par contrat).

"""
from __future__ import annotations

import os
import re
import smtplib
import traceback
import time
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import pandas as pd
import requests

# =============================================================================
# CONFIGURATION
# =============================================================================
MODE = os.getenv("MODE", "envoi").strip().lower()   # "envoi", "apercu" ou "explorer"

# --- Envoi de l'e-mail (Secrets GitHub) ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT") or 465)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM") or SMTP_USER
EMAIL_TO = [a.strip() for a in os.getenv("EMAIL_TO", "").split(",") if a.strip()]

# --- Séance ---
# None = jour ouvré précédent (lundi -> vendredi). Pour tester une date précise :
# DATE_SEANCE = date(2026, 9, 21)
DATE_SEANCE = None

# --- Produits EEX (codes officiels EEX) ---
PRODUITS = {
    "Power Base France": {"commodity": "POWER",  "area": "FR",  "product": "Base",
                          "month": "F7BM", "year": "F7BY"},
    "Gaz TTF":           {"commodity": "NATGAS", "area": "TTF", "product": "Base",
                          "month": "G3BM", "year": "G3BY"},
    "Gaz PEG":           {"commodity": "NATGAS", "area": "PEG", "product": "Base",
                          "month": "G5BM", "year": "G5BY"},
}
API_URL = "https://api.eex-group.com/pub/market-data/table-data"
CALENDARS = [2027, 2028]
LOOKBACK_DAYS = 10   # historique demandé avant la séance (pour la variation)

APERCU = MODE == "apercu"
EXPLORE = MODE == "explorer"

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.eex.com",
    "Referer": "https://www.eex.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

# Noms de colonnes possibles dans la réponse (la première trouvée est utilisée)
TRADE_COLS = ["tradeDate", "tradingDate", "businessDate", "settlementDate", "date", "day"]
PRICE_COLS = ["settlPx", "settlementPrice", "settlement", "settle", "close", "lastPrice", "price"]

MOIS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
           "juil.", "août", "sept.", "oct.", "nov.", "déc."]
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


# =============================================================================
# RÉCUPÉRATION DES DONNÉES
# =============================================================================
def target_session() -> date:
    """Séance voulue : DATE_SEANCE si renseignée, sinon le jour ouvré précédent."""
    if DATE_SEANCE:
        return DATE_SEANCE
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # samedi/dimanche -> vendredi
        d -= timedelta(days=1)
    return d


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def targets_for(kind: str, session: date) -> list[tuple[str, date, str]]:
    """(contrat, début de livraison, libellé d'échéance) attendus pour un type de contrat."""
    if kind == "month":
        start = next_month(session)
        return [("Month-Ahead", start, f"{MOIS_FR[start.month - 1]} {start.year}")]
    return [(f"Cal-{y % 100}", date(y, 1, 1), str(y)) for y in CALENDARS]


def request_variants(market: str, kind: str, start: date) -> list[tuple[str, dict]]:
    """Paramètres à essayer (format relevé sur eex.com), dans l'ordre."""
    p = PRODUITS[market]
    session = target_session()
    params = {
        "shortCode": p[kind], "commodity": p["commodity"], "pricing": "F",
        "area": p["area"], "product": p["product"], "maturity": f"{start:%Y%m}",
        "startDate": (session - timedelta(days=LOOKBACK_DAYS)).isoformat(),
        "endDate": date.today().isoformat(),
        "maturityType": "Month" if kind == "month" else "Year", "isRolling": "true",
    }
    return [
        ("standard", params),
        ("sans product", {k: v for k, v in params.items() if k != "product"}),
        ("isRolling=false", {**params, "isRolling": "false"}),
    ]


def call_api(params: dict) -> pd.DataFrame:
    r = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    if r.status_code in (401, 403):
        raise PermissionError(f"accès refusé par EEX (HTTP {r.status_code})")
    if not r.ok:  # on affiche la réponse du serveur : elle indique souvent le paramètre fautif
        raise ValueError(f"HTTP {r.status_code} – réponse EEX : {r.text[:300]!r}")
    payload = r.json()
    if isinstance(payload, list):
        return pd.DataFrame(payload)
    data = payload.get("data", [])
    if "header" in payload:
        return pd.DataFrame(data, columns=payload["header"])
    return pd.DataFrame(data)


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower = {str(c).lower(): c for c in df.columns}
    return next((lower[c.lower()] for c in candidates if c.lower() in lower), None)


def to_dates(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):  # horodatage en millisecondes
        return pd.to_datetime(series, unit="ms", errors="coerce").dt.date
    return pd.to_datetime(series, errors="coerce").dt.date


def fetch_series(market: str, kind: str, start: date) -> pd.DataFrame:
    """Historique des settlements d'un contrat : colonnes trade (date de séance) et price."""
    last_error = "aucune donnée"
    for name, params in request_variants(market, kind, start):
        try:
            df = call_api(params)
            if df.empty:
                last_error = f"{name} : réponse vide"
                continue
            tcol, pcol = find_col(df, TRADE_COLS), find_col(df, PRICE_COLS)
            if not (tcol and pcol):
                last_error = f"{name} : colonnes non reconnues {list(df.columns)}"
                continue
            out = pd.DataFrame({"trade": to_dates(df[tcol]),
                                "price": pd.to_numeric(df[pcol], errors="coerce")})
            return out.dropna(subset=["trade"])
        except PermissionError:
            raise
        except Exception as e:
            last_error = f"{name} : {e}"
        finally:
            time.sleep(0.3)
    raise ValueError(f"aucune requête exploitable (dernier essai : {last_error})")


def price_on(series: pd.DataFrame, session: date | None) -> float | None:
    if session is None:
        return None
    values = series.loc[series["trade"] == session, "price"].dropna()
    return float(values.iloc[-1]) if not values.empty else None


def collect() -> tuple[list[dict], list[str]]:
    """Récupère tous les contrats pour la séance cible. Renvoie (lignes, anomalies)."""
    session = target_session()
    rows, errors = [], []
    for market in PRODUITS:
        for kind in ("month", "year"):
            for contract, start, label in targets_for(kind, session):
                row = {"marche": market, "contrat": contract, "echeance": label,
                       "seance": None, "prix": None, "prix_prec": None}
                try:
                    series = fetch_series(market, kind, start)
                    trades = set(series["trade"])
                    price = price_on(series, session)
                    if price is None:
                        before = [t for t in trades if t < date.today()]
                        hint = f" (dernière séance disponible : {max(before):%d/%m/%Y})" if before else ""
                        raise ValueError(f"settlement du {session:%d/%m/%Y} non trouvé{hint}")
                    previous = max((t for t in trades if t < session), default=None)
                    row.update(seance=session, prix=price, prix_prec=price_on(series, previous))
                except Exception as e:  # une erreur sur un contrat n'empêche pas les autres
                    errors.append(f"{market} {contract} : {e}")
                rows.append(row)
    return rows, errors


# =============================================================================
# MISE EN FORME DE L'E-MAIL
# =============================================================================
def fr_number(x: float, decimals: int = 2) -> str:
    return f"{x:,.{decimals}f}".replace(",", "\u202f").replace(".", ",")


def fr_date(d: date) -> str:
    return f"{JOURS_FR[d.weekday()]} {d:%d/%m/%Y}"


def variation(p: float | None, q: float | None) -> tuple[str, str]:
    if p is None or q is None:
        return "–", "#888888"
    diff = p - q
    pct = diff / q * 100 if q else 0.0
    if diff > 0:
        return f"▲ +{fr_number(diff)} (+{fr_number(pct, 1)} %)", "#1a7f37"
    if diff < 0:
        return f"▼ −{fr_number(-diff)} (−{fr_number(-pct, 1)} %)", "#c62828"
    return "= 0,00", "#555555"


def build_report(rows: list[dict], errors: list[str]) -> tuple[str, str, str]:
    """Renvoie (objet, html, texte) de l'e-mail de rapport."""
    session = target_session()
    titre = f"Settlements EEX – séance du {session:%d/%m/%Y}"
    subject = f"[INCOMPLET] {titre}" if errors else titre

    td = "padding:8px 12px;border-bottom:1px solid #e5e7eb;"
    html_rows, text_lines = [], []
    for market in PRODUITS:
        m_rows = [r for r in rows if r["marche"] == market]
        for i, r in enumerate(m_rows):
            var_txt, var_col = variation(r["prix"], r["prix_prec"])
            prix = fr_number(r["prix"]) if r["prix"] is not None else "n.d."
            market_cell = (f'<td rowspan="{len(m_rows)}" style="{td}font-weight:600;vertical-align:top;">'
                           f'{market}</td>') if i == 0 else ""
            html_rows.append(
                f"<tr>{market_cell}"
                f'<td style="{td}">{r["contrat"]}</td>'
                f'<td style="{td}color:#555;">{r["echeance"]}</td>'
                f'<td style="{td}text-align:right;font-weight:600;">{prix}</td>'
                f'<td style="{td}text-align:right;color:{var_col};white-space:nowrap;">{var_txt}</td></tr>')
            text_lines.append(f"{market:<18} {r['contrat']:<12} {r['echeance']:<11} "
                              f"{prix:>10}   {var_txt}")

    warn_html = ""
    if errors:
        items = "".join(f"<li>{e}</li>" for e in errors)
        warn_html = (f'<div style="margin-top:20px;padding:12px 16px;background:#fff4e5;'
                     f'border-left:4px solid #f59e0b;"><b>Données manquantes :</b>'
                     f'<ul style="margin:8px 0 0 0;padding-left:20px;">{items}</ul></div>')

    th = "padding:8px 12px;text-align:left;background:#f3f4f6;border-bottom:2px solid #d1d5db;"
    html = f"""<html><body style="font-family:Arial,Helvetica,sans-serif;color:#111;margin:0;padding:16px;">
<h2 style="margin:0 0 4px 0;font-size:20px;">Settlements EEX</h2>
<p style="margin:0 0 16px 0;color:#555;">Séance du {fr_date(session)} · prix en €/MWh</p>
<table style="border-collapse:collapse;font-size:14px;min-width:520px;">
<tr><th style="{th}">Marché</th><th style="{th}">Contrat</th><th style="{th}">Échéance</th>
<th style="{th}text-align:right;">Settlement</th><th style="{th}text-align:right;">Var. vs séance préc.</th></tr>
{''.join(html_rows)}
</table>
{warn_html}
<p style="margin-top:24px;font-size:12px;color:#888;">Source : eex.com – données à titre informatif.</p>
</body></html>"""

    text = (f"Settlements EEX – séance du {fr_date(session)} (€/MWh)\n\n"
            + "\n".join(text_lines)
            + ("\n\nDonnées manquantes :\n- " + "\n- ".join(errors) if errors else "")
            + "\n\nSource : eex.com")
    return subject, html, text


def build_not_found(errors: list[str], details: str | None = None) -> tuple[str, str, str]:
    """E-mail envoyé quand aucune donnée n'a pu être récupérée."""
    session = target_session()
    subject = f"⚠️ Settlements EEX du {session:%d/%m/%Y} : données non trouvées"
    items = "".join(f"<li>{e}</li>" for e in errors)
    details_html = (f'<pre style="font-size:11px;background:#f3f4f6;padding:10px;'
                    f'white-space:pre-wrap;">{details}</pre>') if details else ""
    html = f"""<html><body style="font-family:Arial,Helvetica,sans-serif;color:#111;padding:16px;">
<h2 style="margin:0 0 12px 0;font-size:20px;color:#c62828;">Données non trouvées</h2>
<p>Les settlements EEX de la séance du {fr_date(session)} n'ont pas pu être récupérés.</p>
<ul>{items}</ul>
{details_html}
<p style="font-size:12px;color:#888;">Causes possibles : jour férié sans séance, publication EEX retardée,
modification de l'API eex.com, ou accès bloqué.</p>
</body></html>"""
    text = (f"Les settlements EEX de la séance du {fr_date(session)} n'ont pas pu être récupérés.\n\n- "
            + "\n- ".join(errors) + (f"\n\n{details}" if details else ""))
    return subject, html, text


# =============================================================================
# ENVOI
# =============================================================================
def send_email(subject: str, html: str, text: str) -> None:
    host, port = SMTP_HOST.strip(), int(SMTP_PORT)
    user, password = SMTP_USER, SMTP_PASSWORD
    sender, recipients = EMAIL_FROM, EMAIL_TO
    if not (user and password and recipients):
        raise smtplib.SMTPException("SMTP_USER, SMTP_PASSWORD ou EMAIL_TO manquant (Secrets GitHub)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg, sender, recipients)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg, sender, recipients)


def deliver(subject: str, html: str, text: str) -> None:
    if APERCU:
        with open("apercu_email.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Objet : {subject}\n\n{text}\n\n→ Aperçu enregistré dans apercu_email.html (rien n'a été envoyé)")
    else:
        send_email(subject, html, text)
        print(f"E-mail envoyé : {subject}")


# =============================================================================
# PROGRAMME PRINCIPAL
# =============================================================================
def explore() -> None:
    session = target_session()
    print(f"Séance cible : {fr_date(session)}")
    for market in PRODUITS:
        for kind in ("month", "year"):
            for contract, start, label in targets_for(kind, session):
                print(f"\n===== {market} / {contract} ({PRODUITS[market][kind]}, {label}) =====")
                for name, params in request_variants(market, kind, start):
                    try:
                        df = call_api(params)
                        print(f"[{name}] {len(df)} lignes, colonnes : {list(df.columns)}")
                        if not df.empty:
                            print(df.tail(6).to_string())
                            break
                    except Exception as e:
                        print(f"[{name}] erreur : {e}")
                    time.sleep(0.3)


def main() -> None:
    if EXPLORE:
        explore()
        return
    try:
        rows, errors = collect()
        if any(r["prix"] is not None for r in rows):
            message = build_report(rows, errors)
        else:
            message = build_not_found(errors)
    except Exception as e:  # erreur imprévue pendant la collecte : on prévient par e-mail
        message = build_not_found([f"Erreur inattendue : {e}"], traceback.format_exc())
    try:
        deliver(*message)
    except (smtplib.SMTPException, OSError) as e:
        raise SystemExit(f"\nÉchec de l'envoi de l'e-mail via {SMTP_HOST}:{SMTP_PORT} → {e!r}\n"
                         "Vérifier SMTP_HOST, SMTP_PORT, SMTP_USER et SMTP_PASSWORD.")


if __name__ == "__main__":
    main()
