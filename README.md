# TechBros — Digitales Schaufenster

Vollbild-Widget für ein Hochkant-Display (1080×1920), das die aktiven
Kleinanzeigen der TechBros-Bestandsliste als Endlos-Karussell zeigt.
Gedacht für die Einbindung in AbleSign als Website-Zone.

## Dateien

| Datei | Zweck |
|---|---|
| `index.html` | Das Widget. Enthält CSS, JavaScript und das Logo als Inline-SVG. |
| `ads.json` | Aktuelle Anzeigendaten, täglich vom Workflow erneuert. |
| `scrape_ads.py` | Liest die Bestandsliste und schreibt `ads.json`. Nur Standardbibliothek. |
| `.github/workflows/update-ads.yml` | Führt den Scraper täglich aus und committet das Ergebnis. |

## Einbindung in AbleSign

1. GitHub Pages aktivieren: *Settings → Pages → Branch `main`, Ordner `/root`*.
2. In AbleSign ein Layout in **Portrait 1080×1920** anlegen.
3. Zone auf Vollbild ziehen, Typ **Website**, die Pages-URL eintragen.
4. Zonen-Optionen: Auto-Refresh **aus**, Scrollbars **aus**, Zoom **100 %**.

Das Widget lädt `ads.json` beim Start und danach alle 30 Minuten selbst nach.
Der Auto-Refresh des Players ist deshalb nicht nötig und würde die
Karussell-Position zurücksetzen.

## Anpassen

Alle Stellschrauben stehen im `CONFIG`-Block oben im `<script>` von `index.html`:

| Feld | Bedeutung |
|---|---|
| `SLIDE_MS` | Anzeigedauer je Produkt in Millisekunden (Standard 7000). |
| `SHOW_ANKAUF` | `false` blendet die Ankauf-/Service-Anzeigen aus. |
| `SHUFFLE` | `true` mischt die Reihenfolge bei jedem Start. |
| `IMG_RULE` | Bildgröße von Kleinanzeigen (`$_57.AUTO` = 1448 px). |
| `DATA_URL` | Quelle der Live-Daten. `''` nutzt nur die eingebaute Liste. |
| `DATA_REFRESH_MIN` | Abstand des Nachladens in Minuten. |

Farben und Schriftgrößen liegen als CSS-Variablen unter `:root`.

## Ausfallsicherheit

`index.html` enthält eine eingebaute Anzeigenliste als Fallback. Ist `ads.json`
nicht erreichbar oder unplausibel (weniger als drei Einträge, fehlende Felder),
läuft das Display mit dieser Liste weiter statt leer zu werden. Der Scraper
seinerseits lässt eine vorhandene `ads.json` unangetastet, wenn ein Lauf
verdächtig wenige Anzeigen findet.

## Scraper von Hand starten

```bash
python3 scrape_ads.py --dry-run     # nur prüfen, nichts schreiben
python3 scrape_ads.py               # ads.json aktualisieren
```

## Lokale Vorschau

```bash
python3 -m http.server 8791
```

Dann `http://localhost:8791/` öffnen. Ein direkter Doppelklick auf die Datei
funktioniert nicht: `fetch` auf `ads.json` scheitert dann an der
Same-Origin-Regel und es läuft nur die Fallback-Liste.
