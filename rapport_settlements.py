#!/usr/bin/env python3
"""
Rapport quotidien des settlements EEX envoyé par e-mail, et archive des séances.

Paramètres lus dans les variables d'environnement (Secrets du dépôt GitHub) :
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
  EMAIL_TO (adresses séparées par des virgules), EMAIL_FROM (optionnel),
  MODE (optionnel) : "envoi" (défaut), "apercu" (aperçu du mail, rien n'est envoyé ni
                     stocké) ou "explorer" (affiche les réponses brutes d'EEX).

Contrats suivis (dernière séance, « la veille ») :
  - Power Base France : Month-Ahead, Cal-27, Cal-28
  - Gaz naturel TTF   : Month-Ahead, Cal-27, Cal-28
  - Gaz naturel PEG   : Month-Ahead, Cal-27, Cal-28
  - Garanties d'origine européennes (futures EEX) : solaire, renouvelable, hydraulique,
    sur les GO_MATURITIES prochaines échéances de janvier. L'échéance de janvier A
    couvre la production de mars à décembre de l'année A-1 (spécification EEX).

Historique : tous les settlements sont stockés dans un seul fichier, data/settlements.csv
(une ligne par séance et par contrat). À chaque lancement, chaque contrat est interrogé sur les
HISTORY_DAYS derniers jours et toutes les séances trouvées sont ajoutées ou mises à jour :
le premier lancement récupère d'un coup tout ce qu'EEX publie encore (environ 45 jours), et
une séance manquée un jour est rattrapée le lendemain. Les séances plus anciennes déjà
stockées ne sont jamais supprimées. Le workflow GitHub committe le fichier : c'est la base
de données lue par les autres projets (voir archive.py).

Source : l'API publique qui alimente les tableaux de www.eex.com (non documentée, peut
évoluer sans préavis). Lecture stricte : une seule forme de requête par contrat, colonnes
lues par leur nom exact ; tout écart est signalé, aucune valeur n'est inventée.
"""
from __future__ import annotations

import os
import smtplib
import time
import traceback
from dataclasses import dataclass
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import pandas as pd
import requests

import archive

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

# --- Produits EEX (codes officiels, format de requête relevé sur eex.com) ---
FORWARDS = {
    "Power Base France": {"commodity": "POWER", "area": "FR", "product": "Base",
                          "month": "F7BM", "year": "F7BY"},
    "Gaz TTF":           {"commodity": "NATGAS", "area": "TTF", "product": "Base",
                          "month": "G3BM", "year": "G3BY"},
    "Gaz PEG":           {"commodity": "NATGAS", "area": "PEG", "product": "Base",
                          "month": "G5BM", "year": "G5BY"},
}
CALENDARS = [2027, 2028]

GUARANTEES_OF_ORIGIN = {
    "GO Solaire (EU)":      {"commodity": "GO", "area": "EU", "product": "Solar", "code": "EGOS"},
    "GO Renouvelable (EU)": {"commodity": "GO", "area": "EU", "product": "Renewable Power", "code": "EGOE"},
    "GO Hydraulique (EU)":  {"commodity": "GO", "area": "EU", "product": "Hydro", "code": "EGOH"},
}
GO_MATURITIES = 3     # échéances de janvier suivies, à partir de la première après la séance

API_URL = "https://api.eex-group.com/pub/market-data/table-data"
PAUSE = 1.0           # secondes entre deux requêtes (limite de débit EEX)
REQUIRED_COLS = ("shortCode", "maturityDate", "tradeDate", "settlPx")
RETRY_STATUS = {429, 500, 502, 503, 504}

APERCU = MODE == "apercu"
EXPLORE = MODE == "explorer"
HISTORY_DAYS = 60     # fenêtre interrogée à chaque lancement (EEX ne publie qu'environ 45 jours)

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.eex.com",
    "Referer": "https://www.eex.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

MOIS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
           "juil.", "août", "sept.", "oct.", "nov.", "déc."]
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class DataNotFoundError(RuntimeError):
    """Donnée absente ou réponse EEX incohérente : on n'invente jamais de valeur."""


# =============================================================================
# CONTRATS
# =============================================================================
@dataclass(frozen=True)
class Contract:
    marche: str
    contrat: str          # libellé : Month-Ahead, Cal-27, Prod. 2026...
    echeance: str         # libellé lisible de l'échéance
    commodity: str
    area: str
    product: str
    short_code: str
    maturity: str         # AAAAMM, paramètre `maturity` de la requête
    maturity_type: str    # Month / Year

    def params(self, start: date, end: date) -> dict:
        return {
            "shortCode": self.short_code, "commodity": self.commodity, "pricing": "F",
            "area": self.area, "product": self.product, "maturity": self.maturity,
            "startDate": start.isoformat(), "endDate": end.isoformat(),
            "maturityType": self.maturity_type, "isRolling": "true",
        }


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


def contracts_for(session: date) -> list[Contract]:
    out = []
    for name, p in FORWARDS.items():
        m = next_month(session)
        out.append(Contract(name, "Month-Ahead", f"{MOIS_FR[m.month - 1]} {m.year}", p["commodity"],
                            p["area"], p["product"], p["month"], f"{m:%Y%m}", "Month"))
        for y in CALENDARS:
            out.append(Contract(name, f"Cal-{y % 100}", str(y), p["commodity"], p["area"],
                                p["product"], p["year"], f"{y}01", "Year"))
    # GO : échéances de janvier strictement postérieures à l'année de la séance
    for name, p in GUARANTEES_OF_ORIGIN.items():
        for y in range(session.year + 1, session.year + 1 + GO_MATURITIES):
            out.append(Contract(name, f"Prod. {y - 1}", f"janv. {y}", p["commodity"], p["area"],
                                p["product"], p["code"], f"{y}01", "Month"))
    return out


# =============================================================================
# RÉCUPÉRATION DES DONNÉES
# =============================================================================
def call_api(params: dict, max_retries: int = 4, backoff: float = 15.0) -> dict:
    """Appel brut de l'API table-data ; nouvel essai sur limite de débit / erreur serveur."""
    label = f"{params.get('shortCode')} {params.get('maturity')}"
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise DataNotFoundError(f"{label} : requête impossible : {exc!r}") from exc
            time.sleep(backoff * 2 ** attempt)
            continue
        if r.status_code in RETRY_STATUS and attempt < max_retries:
            try:
                wait = max(float(r.headers.get("Retry-After")), 1.0)
            except (TypeError, ValueError):
                wait = backoff * 2 ** attempt
            time.sleep(wait)
            continue
        if r.status_code in (401, 403):
            raise PermissionError(f"accès refusé par EEX (HTTP {r.status_code})")
        if not r.ok:
            raise DataNotFoundError(f"{label} : HTTP {r.status_code} – réponse EEX : {r.text[:300]!r}")
        try:
            return r.json()
        except ValueError as exc:
            raise DataNotFoundError(f"{label} : réponse non JSON : {r.text[:300]!r}") from exc
    raise DataNotFoundError(f"{label} : échec après {max_retries} nouveaux essais")


def parse_history(payload: dict, c: Contract) -> tuple[pd.Series, str | None, str | None]:
    """Réponse table-data -> (settlements indexés par séance, devise, unité)."""
    if not isinstance(payload, dict) or "header" not in payload or "data" not in payload:
        raise DataNotFoundError("réponse au format inattendu")
    missing = [col for col in REQUIRED_COLS if col not in payload["header"]]
    if missing:
        raise DataNotFoundError(f"colonnes absentes {missing} (reçues : {payload['header']})")
    df = pd.DataFrame(payload["data"], columns=payload["header"])
    if df.empty:
        return pd.Series(dtype=float), payload.get("currency"), payload.get("uOM")
    other = df[(df["shortCode"] != c.short_code) | (df["maturityDate"].astype(str) != c.maturity)]
    if len(other):
        raise DataNotFoundError("la réponse contient des lignes d'un autre contrat")
    trades = [date.fromisoformat(str(x)[:10]) for x in df["tradeDate"]]
    if len(set(trades)) != len(trades):
        raise DataNotFoundError("plusieurs lignes pour une même séance")
    prices = pd.to_numeric(df["settlPx"], errors="raise").astype(float)
    series = pd.Series(prices.to_numpy(), index=trades).sort_index()
    return series, payload.get("currency"), payload.get("uOM")


def fetch_window(today: date) -> tuple[list[dict], dict]:
    """Interroge chaque contrat une fois sur les HISTORY_DAYS derniers jours.

    Renvoie (lignes d'historique à stocker, détail par contrat). Pour chaque séance de la
    fenêtre, on retient les contrats suivis ce jour-là : Month-Ahead du mois suivant la séance,
    Cal suivis, GO des échéances postérieures à l'année de la séance.
    """
    start = today - timedelta(days=HISTORY_DAYS)
    sessions = [start + timedelta(days=i) for i in range((today - start).days)
                if (start + timedelta(days=i)).weekday() < 5]
    by_session = {s: contracts_for(s) for s in sessions}
    unique = sorted({c for cs in by_session.values() for c in cs}, key=lambda c: (c.marche, c.maturity))
    fetched = {}
    for c in unique:
        try:
            series, currency, unit = parse_history(call_api(c.params(start, today)), c)
            fetched[c] = {"series": series.dropna(), "devise": currency, "unite": unit, "error": None}
        except PermissionError:
            raise
        except Exception as e:  # une erreur sur un contrat n'empêche pas les autres
            fetched[c] = {"series": pd.Series(dtype=float), "devise": None, "unite": None, "error": str(e)}
        time.sleep(PAUSE)

    history = []
    for s, cs in by_session.items():
        for c in cs:
            f = fetched[c]
            if s in f["series"].index:
                history.append({**c.__dict__, "session": s, "settlement": float(f["series"][s]),
                                "devise": f["devise"], "unite": f["unite"]})
    return history, fetched


def report_rows(session: date, fetched: dict) -> tuple[list[dict], list[str]]:
    """Lignes du mail pour la séance cible (settlement et séance précédente) et anomalies."""
    rows, errors = [], []
    for c in contracts_for(session):
        f = fetched[c]
        row = {**c.__dict__, "session": session, "settlement": None, "prix_prec": None,
               "devise": f["devise"], "unite": f["unite"]}
        if f["error"]:
            errors.append(f"{c.marche} {c.contrat} : {f['error']}")
        elif session not in f["series"].index:
            before = [t for t in f["series"].index if t < session]
            hint = f" (dernière séance disponible : {max(before):%d/%m/%Y})" if before else ""
            errors.append(f"{c.marche} {c.contrat} : settlement du {session:%d/%m/%Y} non trouvé{hint}")
        else:
            previous = f["series"][f["series"].index < session]
            row.update(settlement=float(f["series"][session]),
                       prix_prec=float(previous.iloc[-1]) if len(previous) else None)
        rows.append(row)
    return rows, errors


# =============================================================================
# MISE EN FORME DE L'E-MAIL
# =============================================================================
def fr_number(x: float, decimals: int = 2) -> str:
    return f"{x:,.{decimals}f}".replace(",", " ").replace(".", ",")


def fr_date(d: date) -> str:
    return f"{JOURS_FR[d.weekday()]} {d:%d/%m/%Y}"


def unit_label(row: dict) -> str:
    """Unité publiée par EEX (devise / unité de mesure) ; rien n'est supposé."""
    if row["devise"] and row["unite"]:
        return f"{row['devise']}/{row['unite']}"
    return row["devise"] or row["unite"] or "unité n.d."


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


def build_report(rows: list[dict], errors: list[str], session: date) -> tuple[str, str, str]:
    """Renvoie (objet, html, texte) de l'e-mail de rapport."""
    titre = f"Settlements EEX – séance du {session:%d/%m/%Y}"
    subject = f"[INCOMPLET] {titre}" if errors else titre

    td = "padding:8px 12px;border-bottom:1px solid #e5e7eb;"
    th = "padding:8px 12px;text-align:left;background:#f3f4f6;border-bottom:2px solid #d1d5db;"
    sections_html, text_lines = [], []
    groups = [("Forwards", list(FORWARDS)), ("Garanties d'origine (futures EEX)", list(GUARANTEES_OF_ORIGIN))]
    for title, markets in groups:
        html_rows = []
        text_lines.append(f"\n{title}")
        for market in markets:
            m_rows = [r for r in rows if r["marche"] == market]
            for i, r in enumerate(m_rows):
                var_txt, var_col = variation(r["settlement"], r["prix_prec"])
                prix = fr_number(r["settlement"]) if r["settlement"] is not None else "n.d."
                unite = unit_label(r) if r["settlement"] is not None else ""
                market_cell = (f'<td rowspan="{len(m_rows)}" style="{td}font-weight:600;vertical-align:top;">'
                               f'{market}</td>') if i == 0 else ""
                html_rows.append(
                    f"<tr>{market_cell}"
                    f'<td style="{td}">{r["contrat"]}</td>'
                    f'<td style="{td}color:#555;">{r["echeance"]}</td>'
                    f'<td style="{td}text-align:right;font-weight:600;">{prix}</td>'
                    f'<td style="{td}color:#555;">{unite}</td>'
                    f'<td style="{td}text-align:right;color:{var_col};white-space:nowrap;">{var_txt}</td></tr>')
                text_lines.append(f"{market:<22} {r['contrat']:<12} {r['echeance']:<11} "
                                  f"{prix:>10} {unite:<10} {var_txt}")
        sections_html.append(
            f'<h3 style="margin:20px 0 8px 0;font-size:16px;">{title}</h3>'
            f'<table style="border-collapse:collapse;font-size:14px;min-width:560px;">'
            f'<tr><th style="{th}">Marché</th><th style="{th}">Contrat</th><th style="{th}">Échéance</th>'
            f'<th style="{th}text-align:right;">Settlement</th><th style="{th}">Unité</th>'
            f'<th style="{th}text-align:right;">Var. vs séance préc.</th></tr>'
            f'{"".join(html_rows)}</table>')

    warn_html = ""
    if errors:
        items = "".join(f"<li>{e}</li>" for e in errors)
        warn_html = (f'<div style="margin-top:20px;padding:12px 16px;background:#fff4e5;'
                     f'border-left:4px solid #f59e0b;"><b>Données manquantes :</b>'
                     f'<ul style="margin:8px 0 0 0;padding-left:20px;">{items}</ul></div>')

    html = f"""<html><body style="font-family:Arial,Helvetica,sans-serif;color:#111;margin:0;padding:16px;">
<h2 style="margin:0 0 4px 0;font-size:20px;">Settlements EEX</h2>
<p style="margin:0 0 4px 0;color:#555;">Séance du {fr_date(session)}</p>
{''.join(sections_html)}
{warn_html}
<p style="margin-top:24px;font-size:12px;color:#888;">Source : eex.com – données à titre informatif.
GO : l'échéance de janvier A couvre la production de mars à décembre de l'année A-1.</p>
</body></html>"""

    text = (f"Settlements EEX – séance du {fr_date(session)}\n"
            + "\n".join(text_lines)
            + ("\n\nDonnées manquantes :\n- " + "\n- ".join(errors) if errors else "")
            + "\n\nSource : eex.com")
    return subject, html, text


def build_not_found(errors: list[str], session: date, details: str | None = None) -> tuple[str, str, str]:
    """E-mail envoyé quand aucune donnée n'a pu être récupérée."""
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
    """Affiche la réponse brute d'EEX pour chaque contrat suivi (diagnostic)."""
    session = target_session()
    print(f"Séance cible : {fr_date(session)}")
    for c in contracts_for(session):
        print(f"\n===== {c.marche} / {c.contrat} ({c.short_code}, maturity {c.maturity}, {c.maturity_type}) =====")
        try:
            payload = call_api(c.params(session - timedelta(days=10), date.today()))
            if isinstance(payload, dict):
                print(f"clés : {list(payload)} | devise : {payload.get('currency')} | unité : {payload.get('uOM')}")
                print(f"header : {payload.get('header')}")
                for row in payload.get("data", [])[-4:]:
                    print("  ", row)
            else:
                print(f"réponse de type {type(payload).__name__} : {str(payload)[:300]}")
        except Exception as e:
            print(f"erreur : {e}")
        time.sleep(PAUSE)


def main() -> None:
    if EXPLORE:
        explore()
        return
    session = target_session()
    try:
        history, fetched = fetch_window(date.today())
        if history and not APERCU:
            path, added, total = archive.upsert(history)
            print(f"Historique : {path} ({added} lignes ajoutées ou mises à jour, {total} au total)")
        rows, errors = report_rows(session, fetched)
        if any(r["settlement"] is not None for r in rows):
            message = build_report(rows, errors, session)
        else:
            message = build_not_found(errors, session)
    except Exception as e:  # erreur imprévue pendant la collecte : on prévient par e-mail
        message = build_not_found([f"Erreur inattendue : {e}"], session, traceback.format_exc())
    try:
        deliver(*message)
    except (smtplib.SMTPException, OSError) as e:
        raise SystemExit(f"\nÉchec de l'envoi de l'e-mail via {SMTP_HOST}:{SMTP_PORT} → {e!r}\n"
                         "Vérifier SMTP_HOST, SMTP_PORT, SMTP_USER et SMTP_PASSWORD.")


if __name__ == "__main__":
    main()
