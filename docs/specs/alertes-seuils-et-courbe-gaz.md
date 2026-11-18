# Spécification — signaux, spreads et courbe gaz dans l'e-mail

## Objectif
Exploiter l'historique stocké (`archive.load_settlements`) pour ajouter à l'e-mail quotidien un bloc
« Signaux » : variations, position dans l'amplitude, alerte z-score, spreads, et un mini-graphique de la courbe
TTF (jour, J-7, J-30). Aucun nouvel appel EEX.

## Périmètre
- Option **désactivée par défaut** : variable d'environnement `SIGNAUX=1` (ou `oui`/`true`). Sans elle, l'e-mail,
  le stockage et les codes de sortie sont strictement inchangés. Le workflow n'est pas modifié : l'activation en
  production se fait en ajoutant `SIGNAUX` à l'environnement de l'étape d'envoi (décision du propriétaire).
- Contrats analysés : Month-Ahead et Cal+1 de Power Base France, Gaz TTF, Gaz PEG.
- Hors périmètre : CO2 (absent des données stockées), GO.
- Usage **interne uniquement** (licence EEX) : le bloc n'est ajouté qu'à l'e-mail privé existant.

## Conception
- Nouveau module `signaux.py`, fonctions pures sur un DataFrame au format de `archive.COLUMNS`.
- Le rang d'un contrat est relatif à la séance (Month : écart en mois, 1 = Month-Ahead ; Year : écart en années).
  Les variations sont calculées sur **un même contrat** (même maturité) suivi dans le temps, ce qui évite les
  sauts artificiels lors du roll du Month-Ahead.
- Par contrat : variation sur 5 et 20 séances (absolue et %), position dans l'amplitude des 365 derniers jours
  (0 % = bas, 100 % = haut, avec le nombre d'observations), z-score de la variation du jour =
  (Δ − moyenne) / écart-type des 60 variations précédentes ; alerte si |z| > 2,5. Exigences : au moins 20
  variations d'historique, écart-type > 0 ; sinon « n.d. » (rien n'est inventé).
- Spreads (séance cible, uniquement si les unités publiées sont identiques) : TTF – PEG (Month-Ahead et Cal+1) ;
  Cal+1 – Month-Ahead pour TTF (contango si > 0, backwardation si < 0) ; spark spread approché
  Power FR Cal+1 − TTF Cal+1 / η, avec `RENDEMENT_CCGT = 0,50` (hypothèse documentée). Pas de terme CO2 (donnée
  absente) : c'est un spark spread « hors carbone », indicatif.
- Graphique : PNG généré sans nouvelle dépendance (numpy + zlib), joint à l'e-mail en image inline (`cid:courbe`),
  les SVG et data-URI étant bloqués par les clients courants. Abscisse : M+1…M+6 puis Cal+1…Cal+3 ; légende en HTML.
- `rapport_settlements.py` : `build_report(..., signaux=None)`, `send_email`/`deliver(..., images=None)`. Toute
  erreur du calcul des signaux est journalisée et ignorée : elle n'empêche jamais l'envoi du rapport.
- En mode `apercu`, les lignes collectées sont fusionnées en mémoire avec l'historique stocké (rien n'est écrit).

## Plan
1. Spécification (ce fichier). 2. `signaux.py` + tests. 3. Branchement optionnel dans `rapport_settlements.py`
+ tests. 4. Mention dans le README.

## Tests
Z-score et seuil (série synthétique avec saut), « n.d. » si historique insuffisant, roll du Month-Ahead sans saut
artificiel, spreads (valeurs ; unités différentes → omis), PNG valide, e-mail identique sans option, e-mail avec
bloc et image inline avec option, échec des signaux sans effet sur l'envoi.

## Risques
- Historique court (≈45 jours au démarrage) : amplitude 52 semaines et z-score peu significatifs au début,
  signalé par le nombre d'observations et « n.d. ».
- Hypothèse de rendement et absence de CO2 : le spread est indicatif, pas un coût de production.
- Clients mail : image inline en `cid` ; HTML inchangé sans l'option.
