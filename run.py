#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Briefing NQ automatique.
Pipeline : LevelBrief -> screenshots (Playwright) -> API Gemini -> HTML + PDF.
Lancé tous les jours par GitHub Actions (voir .github/workflows/briefing.yml).
"""

import os
import re
import sys
import json
import pathlib
import datetime
import zoneinfo

from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# CONFIG  (tu peux tout surcharger via des secrets/variables GitHub)
# ----------------------------------------------------------------------------
# >>> URL LevelBrief (déjà réglée sur la page GEX). Surcharge possible via secret. <<<
LEVELBRIEF_URL = os.environ.get("LEVELBRIEF_URL", "https://levelbrief.com/gex")

# Modèle Gemini (tier GRATUIT). Les modèles "Flash" sont gratuits et lisent les images.
# gemini-2.5-flash = bon défaut. gemini-2.5-flash-lite = encore plus de marge si besoin.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

MAX_TILES = 6          # nb max de captures (scroll) envoyées à Gemini
VIEWPORT_W = 1440
VIEWPORT_H = 900

# Onglet à sélectionner sur LevelBrief (SPX par défaut sur la page → on veut NDX pour le NQ)
TICKER = os.environ.get("LEVELBRIEF_TICKER", "NDX")

ROOT = pathlib.Path(__file__).parent
TEMPLATE_PATH = ROOT / "template_reference.html"
OUT_DIR = ROOT / "briefings"
SHOTS_DIR = ROOT / "_shots"

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def date_fr(d: datetime.date) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


# ----------------------------------------------------------------------------
# 1) CAPTURE — screenshots automatiques de LevelBrief (scroll par tuiles)
# ----------------------------------------------------------------------------
def capture(url: str) -> tuple[list[pathlib.Path], str]:
    SHOTS_DIR.mkdir(exist_ok=True)
    for old in SHOTS_DIR.glob("*.png"):
        old.unlink()

    paths: list[pathlib.Path] = []
    storage = os.environ.get("LEVELBRIEF_STORAGE_STATE")  # optionnel (cookies)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=2,  # texte net pour que Gemini lise bien les chiffres
            storage_state=storage if storage and os.path.exists(storage) else None,
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)  # laisse le dashboard se charger

        # Sélectionne le bon ticker (NDX). La page démarre sur SPX.
        try:
            page.get_by_text(TICKER, exact=True).first.click(timeout=8000)
            page.wait_for_timeout(4000)            # laisse les données du ticker se recharger
            page.wait_for_load_state("networkidle")
            print(f"[capture] ticker {TICKER} sélectionné")
        except Exception as e:
            print(f"[capture] clic {TICKER} impossible ({e}) — capture de l'état par défaut")

        total = page.evaluate("document.body.scrollHeight") or VIEWPORT_H
        y, i = 0, 0
        while y < total and i < MAX_TILES:
            page.evaluate(f"window.scrollTo(0,{y})")
            page.wait_for_timeout(800)
            shot = SHOTS_DIR / f"tile_{i}.png"
            page.screenshot(path=str(shot))
            paths.append(shot)
            y += VIEWPORT_H
            i += 1

        # Texte RÉEL de la page (chiffres caractère pour caractère, sans OCR)
        page_text = page.inner_text("body")

        browser.close()

    print(f"[capture] {len(paths)} captures + {len(page_text)} caractères de texte")
    return paths, page_text


# ----------------------------------------------------------------------------
# 2) GÉNÉRATION — en 2 étapes pour la fiabilité :
#    (A) Gemini EXTRAIT les chiffres des captures -> JSON
#    (B) Gemini REMPLIT le template avec ce JSON verrouillé (il ne peut plus inventer)
# ----------------------------------------------------------------------------

EXTRACTION_PROMPT = """Tu lis le dashboard GEX de LevelBrief (ticker NDX) sur les CAPTURES jointes.
Extrais les valeurs EXACTEMENT comme affichées, sans rien interpréter ni réarranger.

Lis SURTOUT, mot pour mot :
- L'encadré "KEY LEVELS" : SPOT, GAMMA FLIP, CALL WALL (+ son OI), PUT WALL (+ son OI),
  MAX PAIN, HEDGING BAND LO, HEDGING BAND HI. Chaque valeur va à SON niveau, ne les
  intervertis jamais (le call wall n'est pas le put wall).
- Le régime (positive/negative gamma) et le texte "Market Structure Read".
- IV / IV Skew (25-delta) si visible.
- Le tableau "TOP GEX STRIKES" : pour chaque ligne, strike, Net GEX ($B), Call OI, Put OI,
  Call Vol, Put Vol, Vol/OI.
- "Dealer Action Map" : $ sur +1% et sur -1%.
- "GEX by Expiry" : valeurs par échéance (0DTE/1W/1M+ ou hebdo/mensuel/etc.) si visibles.
- Les flèches "fresh flow" (↑ fresh calls / ↓ fresh puts) et leurs strikes si visibles.

Réponds en JSON STRICT, cette forme exacte (mets null si une valeur n'est pas lisible —
n'invente JAMAIS) :
{
 "spot": number|null,
 "gamma_flip": number|null,
 "call_wall": {"strike": number|null, "oi": number|null},
 "put_wall": {"strike": number|null, "oi": number|null},
 "max_pain": number|null,
 "hedging_band_lo": number|null,
 "hedging_band_hi": number|null,
 "regime": string|null,
 "net_gex_bn": number|null,
 "iv30": number|null,
 "iv_skew": number|null,
 "dealer_action_up_1pct_bn": number|null,
 "dealer_action_down_1pct_bn": number|null,
 "gex_by_expiry": [{"label": string, "value_bn": number}],
 "fresh_flow": [{"strike": number, "side": "call"|"put", "ratio": number|null}],
 "top_strikes": [{"strike": number, "net_gex_bn": number, "call_oi": number|null,
                  "put_oi": number|null, "call_vol": number|null, "put_vol": number|null,
                  "vol_oi": number|null}],
 "market_structure_read": string|null
}"""


def extract_data(shots: list[pathlib.Path], page_text: str, client) -> dict:
    parts = [types.Part.from_bytes(data=s.read_bytes(), mime_type="image/png")
             for s in shots]
    parts.append(
        "TEXTE EXACT DE LA PAGE LEVELBRIEF (source de vérité — les chiffres sont copiés "
        "caractère pour caractère depuis le site, utilise-les tels quels ; les captures "
        "ne servent qu'à lever une ambiguïté de mise en page) :\n\n"
        + page_text[:20000]
    )
    parts.append(EXTRACTION_PROMPT)

    resp = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=8000,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
        ),
    )
    data = json.loads(resp.text)
    print("[extract] niveaux lus :",
          {k: data.get(k) for k in ("spot", "gamma_flip", "call_wall",
                                    "put_wall", "max_pain")})
    return data


def build_prompt(data: dict, template_html: str, target_date: datetime.date) -> str:
    return f"""Tu produis un briefing pre-market NQ pour {date_fr(target_date)}.

DONNÉES VERROUILLÉES (lues sur LevelBrief NDX — c'est la SEULE source de vérité) :
{json.dumps(data, ensure_ascii=False, indent=2)}

RÈGLES ABSOLUES
- Reproduis le template HTML ci-dessous À L'IDENTIQUE : structure, CSS, classes, polices.
  Tu ne touches NI au design NI au CSS.
- Tu remplis avec les DONNÉES VERROUILLÉES ci-dessus, et RIEN d'autre. Chaque niveau va
  à son strike exact : call_wall.strike = Call Wall, put_wall.strike = Put Wall,
  max_pain = Max Pain, etc. N'INTERVERTIS JAMAIS. N'INVENTE AUCUN chiffre. Si une donnée
  est null, écris "—" à sa place (jamais une valeur inventée).
- IGNORE complètement les chiffres présents dans le template d'exemple : ce sont d'anciennes
  valeurs, elles ne doivent PAS réapparaître. Le "ladder" doit être trié par prix décroissant
  et refléter les vrais niveaux ci-dessus.
- Le SPOT sert de repère central du ladder. Au-dessus = zone de dampening, en-dessous du
  gamma flip = zone d'amplification.
- Thèse, badges, conviction matrix et matrice de scénarios : déduis-les du régime et de la
  position du spot vs gamma_flip/walls. Reste cohérent, sobre, factuel.
- Langue : français. Niveaux en NDX (le disclaimer mentionne déjà "ajouter le basis NQ").
- Garde le disclaimer éducatif (pas un conseil en investissement).

SORTIE : UNIQUEMENT le HTML complet, de <!DOCTYPE html> à </html>. Aucun texte autour,
aucun bloc markdown.

===== TEMPLATE (reproduire la structure exactement, ignorer ses chiffres) =====
{template_html}
===== FIN DU TEMPLATE ====="""


def build_html(data: dict, template_html: str, target_date: datetime.date,
               client) -> str:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[build_prompt(data, template_html, target_date)],
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=24000,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    text = (resp.text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n```$", "", text).strip()

    if "<!DOCTYPE html>" not in text or "</html>" not in text:
        raise RuntimeError("Réponse Gemini inattendue ou tronquée. Début :\n"
                           + text[:300])
    return text


def generate_html(shots: list[pathlib.Path], page_text: str, template_html: str,
                  target_date: datetime.date) -> tuple[str, dict]:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    data = extract_data(shots, page_text, client)    # étape A : lire les chiffres (texte exact)
    html = build_html(data, template_html, target_date, client)  # étape B : remplir
    return html, data


# ----------------------------------------------------------------------------
# 3bis) VÉRIFICATION — chaque niveau clé extrait doit apparaître dans le HTML final
# ----------------------------------------------------------------------------
def _present_in_html(value, html: str) -> bool:
    """Vrai si le nombre apparaît dans le HTML (tolère 30325 / 30,325 / 30 325)."""
    if value is None:
        return True  # rien à vérifier
    n = int(round(float(value)))
    variants = {
        str(n),                                   # 30325
        f"{n:,}",                                 # 30,325
        f"{n:,}".replace(",", " "),               # 30 325
        f"{n:,}".replace(",", "\u202f"),          # 30 325 (espace fine)
        f"{n:,}".replace(",", "."),               # 30.325
    }
    return any(v in html for v in variants)


def verify_levels(html: str, data: dict) -> list[str]:
    checks = {
        "spot": data.get("spot"),
        "gamma_flip": data.get("gamma_flip"),
        "call_wall": (data.get("call_wall") or {}).get("strike"),
        "put_wall": (data.get("put_wall") or {}).get("strike"),
        "max_pain": data.get("max_pain"),
        "hedging_band_lo": data.get("hedging_band_lo"),
        "hedging_band_hi": data.get("hedging_band_hi"),
    }
    failures = []
    for name, value in checks.items():
        ok = _present_in_html(value, html)
        print(f"[verify] {name:16} = {value!s:>10}  {'OK' if ok else '*** MANQUANT ***'}")
        if not ok:
            failures.append(f"{name}={value}")
    return failures


# ----------------------------------------------------------------------------
# 4) PDF — rendu du HTML en PDF via Chromium (sauts de page propres)
# ----------------------------------------------------------------------------
PRINT_CSS = """
<style id="print-rules">
@media print {
  body { background:#fff !important; padding:0 !important; }
  .deck { gap:0 !important; }
  .page { page-break-after: always; box-shadow:none !important; border-radius:0 !important; }
  .disclaimer { page-break-before: avoid; }
}
</style>
"""


def inject_print_css(html: str) -> str:
    if "</head>" in html:
        return html.replace("</head>", PRINT_CSS + "\n</head>", 1)
    return PRINT_CSS + html


def to_pdf(html_path: pathlib.Path, pdf_path: pathlib.Path) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # file:// + networkidle => les Google Fonts se chargent avant l'impression
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.pdf(
            path=str(pdf_path),
            landscape=True,
            print_background=True,
            format="A4",
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    template_html = TEMPLATE_PATH.read_text(encoding="utf-8")

    # Briefing daté du jour (heure de Paris)
    target = datetime.datetime.now(PARIS).date()
    stamp = target.isoformat()  # ex : 2026-06-20

    print(f"[run] Briefing NQ pour {date_fr(target)}")

    shots, page_text = capture(LEVELBRIEF_URL)
    if not shots:
        raise RuntimeError("Aucune capture prise — vérifie l'URL LevelBrief.")

    html, data = generate_html(shots, page_text, template_html, target)
    html = inject_print_css(html)

    html_path = OUT_DIR / f"Briefing_NQ_{stamp}.html"
    pdf_path = OUT_DIR / f"Briefing_NQ_{stamp}.pdf"

    html_path.write_text(html, encoding="utf-8")
    print(f"[run] HTML écrit : {html_path}")

    # index.html à la racine = dernier briefing (c'est ce que GitHub Pages affichera)
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    print("[run] index.html (dernier briefing) mis à jour pour le site")

    try:
        to_pdf(html_path, pdf_path)
        print(f"[run] PDF écrit  : {pdf_path}")
    except Exception as e:
        print(f"[run] PDF échoué (HTML quand même OK) : {e}")

    # Vérification finale : tout niveau clé absent du HTML => run en ROUGE
    failures = verify_levels(html, data)
    if failures:
        print("\n[run] ⚠️  INCOHÉRENCE détectée — le briefing est généré mais NE COLLE PAS "
              "aux données extraites :", ", ".join(failures))
        print("[run] Le fichier est quand même là pour inspection, mais à NE PAS utiliser tel quel.")
        sys.exit(1)
    print("\n[run] ✅ Tous les niveaux clés vérifiés — cohérents avec LevelBrief.")


if __name__ == "__main__":
    main()
