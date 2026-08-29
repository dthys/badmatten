# Badmatten-dashboard

Een kaal, alleen-lezen dashboard voor de vier badmatten, dat je partner op zijn
telefoon kan openen. Geen login, geen server, geen abonnement: GitHub haalt elk
uur de cijfers bij bol op en zet ze op een webpagina. Hij ziet **alleen** deze
vier artikelen — de rest van je winkel komt er niet in voor, want alles wat niet
bij deze EAN's hoort wordt weggegooid vóórdat er iets wordt opgeslagen.

| EAN | Kleur |
|---|---|
| 5430004400141 | Rood |
| 5430004400158 | Beige |
| 5430004400110 | Marine |
| 5430004400080 | Lichtgrijs |

## Hoe het in elkaar zit

```
elk uur:  GitHub Actions  →  bol API  →  data.json (versleuteld)  →  GitHub Pages
telefoon:  https://…github.io/badmatten/#s=JOUWCODE  →  ontsleutelt in de browser
```

Drie dingen die belangrijk zijn om te snappen:

**De pagina is openbaar, de cijfers niet.** GitHub Pages is voor iedereen
bereikbaar, dus `data.json` staat er versleuteld op (AES-256-GCM). De sleutel
staat achter het `#` in de link en gaat daardoor **nooit** naar GitHub mee —
browsers sturen dat deel van een adres niet in het verzoek. Wie de link niet
volledig heeft, ziet niets. Je partner klikt gewoon zijn bladwijzer aan; de
telefoon onthoudt de code na de eerste keer.

**De historie zit in data.json, niet bij bol.** Bol geeft via de API maar drie
maanden bestellingen terug (facturen twee jaar). Wat we ooit ophaalden bewaren
we daarom zelf, in dat ene bestand. Elke ronde haalt de workflow eerst de
gepubliceerde versie op, voegt de nieuwe dagen toe en publiceert opnieuw. Lukt
dat ophalen niet, dan stopt de workflow — liever geen update dan een lege
historie.

**Er wordt niets naar de repo gecommit.** Versleutelde bytes zijn elke ronde
volledig anders, ook als er inhoudelijk niets veranderde. Elk uur committen zou
de repo in een jaar naar een gigabyte laten groeien. De workflow publiceert
daarom rechtstreeks naar Pages.

## Eenmalig instellen

### 1. Repo aanmaken en deze map erin zetten

Maak op GitHub een nieuwe **publieke** repo, bijvoorbeeld `badmatten`. Publiek
is nodig voor gratis GitHub Pages; dat is niet erg, want de cijfers zijn
versleuteld en de code hier bevat niets van je eigen app.

```powershell
cd "C:\Users\d_thy\Desktop\Privé\AI Claude\Bol-dashboard-Borco"
git init
git add .
git commit -m "badmatten-dashboard"
git branch -M main
git remote add origin https://github.com/dthys/badmatten.git
git push -u origin main
```

### 2. Verzin een code

Dit wordt de sleutel van het dashboard. Neem iets lang en willekeurigs, geen
woord uit het woordenboek — hij hoeft nooit getypt te worden, alleen geplakt:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(18))"
```

Bewaar hem. Je hebt hem op drie plekken nodig: als secret, in het seed-commando
en in de link naar je partner.

### 3. Historie vullen vanaf je eigen dashboard

Bol geeft geen bestellingen ouder dan drie maanden. Je eigen database heeft ze
wel, dus die vullen we één keer over. Draai vanuit deze map:

```powershell
python seed_lokaal.py --sleutel "JOUWCODE"
```

Het script leest `%APPDATA%\Seloo\bol_data.db`, pakt er de vier badmatten uit,
schrijft `data.json` en print onderaan de **INSTELLINGEN**-regel (kostprijzen,
btw en je eigen voorraad — die komen dus rechtstreeks uit je eigen database).
Kopieer die hele regel.

```powershell
git add data.json
git commit -m "historie"
git push
```

### 4. Secrets zetten

GitHub → je repo → **Settings** → **Secrets and variables** → **Actions** →
*New repository secret*. Vijf stuks:

| Naam | Waarde |
|---|---|
| `BOL_CLIENT_ID` | je bol **retailer**-sleutel (staat in `config.json` van je app) |
| `BOL_CLIENT_SECRET` | het bijbehorende secret |
| `BOL_ADS_CLIENT_ID` | je bol **adverteerder**-sleutel (Instellingen → Advertenties in je app) |
| `BOL_ADS_CLIENT_SECRET` | het bijbehorende secret |
| `DATA_SLEUTEL` | de code uit stap 2 |
| `INSTELLINGEN` | de regel die `seed_lokaal.py` printte |

De adverteerder-sleutels zijn optioneel. Laat je ze weg, dan draait alles
gewoon door en blijven alleen de advertentiekosten op nul staan.

### 5. Pages aanzetten

**Settings** → **Pages** → bij *Source* kies je **GitHub Actions** (dus niet
"Deploy from a branch").

### 6. Eerste ronde draaien

**Actions** → *Cijfers verversen* → **Run workflow**. Duurt de eerste keer wat
langer (hij haalt dan ook de facturen tot twee jaar terug en de
advertentiehistorie op). Daarna gaat het vanzelf, elk uur.

### 7. De link doorsturen

```
https://dthys.github.io/badmatten/#s=JOUWCODE
```

Zeg erbij dat hij hem moet bewaren als bladwijzer, of — netter op een telefoon —
op het beginscherm zetten: Safari → deelknop → *Zet op beginscherm*; Chrome →
menu → *Toevoegen aan startscherm*. Hij opent dan als een app.

## Onderhoud

**Voorraad thuis en bij je fulfilmentpartner** houdt de app niet zelf bij (de
bol-API weet daar niets van). Pas het secret `INSTELLINGEN` aan als het
verandert: Settings → Secrets → `INSTELLINGEN` → *Update*, en pas de getallen in
`"voorraad"` aan. Bij de volgende ronde staat het in het dashboard. De voorraad
bij bol zelf komt wél automatisch binnen.

**Kostprijs veranderd?** Zelfde secret, veld `kostprijs`. Dat werkt met
terugwerkende kracht op de hele historie — bewust: zo blijft de winst per maand
onderling vergelijkbaar. Wil je dat niet, laat het me weten.

**Code veranderen** (bijvoorbeeld als de link ergens rondslingert): zet het
nieuwe `DATA_SLEUTEL`-secret, draai dan *Run workflow* met **opnieuw beginnen**
aangevinkt, en stuur de nieuwe link door. De historie wordt dan opnieuw
opgebouwd uit wat bol nog heeft (bestellingen 3 maanden, facturen 2 jaar); wil
je de volledige historie behouden, draai dan eerst `seed_lokaal.py` opnieuw met
de nieuwe code en commit die `data.json`.

**Iets kapot?** Actions → de laatste run openen. Elke stap logt wat hij deed.
Gaat één bron onderuit (bijvoorbeeld het advertentierapport), dan blijven de
andere cijfers gewoon staan en zegt het dashboard onderaan wat er misging.

## Hoe de winst berekend wordt

```
omzet excl. btw
  − commissie bol
  − fulfilment (pick & pack)
  − opslag bij bol
  − advertenties
  − inkoop
  − verlies op retours
= winst
```

- **Omzet, stuks en commissie** komen uit de bestellingen zelf, dus direct.
- **Fulfilment en opslag** komen van de maandfactuur van bol, per artikel. Die
  komt pas na afloop van de maand. Voor de lopende maand rekent het dashboard
  met het tarief per stuk uit de laatste drie afgerekende maanden, en zegt er
  duidelijk bij dat die maand nog niet gefactureerd is.
- **Advertenties** komen uit het dagrapport van bol, per artikel. Alleen wat bol
  aan deze vier artikelen toewijst; merkcampagnes die aan je hele winkel hangen
  tellen hier niet mee, want die zijn niet aan één artikel toe te rekenen.
- **Inkoop** is kostprijs × verkochte stuks.
- **Retours** tellen mee zodra bol ze verwerkt en goedgekeurd heeft: omzet en
  commissie gaan terug, en de mat komt terug op voorraad, tenzij bol hem als
  verloren of vernietigd afmeldt.

Kleine verschillen met je eigen dashboard zijn mogelijk: dat verdeelt ook de
kosten die bol niet aan een artikel hangt (verzamelregels, merkcampagnes) over
je hele assortiment. Dat kan hier niet, want dit dashboard kent alleen de vier
badmatten. Het verschil valt aan deze kant altijd de goede kant op: er staan
hier nooit kosten die niet echt van deze matten zijn.

## Wat er in deze map staat

| Bestand | Wat het doet |
|---|---|
| `index.html` | de webapp zelf — één bestand, geen externe scripts |
| `ververs/ververs.py` | haalt alles bij bol op en schrijft `data.json` |
| `ververs/bolapi.py` | de bol-client (alleen wat dit dashboard nodig heeft) |
| `ververs/rekenen.py` | de berekening hierboven |
| `ververs/kluis.py` | de versleuteling |
| `seed_lokaal.py` | eenmalig: historie uit je eigen database overzetten |
| `.github/workflows/ververs.yml` | het uurritme en het publiceren |
| `instellingen-voorbeeld.json` | hoe het secret `INSTELLINGEN` eruitziet |
