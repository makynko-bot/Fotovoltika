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

1. V repozitári na GitHube choď do **Settings → Secrets and variables → Actions → New repository secret**
   a pridaj dve tajomstvá:
   | Názov | Hodnota |
   |---|---|
   | `SEMS_ACCOUNT` | e-mail/login do SEMS |
   | `SEMS_PASSWORD` | heslo do SEMS |

   ID elektrárne netreba – skript si ho po prihlásení nájde sám (ak máš na účte viac elektrární,
   použije prvú; inú vyberieš tajomstvom `SEMS_STATION_ID`).
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Actions → „Aktualizácia dát zo SEMS“ → Run workflow.** Po dobehnutí je stránka na
   `https://<tvoj-github-login>.github.io/<názov-repa>/`.

Odvtedy sa dáta aktualizujú samé. Pri prvom behu sa stiahnu denné súčty za posledný rok
a podrobný priebeh výkonu sa postupne doplní za posledných ~60 dní.

> **Súkromie:** heslo je uložené len v GitHub Secrets a na stránku sa nikdy nedostane.
> Stránka (GitHub Pages) je však verejná pre každého, kto pozná adresu – zobrazuje čísla spotreby.
> GitHub Pages zo súkromného repozitára vyžaduje platený účet GitHub Pro.

## Okamžitá spotreba naživo (Cloudflare Worker, nepovinné)

Bez tohto kroku sa časť „Práve teraz“ obnovuje len pri každom behu GitHub Actions (~15+ min).
S Cloudflare Workerom sa obnovuje **každých 30 sekúnd** (SEMS sám dostáva dáta zo striedača
zhruba každých 5 minút, takže rýchlejšie to cez cloud nejde).

Worker je malý program bežiaci na serveroch Cloudflare – zadarmo do 100 000 požiadaviek denne.
Prihlási sa do SEMS a stránke vráti iba čísla, heslo zostane u neho.

1. Založ si bezplatný účet na <https://dash.cloudflare.com/sign-up>.
2. V ľavom menu **Compute (Workers) → Workers & Pages → Create → Start with Hello World**,
   pomenuj ho napr. `fotovoltika` a daj **Deploy**.
3. **Edit code** → celý obsah nahraď súborom [`worker/worker.js`](worker/worker.js) → **Deploy**.
4. Vo Workeri **Settings → Variables and Secrets → Add**, typ **Secret**:
   `SEMS_ACCOUNT`, `SEMS_PASSWORD` (rovnaké hodnoty ako na GitHube).
   Voliteľne typ *Text*: `ALLOWED_ORIGIN` = `https://<tvoj-github-login>.github.io`
   (aby sa naň nemohli pýtať iné stránky).
5. Otvor adresu Workera (napr. `https://fotovoltika.<meno>.workers.dev`) – musíš vidieť
   niečo ako `{"time":"…","pv":1.2,"load":0.8,…}`.
6. Na GitHube: **Settings → Secrets and variables → Actions → záložka Variables → New repository variable**
   `LIVE_URL` = adresa Workera. Potom spusti workflow ručne (alebo počkaj na najbližší beh).

Na stránke sa pri „Práve teraz“ objaví zelená bodka **naživo**.

## Odkiaľ sú dáta

- **Denné súčty (kWh)** – zo štatistík SEMS+ (výroba, spotreba, odber, dodávka, nabíjanie/vybíjanie batérie),
  rovnaké čísla ako v aplikácii SEMS. Ak by pre niektorý deň chýbali, dopočítajú sa z priebehu výkonu (označené ≈).
- **Priebeh dňa** – výkony z SEMS+ s krokom 5 minút, uchovávané 90 dní.
- **Okamžitý stav** – tok energie z SEMS+ (pri každom behu, alebo naživo cez Worker).

## Lokálne vyskúšanie

```bash
pip install requests
python demo.py                # vygeneruje ukážkové dáta do site/data/
cd site && python -m http.server 8000   # otvor http://localhost:8000
```

Skutočné dáta: `SEMS_ACCOUNT=... SEMS_PASSWORD=... python fetch.py`
(zapíše do `data/history.json`).

## Súbory

- `fetch.py` – klient SEMS+ API a ukladanie histórie
- `site/index.html` – webová stránka (Chart.js)
- `.github/workflows/update.yml` – plánované sťahovanie a nasadenie na GitHub Pages
- `worker/worker.js` – Cloudflare Worker pre okamžité hodnoty
- `demo.py` – ukážkové dáta
