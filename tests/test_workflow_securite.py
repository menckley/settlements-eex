"""Garde-fou : le jeton du dépôt de données ne doit pas persister dans le checkout."""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "settlements.yml"


def test_checkout_donnees_sans_credentials_persistants():
    texte = WORKFLOW.read_text(encoding="utf-8")
    bloc = texte.split("repository: menckley/settlements-eex-data", 1)[1]
    bloc = bloc.split("- name:", 1)[0]
    assert "persist-credentials: false" in bloc


def test_jeton_fourni_uniquement_a_l_etape_de_push():
    texte = WORKFLOW.read_text(encoding="utf-8")
    etapes = texte.split("      - name:")
    avec_env = [e for e in etapes if "DATA_REPO_TOKEN: ${{" in e]
    assert len(avec_env) == 1
    assert "git push" in avec_env[0]
