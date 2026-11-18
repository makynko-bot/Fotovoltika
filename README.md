# ☀️ Moja fotovoltika – prehľad spotreby z GoodWe SEMS

Webová stránka s dennou spotrebou, výrobou a odberom zo siete pre GoodWe fotovoltiku.
Beží celá na GitHube zadarmo – žiadny server ani počítač doma netreba:

- **GitHub Actions** každých ~15 minút stiahne dáta zo **SEMS+** (rovnaké prihlásenie ako v aplikácii SEMS),
- história sa ukladá do vetvy `data` (`history.json`),
- **GitHub Pages** zobrazí stránku s grafmi.

Na stránke nájdeš:
- okamžitý výkon (výroba, spotreba, sieť, batéria),
- detail ľubovoľného dňa – priebeh výkonu a súčty v kWh,
- graf dennej spotreby (14 / 30 / 90 / 365 dní) rozdelený na vlastnú výrobu a odber zo siete,
- mesačný súhrn.

## Nastavenie (raz, ~5 minút)

1. **Zisti ID elektrárne.** Prihlás sa na <https://semsplus.goodwe.com> (alebo semsportal.com),
   otvor svoju elektráreň – ID je dlhý reťazec v adrese stránky
   (napr. `.../station/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
2. V repozitári na GitHube choď do **Settings → Secrets and variables → Actions → New repository secret**
   a pridaj tri tajomstvá:
   | Názov | Hodnota |
   |---|---|
   | `SEMS_ACCOUNT` | e-mail/login do SEMS |
   | `SEMS_PASSWORD` | heslo do SEMS |
   | `SEMS_STATION_ID` | ID elektrárne z kroku 1 |
3. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
4. **Actions → „Aktualizácia dát zo SEMS“ → Run workflow.** Po dobehnutí je stránka na
   `https://<tvoj-github-login>.github.io/<názov-repa>/`.

Odvtedy sa dáta aktualizujú samé. Prvé denné súčty budú úplné od nasledujúceho dňa.

> **Súkromie:** heslo je uložené len v GitHub Secrets a na stránku sa nikdy nedostane.
> Stránka (GitHub Pages) je však verejná pre každého, kto pozná adresu – zobrazuje čísla spotreby.
> GitHub Pages zo súkromného repozitára vyžaduje platený účet GitHub Pro.

## Ako sa počítajú denné súčty

SEMS+ API poskytuje okamžité výkony (kW). Denné kWh sa dopočítajú integráciou vzoriek
(každých ~15 min), takže sú to dobré odhady, nie úradné hodnoty z elektromera.
Ak striedač hlási denné počítadlá (napr. `proPvStatsToday` = dnešná výroba), použijú sa namiesto odhadu.
Zoznam počítadiel, ktoré tvoj striedač posiela, je v `history.json` v poli `counter_codes`.

Smer toku zo siete (odber vs. dodávka) sa určí automaticky z nočných hodnôt.

## Lokálne vyskúšanie

```bash
pip install requests
python demo.py                # vygeneruje ukážkové dáta do site/data/
cd site && python -m http.server 8000   # otvor http://localhost:8000
```

Skutočné dáta: `SEMS_ACCOUNT=... SEMS_PASSWORD=... SEMS_STATION_ID=... python fetch.py`
(zapíše do `data/history.json`).

## Súbory

- `fetch.py` – klient SEMS+ API a ukladanie histórie
- `site/index.html` – webová stránka (Chart.js)
- `.github/workflows/update.yml` – plánované sťahovanie a nasadenie na GitHub Pages
- `demo.py` – ukážkové dáta
