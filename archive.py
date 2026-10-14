"""Archive des settlements : un fichier CSV par séance dans data/settlements/.

  data/settlements/session=2026-10-13.csv

Écriture : appelée par rapport_settlements.py après chaque collecte.
Lecture : load_settlements(), utilisable depuis un autre projet (copie locale du dépôt,
ou fichiers récupérés par l'API GitHub).
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent / "data" / "settlements"
COLUMNS = ["session", "marche", "contrat", "echeance", "commodity", "area", "product",
           "short_code", "maturity", "maturity_type", "settlement", "devise", "unite"]


def session_path(session: date, root: Path = ROOT) -> Path:
    return Path(root) / f"session={session.isoformat()}.csv"


def save_session(rows: list[dict], session: date, root: Path = ROOT) -> Path:
    """Écrit (ou réécrit) le fichier de la séance ; écriture atomique."""
    df = pd.DataFrame(rows)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"colonnes absentes pour l'archive : {missing}")
    df = df[COLUMNS].sort_values(["marche", "maturity"]).reset_index(drop=True)
    if df["settlement"].isna().any():
        raise ValueError("l'archive ne contient que des settlements trouvés")
    if (df["session"] != session).any():
        raise ValueError("lignes d'une autre séance")
    path = session_path(session, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path


def list_sessions(root: Path = ROOT) -> list[date]:
    return sorted(date.fromisoformat(p.stem.split("=", 1)[1]) for p in Path(root).glob("session=*.csv"))


def load_settlements(start=None, end=None, marche: str | None = None, contrat: str | None = None,
                     root: Path = ROOT) -> pd.DataFrame:
    """Settlements archivés sur [start, end], filtrés éventuellement par marché et contrat."""
    sessions = list_sessions(root)
    if start is not None:
        sessions = [s for s in sessions if s >= pd.Timestamp(start).date()]
    if end is not None:
        sessions = [s for s in sessions if s <= pd.Timestamp(end).date()]
    if not sessions:
        raise FileNotFoundError("aucune séance archivée sur la période demandée")
    df = pd.concat([pd.read_csv(session_path(s, root), dtype={"maturity": str}) for s in sessions],
                   ignore_index=True)
    df["session"] = pd.to_datetime(df["session"]).dt.date
    if marche is not None:
        df = df[df["marche"] == marche]
    if contrat is not None:
        df = df[df["contrat"] == contrat]
    return df.reset_index(drop=True)
