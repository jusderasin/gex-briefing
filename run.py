#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Briefing NQ automatique.
Pipeline : LevelBrief -> screenshots (Playwright) -> API Gemini -> HTML + PDF.
Lancé tous les jours par GitHub Actions (voir .github/workflows/briefing.yml).
"""

import os
import re
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
def capture(url: str) -> list[pathlib.Path]:
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

        browser.close()

    print(f"[capture] {len(paths)} captures prises sur {url}")
    return paths


# ----------------------------------------------------------------------------
# 2) GÉNÉRATION — Gemini lit les captures et reproduit le template
# ----------------------------------------------------------------------------
def build_prompt(template_html: str, target_date: datetime.date) -> str:
    return f"""Tu es un analyste options/GEX. À partir des CAPTURES D'ÉCRAN de LevelBrief
(données NDX) jointes, tu produis un briefing pre-market NQ pour {date_fr(target_date)}.

OBJECTIF
- Reproduire À L'IDENTIQUE le template HTML ci-dessous (structure, CSS, classes, mise
  en page, polices). Tu ne changes RIEN au design ni au CSS.
- Tu mets seulement à jour les DONNÉES en fonction de ce que tu lis sur les captures :
  date et statusbar, thèse (core thesis + callout), badges (régime gamma, expiry, etc.),
  microstructure, IV30 / skew, net GEX et GEX shift, conviction matrix (les 4 metrics),
  l'échelle de niveaux (le "ladder" : strikes, call/put walls, gamma flip/HVL, spot,
  max pain, hedging bands), la carte d'action des dealers (±1%), le GEX par expiration,
  la matrice de scénarios, et la date du disclaimer.

CONVENTIONS (comme dans le template)
- Niveaux exprimés en NDX (mentionner "ajouter le basis NQ" dans le disclaimer, déjà présent).
- Langue : français.
- Si une valeur n'est PAS lisible sur les captures, mets "—" (comme les indices globaux
  du template). N'INVENTE JAMAIS un chiffre que tu ne vois pas — c'est utilisé pour trader.
- Garde le disclaimer éducatif (ceci n'est pas un conseil en investissement).

SORTIE
- Renvoie UNIQUEMENT le HTML complet, commençant par <!DOCTYPE html> et finissant par
  </html>. AUCUN texte avant/après, AUCUN bloc markdown ```.

===== TEMPLATE DE RÉFÉRENCE (reproduire la structure exactement) =====
{template_html}
===== FIN DU TEMPLATE ====="""


def generate_html(shots: list[pathlib.Path], template_html: str,
                  target_date: datetime.date) -> str:
    # Lit GEMINI_API_KEY dans l'environnement
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    parts = []
    for s in shots:
        parts.append(types.Part.from_bytes(data=s.read_bytes(), mime_type="image/png"))
    parts.append(build_prompt(template_html, target_date))

    resp = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=24000,
            # On coupe le "thinking" pour garantir que tout le HTML sorte sans être tronqué
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    text = (resp.text or "").strip()

    # Sécurité : retire d'éventuels fences markdown
    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n```$", "", text).strip()

    if "<!DOCTYPE html>" not in text or "</html>" not in text:
        raise RuntimeError("Réponse Gemini inattendue ou tronquée. Début :\n"
                           + text[:300])
    return text


# ----------------------------------------------------------------------------
# 3) PDF — rendu du HTML en PDF via Chromium (sauts de page propres)
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

    shots = capture(LEVELBRIEF_URL)
    if not shots:
        raise RuntimeError("Aucune capture prise — vérifie l'URL LevelBrief.")

    html = generate_html(shots, template_html, target)
    html = inject_print_css(html)

    html_path = OUT_DIR / f"Briefing_NQ_{stamp}.html"
    pdf_path = OUT_DIR / f"Briefing_NQ_{stamp}.pdf"

    html_path.write_text(html, encoding="utf-8")
    print(f"[run] HTML écrit : {html_path}")

    try:
        to_pdf(html_path, pdf_path)
        print(f"[run] PDF écrit  : {pdf_path}")
    except Exception as e:
        print(f"[run] PDF échoué (HTML quand même OK) : {e}")


if __name__ == "__main__":
    main()
