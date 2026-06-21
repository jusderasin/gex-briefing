# Briefing NQ automatique — 100 % gratuit

Chaque jour : screenshot de LevelBrief → Gemini lit les données → génère le
briefing NQ (HTML + PDF) → commit dans `briefings/`. Zéro manip, zéro coût.

## Pourquoi c'est gratuit

- **GitHub Actions** (screenshot + cron quotidien) : gratuit (illimité en repo public).
- **Google Gemini API** : offre gratuite permanente, sans carte bancaire. Les modèles
  Flash lisent les images et tu fais 1 briefing/jour → jamais à la limite.
- ⚠️ Sur le tier gratuit, Google peut utiliser tes prompts pour entraîner ses modèles.
  Ici tu n'envoies que des captures LevelBrief (rien de perso) → aucun souci.

## Mise en place (une seule fois)

1. **Récupère une clé Gemini gratuite** : va sur https://aistudio.google.com/apikey
   → "Create API key" (compte Google suffit, pas de CB).

2. **Crée un repo GitHub** et pousse ces fichiers dedans.

3. **Ajoute le secret** (Settings → Secrets and variables → Actions → New secret) :
   - `GEMINI_API_KEY` → la clé de l'étape 1. **(seul secret obligatoire)**

   > L'URL est déjà pré-réglée sur `https://levelbrief.com/gex` et le ticker sur **NDX**
   > (la page démarre sur SPX, le script clique sur NDX tout seul). Pour changer :
   > ajoute les secrets `LEVELBRIEF_URL` ou `LEVELBRIEF_TICKER`.

4. C'est tout. Le workflow tourne **tous les jours (lun–ven) à 12:00 UTC**.

## Tester tout de suite

Onglet **Actions** → *Briefing NQ quotidien* → **Run workflow**.
Le briefing apparaît dans `briefings/Briefing_NQ_AAAA-MM-JJ.html` (+ `.pdf`).

## Régler l'heure

Dans `.github/workflows/briefing.yml`, ligne `cron: "0 12 * * 1-5"`.
Format = `minute heure * * jours`, en **UTC**. Ex. pour 11h Paris l'été → `0 9 * * 1-5`.

## Réglages utiles (`run.py` en haut)

- `MODEL` → `gemini-2.5-flash` par défaut. Si Gemini lit mal les chiffres, garde Flash
  (le mieux du gratuit) et augmente la netteté des captures via `MAX_TILES`.
- `MAX_TILES` → nombre de captures (scroll) envoyées. Augmente si la page LevelBrief
  est longue et que des sections manquent.

## Si LevelBrief demande en fait une connexion

Le navigateur headless n'aura pas ta session. Génère un fichier de cookies une fois :

```bash
pip install playwright && playwright install chromium
python -c "from playwright.sync_api import sync_playwright as s; \
p=s().start(); b=p.chromium.launch(headless=False); c=b.new_context(); \
pg=c.new_page(); pg.goto('TON_URL_LEVELBRIEF'); input('Connecte-toi puis Entrée...'); \
c.storage_state(path='state.json'); b.close()"
```

Commit `state.json` (ou mets-le en secret) et définis `LEVELBRIEF_STORAGE_STATE=state.json`.

## Lancer en local (debug)

```bash
pip install -r requirements.txt
python -m playwright install chromium
export GEMINI_API_KEY=...
export LEVELBRIEF_URL="https://..."
python run.py
```
