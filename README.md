# settlements-eex

Collecte quotidienne des settlements EEX (power France, gaz TTF / PEG, garanties d'origine
européennes), envoi d'un e-mail récapitulatif et archive des séances.

## Archive

`data/settlements/session=AAAA-MM-JJ.csv` : une ligne par contrat trouvé lors de la séance
(colonnes : session, marche, contrat, echeance, commodity, area, product, short_code,
maturity, maturity_type, settlement, devise, unite). Le workflow committe ce dossier chaque jour.

```python
from archive import load_settlements
go = load_settlements(marche="GO Solaire (EU)")
```

## Commandes locales

```bash
MODE=explorer python rapport_settlements.py   # réponses brutes d'EEX, rien n'est écrit
MODE=apercu   python rapport_settlements.py   # aperçu du mail (apercu_email.html), ni envoi ni archive
python -m pytest                              # tests hors ligne (pip install pytest)
```

Règle : aucune donnée n'est inventée. Un contrat sans settlement à la séance est signalé dans
le mail (« Données manquantes ») et n'est pas archivé.
