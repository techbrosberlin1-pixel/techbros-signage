#!/usr/bin/env python3
"""
Kleinanzeigen-Bestandsliste -> ads.json fuer das Digital-Signage-Widget.

Liest alle Seiten der oeffentlichen Bestandsliste eines Nutzers und schreibt
Titel, Preis, Galeriebilder und Einstelldatum als JSON neben die HTML-Datei.

Zusaetzlich zum reinen Abruf:
  * Relative Datumsangaben ("Heute, 07:55") werden in ein absolutes Datum
    umgerechnet, damit das Widget das Alter einer Anzeige bestimmen kann.
  * Preissenkungen werden gegen die vorherige ads.json erkannt und als
    p_alt / p_seit hinterlegt.
  * Galeriebilder werden nur fuer neue Anzeigen von der Detailseite geholt;
    bekannte Anzeigen uebernehmen ihre Bilder aus der alten Datei.

Nur Standardbibliothek, keine Abhaengigkeiten.

Aufruf:
    python3 scrape_ads.py                     # -> ./ads.json
    python3 scrape_ads.py --out /var/www/ads.json
    python3 scrape_ads.py --max-images 5      # Bilder je Anzeige (Standard 3)
    python3 scrape_ads.py --refresh-images    # Detailseiten aller Anzeigen neu lesen
    python3 scrape_ads.py --dry-run           # nur pruefen, nichts schreiben

Exit-Codes: 0 = aktualisiert oder unveraendert, 1 = Fehler
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
from datetime import date, datetime, timedelta, timezone

USER_ID = "150382420"
BASE = "https://www.kleinanzeigen.de/s-bestandsliste.html?userId={uid}"
MAX_PAGES = 20
DELAY_S = 2.0          # Pause zwischen Listenseiten
DETAIL_DELAY_S = 1.2   # Pause zwischen Detailseiten
MIN_ADS = 3            # Untergrenze: darunter gilt der Lauf als fehlgeschlagen
TIMEOUT_S = 30
PREIS_HISTORIE_TAGE = 21   # so lange bleibt ein Alt-Preis hinterlegt

# Bilder werden mit denselben Kopfzeilen geprueft, die ein Browser sendet.
# Die Bild-CDN verhandelt ueber Accept: dieselbe URL liefert ohne Accept eine
# 200 und mit dem Accept eines Browsers eine 404. Eine Pruefung ohne diesen
# Kopf haette also genau die Bilder durchgewinkt, die auf dem Display fehlen.
IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}

# Groessenstufe fuer Galeriebilder. $_59 = 960 px Breite und die einzige
# Stufe, die zuverlaessig fuer jedes Bild erzeugt wird; $_57 waere schaerfer,
# fehlt aber bei einem Teil der Anzeigen.
RULE_STD = "$_59.AUTO"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}

# --- Regex ------------------------------------------------------------------

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

# Galeriebild der Detailseite: das src steht im selben <img>-Tag wie die
# Kennung viewad-image. Bilder der Seitenleiste (andere Anzeigen) tragen die
# Kennung nicht und fallen so heraus.
RE_GALERIE = re.compile(
    r'src="(https://img\.kleinanzeigen\.de/api/v1/prod-ads/images/[^"]+)"'
    r'[^>]{0,800}?id="viewad-image"')


def text(raw):
    """HTML-Fragment -> sauberer Text."""
    return re.sub(r"\s+", " ", html.unescape(RE_TAG.sub("", raw))).strip()


def bild_basis(url):
    """URL ohne ?rule=... - dient nur dem Erkennen von Dubletten."""
    return url.split("?")[0]


def bild_url(url):
    """Bild-URL auf die Standard-Groessenstufe bringen."""
    return bild_basis(url) + "?rule=" + RULE_STD


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


# --- Datum ------------------------------------------------------------------

def parse_datum(roh, heute=None):
    """'Heute, 07:55' / 'Gestern, 20:10' / '29.08.2026' -> 'YYYY-MM-DD'.

    Relative Angaben werden beim Abruf aufgeloest; sonst waere im Widget nicht
    mehr feststellbar, worauf sich 'Heute' bezog."""
    heute = heute or date.today()
    if not roh:
        return ""
    r = roh.strip().lower()
    if r.startswith("heute"):
        return heute.isoformat()
    if r.startswith("gestern"):
        return (heute - timedelta(days=1)).isoformat()
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", roh)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return ""
    return ""


# --- Preis ------------------------------------------------------------------

def preis_zahl(p):
    """'1.299 €' -> 1299.0, 'VB' -> None."""
    if not p:
        return None
    m = re.search(r"([\d.]+)\s*€", p.replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", ""))
    except ValueError:
        return None


def preis_historie(neu, alt, heute):
    """Traegt p_alt/p_seit ein, wenn der Preis gesunken ist.

    Preiserhoehungen werden bewusst nicht ausgewiesen. Ein bestehender
    Alt-Preis bleibt stehen, solange der Preis unveraendert und der Eintrag
    juenger als PREIS_HISTORIE_TAGE ist."""
    if not alt:
        return
    p_neu, p_alt = preis_zahl(neu["p"]), preis_zahl(alt.get("p"))

    if p_neu is not None and p_alt is not None and p_neu < p_alt:
        neu["p_alt"] = alt["p"]
        neu["p_seit"] = heute.isoformat()
        return

    # Preis unveraendert: vorhandenen Alt-Preis weiterreichen, bis er verfaellt.
    if neu["p"] == alt.get("p") and alt.get("p_alt"):
        seit = alt.get("p_seit", "")
        try:
            alter = (heute - date.fromisoformat(seit)).days
        except ValueError:
            return
        if alter <= PREIS_HISTORIE_TAGE:
            neu["p_alt"] = alt["p_alt"]
            neu["p_seit"] = seit


# --- Parser -----------------------------------------------------------------

def parse_page(page_html, heute):
    """Ein Listing -> Liste von Anzeigen-Dicts."""
    ads = []
    starts = [m.start() for m in RE_ARTICLE.finditer(page_html)]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(page_html)
        blk = page_html[start:end]

        m = RE_ADID.search(blk)
        if not m:
            continue
        ad_id = m.group(1)

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
        datum_roh = text(m.group(1)) if m else ""
        m = RE_HREF.search(blk)
        href = m.group(1) if m else ""

        ads.append({
            "id": ad_id,
            "t": title,
            "p": price,
            "i": bild_url(img),
            "imgs": [],
            "ts": parse_datum(datum_roh, heute),
            "loc": loc,
            "d": datum_roh,
            "url": "https://www.kleinanzeigen.de" + href if href else "",
        })
    return ads


def bild_erreichbar(url):
    """HEAD-Anfrage auf ein Bild.

    Einzelne Bilder sind bei Kleinanzeigen verwaist: sie stehen noch im
    Markup, liefern aber 404. Ungeprueft wandern sie ins Schaufenster und
    hinterlassen dort eine leere Flaeche. Geprueft wird nur einmal je neuem
    Bild, bekannte Anzeigen kommen aus dem Bestand."""
    try:
        req = urllib.request.Request(url, headers=IMG_HEADERS, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def galeriebilder(ad_url, limit):
    """Detailseite lesen und bis zu `limit` Galeriebilder zurueckgeben."""
    try:
        page = fetch(ad_url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print("    Detailseite nicht lesbar (%s)" % e, file=sys.stderr)
        return []
    bilder, gesehen, verworfen = [], set(), 0
    for roh in RE_GALERIE.findall(page):
        b = bild_basis(roh)
        if b in gesehen:
            continue
        gesehen.add(b)
        u = bild_url(roh)
        if not bild_erreichbar(u):
            verworfen += 1
            continue
        bilder.append(u)
        if len(bilder) >= limit:
            break
    if verworfen:
        print("    %d verwaiste Bilder uebersprungen" % verworfen, file=sys.stderr)
    return bilder


def scrape(user_id, heute):
    """Alle Listenseiten durchlaufen, Duplikate ueber die Anzeigen-ID abfangen."""
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

        found = parse_page(page_html, heute)
        new = [a for a in found if a["id"] not in seen]
        for a in new:
            seen.add(a["id"])
        ads.extend(new)
        print("  Seite %d: %d Anzeigen (%d neu)" % (page, len(found), len(new)),
              file=sys.stderr)
        if not new:
            break
        if page < MAX_PAGES:
            time.sleep(DELAY_S)
    return ads


def lade_alt(pfad):
    """Vorherige ads.json als {id: anzeige} - Grundlage fuer Preisvergleich
    und Bild-Zwischenspeicher."""
    try:
        with open(pfad, encoding="utf-8") as f:
            return {a["id"]: a for a in json.load(f).get("ads", []) if a.get("id")}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


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
    ap.add_argument("--max-images", type=int, default=3,
                    help="Galeriebilder je Anzeige (Standard 3, 1 = nur Aufmacher)")
    ap.add_argument("--refresh-images", action="store_true",
                    help="Detailseiten aller Anzeigen neu lesen statt nur der neuen")
    ap.add_argument("--dry-run", action="store_true",
                    help="nur abrufen und pruefen, nichts schreiben")
    args = ap.parse_args()

    heute = date.today()
    alt = lade_alt(args.out)

    print("Lese Bestandsliste userId=%s" % args.user_id, file=sys.stderr)
    try:
        ads = scrape(args.user_id, heute)
    except Exception as e:
        print("FEHLER beim Abruf: %s" % e, file=sys.stderr)
        print("Bestehende %s bleibt unveraendert." % args.out, file=sys.stderr)
        return 1

    if len(ads) < MIN_ADS:
        print("FEHLER: nur %d Anzeigen gefunden (Minimum %d). Das deutet auf eine "
              "Sperre oder ein geaendertes Seitenlayout hin - vorhandene Datei "
              "bleibt unveraendert." % (len(ads), MIN_ADS), file=sys.stderr)
        return 1

    # Preisvergleich gegen den vorherigen Lauf
    gesenkt = 0
    for a in ads:
        preis_historie(a, alt.get(a["id"]), heute)
        if a.get("p_seit") == heute.isoformat():
            gesenkt += 1

    # Galeriebilder: nur fuer unbekannte Anzeigen von der Detailseite holen.
    # Das haelt den taeglichen Lauf bei meist null zusaetzlichen Abrufen.
    if args.max_images > 1:
        offen = {a["id"] for a in ads if args.refresh_images
                 or not alt.get(a["id"], {}).get("imgs")}
        print("Galeriebilder: %d Anzeigen abzurufen, %d aus dem Bestand"
              % (len(offen), len(ads) - len(offen)), file=sys.stderr)
        for a in ads:
            vorher = alt.get(a["id"], {}).get("imgs") or []
            if a["id"] in offen and a["url"]:
                a["imgs"] = galeriebilder(a["url"], args.max_images)
                print("    %-52s %d Bilder" % (a["t"][:52], len(a["imgs"])),
                      file=sys.stderr)
                time.sleep(DETAIL_DELAY_S)
            else:
                a["imgs"] = vorher[:args.max_images]
            if not a["imgs"]:
                a["imgs"] = [a["i"]]      # Rueckfall auf das Aufmacherbild
    else:
        for a in ads:
            a["imgs"] = [a["i"]]

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": BASE.format(uid=args.user_id),
        "count": len(ads),
        "ads": ads,
    }

    if args.dry_run:
        print(json.dumps(payload["ads"][:2], ensure_ascii=False, indent=1))
        print("\n(dry-run: nichts geschrieben) %d Anzeigen, %d Preissenkungen"
              % (len(ads), gesenkt), file=sys.stderr)
        return 0

    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                if json.load(f).get("ads") == ads:
                    print("Unveraendert: %d Anzeigen." % len(ads), file=sys.stderr)
                    return 0
        except (OSError, json.JSONDecodeError):
            pass

    write_atomic(args.out, payload)
    print("Geschrieben: %s (%d Anzeigen, %d Preissenkungen)"
          % (args.out, len(ads), gesenkt), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
