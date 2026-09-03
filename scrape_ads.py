#!/usr/bin/env python3
"""
Kleinanzeigen-Bestandsliste -> ads.json fuer das Digital-Signage-Widget.

Liest alle Seiten der oeffentlichen Bestandsliste eines Nutzers und schreibt
Titel, Preis und Aufmacherbild als JSON neben die HTML-Datei.

Nur Standardbibliothek, keine Abhaengigkeiten.

Aufruf:
    python3 scrape_ads.py                       # -> ./ads.json
    python3 scrape_ads.py --out /var/www/ads.json
    python3 scrape_ads.py --user-id 150382420
    python3 scrape_ads.py --dry-run             # nur pruefen, nichts schreiben

Exit-Codes: 0 = ads.json aktualisiert oder unveraendert, 1 = Fehler
(bestehende ads.json bleibt bei Fehlern unangetastet).
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER_ID = "150382420"
BASE = "https://www.kleinanzeigen.de/s-bestandsliste.html?userId={uid}"
MAX_PAGES = 20
DELAY_S = 2.0          # Pause zwischen Seitenabrufen
MIN_ADS = 3            # Untergrenze: darunter gilt der Lauf als fehlgeschlagen
TIMEOUT_S = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}

# --- Parser -----------------------------------------------------------------

RE_ARTICLE = re.compile(r'<article[^>]*class="[^"]*\baditem\b[^"]*"[^>]*>', re.S)
RE_ADID = re.compile(r'data-adid="(\d+)"')
RE_HREF = re.compile(r'data-href="([^"]+)"')
RE_LDJSON = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
RE_IMG_SRC = re.compile(r'<img[^>]+src="(https://img\.kleinanzeigen\.de/[^"]+)"')
RE_TITLE = re.compile(
    r'<h2[^>]*class="[^"]*text-module-begin[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>', re.S)
RE_PRICE = re.compile(
    r'class="[^"]*aditem-main--middle--price[^"]*"[^>]*>(.*?)</p>', re.S)
RE_LOC = re.compile(r'icon-pin-gray[^>]*></i>\s*([^<]+)')
RE_DATE = re.compile(r'icon-calendar-open[^>]*></i>\s*([^<]+)')
RE_TAG = re.compile(r"<[^>]+>")


def text(raw):
    """HTML-Fragment -> sauberer Text."""
    return re.sub(r"\s+", " ", html.unescape(RE_TAG.sub("", raw))).strip()


def strip_rule(url):
    """?rule=$_59.AUTO abschneiden - die Groesse waehlt das Widget."""
    return url.split("?")[0]


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def parse_page(page_html):
    """Ein Listing -> Liste von Anzeigen-Dicts."""
    ads = []
    # Auf <article class="aditem"> aufteilen, damit jeder Block genau eine Anzeige haelt.
    starts = [m.start() for m in RE_ARTICLE.finditer(page_html)]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(page_html)
        blk = page_html[start:end]

        m = RE_ADID.search(blk)
        if not m:
            continue
        ad_id = m.group(1)

        # Bild: bevorzugt aus dem JSON-LD-Block (dort steht die grosse Variante),
        # sonst aus dem <img src>.
        img = None
        ld = RE_LDJSON.search(blk)
        if ld:
            try:
                img = json.loads(ld.group(1).strip()).get("contentUrl")
            except (json.JSONDecodeError, AttributeError):
                img = None
        if not img:
            m = RE_IMG_SRC.search(blk)
            img = m.group(1) if m else None
        if not img:
            continue                      # ohne Aufmacherbild kein Slide

        m = RE_TITLE.search(blk)
        title = text(m.group(1)) if m else None
        if not title:
            continue

        m = RE_PRICE.search(blk)
        price = text(m.group(1)) if m else ""

        m = RE_LOC.search(blk)
        loc = text(m.group(1)) if m else ""
        m = RE_DATE.search(blk)
        date = text(m.group(1)) if m else ""
        m = RE_HREF.search(blk)
        href = m.group(1) if m else ""

        ads.append({
            "id": ad_id,
            "t": title,
            "p": price,
            "i": strip_rule(img),
            "loc": loc,
            "d": date,
            "url": "https://www.kleinanzeigen.de" + href if href else "",
        })
    return ads


def scrape(user_id):
    """Alle Seiten durchlaufen, Duplikate ueber die Anzeigen-ID abfangen."""
    base = BASE.format(uid=user_id)
    seen, ads = set(), []
    for page in range(1, MAX_PAGES + 1):
        url = base if page == 1 else base + "&pageNum=%d" % page
        try:
            page_html = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if page == 1:
                raise
            print("  Seite %d nicht abrufbar (%s) - Abbruch" % (page, e), file=sys.stderr)
            break

        found = parse_page(page_html)
        new = [a for a in found if a["id"] not in seen]
        for a in new:
            seen.add(a["id"])
        ads.extend(new)
        print("  Seite %d: %d Anzeigen (%d neu)" % (page, len(found), len(new)),
              file=sys.stderr)

        # Letzte Seite erreicht, sobald nichts Neues mehr dazukommt.
        if not new:
            break
        if page < MAX_PAGES:
            time.sleep(DELAY_S)
    return ads


def write_atomic(path, payload):
    """Erst in eine temporaere Datei, dann umbenennen - das Widget liest nie
    eine halb geschriebene ads.json."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user-id", default=USER_ID, help="Kleinanzeigen-userId")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "ads.json"), help="Zieldatei")
    ap.add_argument("--dry-run", action="store_true",
                    help="nur abrufen und pruefen, nichts schreiben")
    args = ap.parse_args()

    print("Lese Bestandsliste userId=%s" % args.user_id, file=sys.stderr)
    try:
        ads = scrape(args.user_id)
    except Exception as e:
        print("FEHLER beim Abruf: %s" % e, file=sys.stderr)
        print("Bestehende %s bleibt unveraendert." % args.out, file=sys.stderr)
        return 1

    if len(ads) < MIN_ADS:
        print("FEHLER: nur %d Anzeigen gefunden (Minimum %d). Das deutet auf eine "
              "Sperre oder ein geaendertes Seitenlayout hin - vorhandene Datei "
              "bleibt unveraendert." % (len(ads), MIN_ADS), file=sys.stderr)
        return 1

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": BASE.format(uid=args.user_id),
        "count": len(ads),
        "ads": ads,
    }

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:1500])
        print("\n(dry-run: nichts geschrieben) %d Anzeigen" % len(ads), file=sys.stderr)
        return 0

    # Unveraenderte Daten nicht neu schreiben - spart bei Git-Deployments
    # einen leeren Commit pro Lauf.
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("ads") == ads:
                print("Unveraendert: %d Anzeigen." % len(ads), file=sys.stderr)
                return 0
        except (OSError, json.JSONDecodeError):
            pass

    write_atomic(args.out, payload)
    print("Geschrieben: %s (%d Anzeigen)" % (args.out, len(ads)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
