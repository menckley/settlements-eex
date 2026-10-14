"""Historique des settlements EEX : un seul fichier, data/settlements.csv.

Une ligne par séance et par contrat. Colonnes :
  session, marche, contrat, echeance, commodity, area, product, short_code,
  maturity (AAAAMM), maturity_type, settlement, devise, unite

Écriture : upsert(), appelée par rapport_settlements.py à chaque lancement.
Lecture  : load_settlements(), depuis ce dépôt ou depuis un autre projet
           (copie locale du dépôt, ou fichier téléchargé depuis GitHub).
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

PATH = Path(__file__).resolve().parent / "data" / "settlements.csv"
COLUMNS = ["session", "marche", "contrat", "echeance", "commodity", "area", "product",
           "short_code", "maturity", "maturity_type", "settlement", "devise", "unite"]
KEY = ["session", "short_code", "maturity", "product"]


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"maturity": str})
    df["session"] = pd.to_datetime(df["session"]).dt.date
    return df


def upsert(rows: list[dict], path: Path = PATH) -> tuple[Path, int, int]:
    """Ajoute les lignes (ou remplace celles de même séance et même contrat).

    Les lignes déjà stockées et absentes de `rows` sont conservées : l'historique ne fait que
    s'allonger. Écriture atomique. Renvoie (chemin, lignes ajoutées ou modifiées, total).
    """
    new = pd.DataFrame(rows)
    missing = [c for c in COLUMNS if c not in new.columns]
    if missing:
        raise ValueError(f"colonnes absentes : {missing}")
    new = new[COLUMNS]
    if new["settlement"].isna().any():
        raise ValueError("l'historique ne contient que des settlements publiés")
    if new.duplicated(KEY).any():
        raise ValueError("doublons séance / contrat dans les nouvelles lignes")

    path = Path(path)
    old = _read(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates(KEY, keep="last")
    merged = merged.sort_values(["session", "marche", "maturity"]).reset_index(drop=True)
    if len(old):
        old_cmp = old.astype(str)
        merged_cmp = merged.astype(str)
        changed = len(merged_cmp.merge(old_cmp, how="left", on=COLUMNS, indicator=True)
                      .query("_merge == 'left_only'"))
    else:
        changed = len(merged)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    merged.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path, changed, len(merged)


def load_settlements(start=None, end=None, marche: str | None = None, contrat: str | None = None,
                     short_code: str | None = None, path: Path = PATH) -> pd.DataFrame:
    """Settlements stockés sur [start, end], filtrés éventuellement par marché, contrat ou code."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"historique absent : {path}")
    df = _read(path)
    if start is not None:
        df = df[df["session"] >= pd.Timestamp(start).date()]
    if end is not None:
        df = df[df["session"] <= pd.Timestamp(end).date()]
    for col, val in (("marche", marche), ("contrat", contrat), ("short_code", short_code)):
        if val is not None:
            df = df[df[col] == val]
    return df.reset_index(drop=True)
