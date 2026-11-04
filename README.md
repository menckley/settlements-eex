# settlements-eex

> **Notice détaillée** — chaque calcul, chaque paramètre et les dépendances entre les dépôts : [docs/NOTICE.md](docs/NOTICE.md)

Collecte quotidienne des settlements EEX (power France, gaz TTF / PEG, garanties d'origine
européennes), e-mail récapitulatif, et historique de tous les prix.

## Historique

Un seul fichier : **`data/settlements.csv`**, une ligne par séance et par contrat :

| colonne | contenu |
|---|---|
| session | date de la séance |
| marche, contrat, echeance | ex. `GO Solaire (EU)`, `Prod. 2026`, `janv. 2027` |
| commodity, area, product, short_code, maturity, maturity_type | identifiants EEX du contrat |
| settlement, devise, unite | prix et unité publiés par EEX |

À chaque lancement, chaque contrat est interrogé sur les 60 derniers jours et toutes les séances
trouvées sont ajoutées (ou mises à jour) : le premier lancement récupère tout ce qu'EEX publie
encore (environ 45 jours), une séance manquée est rattrapée le lendemain, et rien n'est jamais
supprimé. Le workflow committe le fichier chaque jour.

Lecture en Python :

```python
from archive import load_settlements
go = load_settlements(marche="GO Solaire (EU)", start="2026-10-01")
```

## Commandes locales

```bash
MODE=explorer python rapport_settlements.py   # réponses brutes d'EEX, rien n'est écrit
MODE=apercu   python rapport_settlements.py   # aperçu du mail (apercu_email.html), ni envoi ni stockage
python -m pytest                              # tests hors ligne (pip install pytest)
```

Règle : aucune donnée n'est inventée. Un contrat sans settlement à la séance est signalé dans
le mail (« Données manquantes ») et n'est pas stocké.
