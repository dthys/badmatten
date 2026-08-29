#!/usr/bin/env python3
"""
Haalt de cijfers van de vier badmatten bij bol op en schrijft data.json.

Draait in GitHub Actions (elk uur) en is ook los te draaien:

    BOL_CLIENT_ID=...  BOL_CLIENT_SECRET=...  DATA_SLEUTEL=...  \
    INSTELLINGEN="$(cat instellingen-voorbeeld.json)" python3 ververs/ververs.py

Wat het doet:

  1. data.json openen (versleuteld) - daar staat de historie in. Bol bewaart
     bestellingen maar drie maanden, dus wat we ooit ophaalden bewaren we zelf.
  2. de nieuwe dagen bij bol ophalen en samenvoegen
  3. alles doorrekenen (zie rekenen.py)
  4. data.json opnieuw versleuteld wegschrijven

Alles buiten de vier EAN's wordt weggegooid VOORDAT er iets wordt opgeslagen.
Er kan dus niets van de rest van de winkel in het bestand terechtkomen, ook
niet per ongeluk.
"""

import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bolapi
import kluis
import rekenen

HIER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HIER, "data.json")

# Bol levert hoogstens drie maanden bestelhistorie; daarbuiten is het aan ons.
MAX_HISTORIE_DAGEN = 90
ORDER_VENSTER = int(os.environ.get("ORDER_DAGEN", "10"))
ADS_VENSTER = int(os.environ.get("ADS_DAGEN", "45"))
ADS_BLOK = 35
FACTUUR_MAANDEN = int(os.environ.get("FACTUUR_MAANDEN", "3"))
MAX_DETAILS = int(os.environ.get("MAX_ORDER_DETAILS", "300"))


def log(bericht):
    print(f"{datetime.now():%H:%M:%S}  {bericht}", flush=True)


# ---------------------------------------------------------------- hulpjes

def _f(waarde):
    if isinstance(waarde, dict):
        for k in ("amount", "value", "totalAmount"):
            if k in waarde:
                return _f(waarde[k])
        return 0.0
    if waarde in (None, ""):
        return 0.0
    try:
        return float(str(waarde).replace(",", "."))
    except ValueError:
        return 0.0


def _s(bron, *namen):
    for n in namen:
        w = (bron or {}).get(n)
        if w not in (None, ""):
            return str(w)
    return ""


def _ci(bron, *namen):
    if not isinstance(bron, dict):
        return None
    laag = {str(k).lower(): v for k, v in bron.items()}
    for n in namen:
        if n.lower() in laag:
            return laag[n.lower()]
    return None


def _uitpakken(waarde):
    """Bol verpakt scalairen als {"value": x}, {"amount": x} of [{"value": x}]."""
    if isinstance(waarde, list):
        for el in waarde:
            uit = _uitpakken(el)
            if uit not in (None, ""):
                return uit
        return None
    if isinstance(waarde, dict):
        for k in ("value", "amount", "Value"):
            if k in waarde:
                return _uitpakken(waarde[k])
        return None
    return waarde


def _dagen(van, tot):
    d = van
    while d <= tot:
        yield d
        d += timedelta(days=1)


def _maandvenster(vandaag, maanden_terug):
    jaar, maand = vandaag.year, vandaag.month - maanden_terug
    while maand <= 0:
        maand += 12
        jaar -= 1
    eerste = date(jaar, maand, 1)
    if (jaar, maand) == (vandaag.year, vandaag.month):
        return eerste, vandaag
    volgende = date(jaar + (maand == 12), 1 if maand == 12 else maand + 1, 1)
    return eerste, volgende - timedelta(days=1)


# ------------------------------------------------------------- bestellingen

def haal_orders(client, ruw, eans, vanaf):
    """
    Bestellingen worden per KALENDERDAG opgehaald met latest-change-date. Dat is
    de enige manier waarop bol je door de historie laat lopen; zonder die filter
    krijg je alleen de recente pagina's.
    """
    vandaag = date.today()
    regels = ruw.setdefault("orderregels", {})
    orders = ruw.setdefault("orders", {})
    nieuw = 0
    dagen = list(_dagen(vanaf, vandaag))
    log(f"Bestellingen ophalen: {vanaf} t/m {vandaag} ({len(dagen)} dagen)")

    def verwerk(order):
        nonlocal nieuw
        oid = _s(order, "orderId")
        geplaatst = _s(order, "orderPlacedDateTime")
        dag = geplaatst[:10]
        raakt_ons = False
        for item in order.get("orderItems") or []:
            product = item.get("product") or {}
            ean = _s(product, "ean") or _s(item, "ean")
            if ean not in eans:
                continue
            raakt_ons = True
            oii = _s(item, "orderItemId")
            bestaand = regels.get(oii) or {}
            regels[oii] = {
                "oid": oid,
                "dag": dag or bestaand.get("dag"),
                "tijd": geplaatst or bestaand.get("tijd"),
                "ean": ean,
                "titel": _s(product, "title") or bestaand.get("titel", ""),
                "aantal": int(_f(item.get("quantity"))),
                "verzonden": int(_f(item.get("quantityShipped"))),
                "geannuleerd": int(_f(item.get("quantityCancelled"))),
                "prijs": _f(item.get("unitPrice")),
                "commissie": _f(item.get("commission")),
                "ff": _s(item.get("fulfilment") or {}, "method"),
                "land": bestaand.get("land", ""),
            }
            if oii not in bestaand:
                nieuw += 1
        if raakt_ons:
            verzend = order.get("shipmentDetails") or {}
            rij = orders.setdefault(oid, {})
            rij["dag"] = dag or rij.get("dag")
            if _s(verzend, "countryCode"):
                rij["land"] = _s(verzend, "countryCode")

    for i, dag in enumerate(dagen, 1):
        for pagina in range(1, 40):
            lijst = client.orders_pagina(pagina=pagina, status="ALL",
                                         fulfilment="ALL",
                                         laatste_wijziging=dag.isoformat())
            for order in lijst:
                verwerk(order)
            if len(lijst) < 50:
                break
        if i % 10 == 0 or i == len(dagen):
            log(f"  ... {i}/{len(dagen)} dagen")

    # En wat er nu openstaat, ongeacht wijzigingsdatum.
    for pagina in range(1, 40):
        lijst = client.orders_pagina(pagina=pagina, status="OPEN", fulfilment="ALL")
        for order in lijst:
            verwerk(order)
        if len(lijst) < 50:
            break

    log(f"Bestellingen klaar ({len(regels)} regels bewaard, {nieuw} nieuw)")
    return nieuw


def haal_orderdetails(client, ruw, eans):
    """
    Het land van de klant staat alleen in de DETAILS van een bestelling, niet in
    de lijst. We halen dat na voor bestellingen waar het nog ontbreekt, met een
    plafond per ronde zodat een inhaalslag nooit de hele run opsnoept.
    """
    orders = ruw.setdefault("orders", {})
    regels = ruw.get("orderregels", {})
    ontbreekt = [oid for oid, o in orders.items() if not o.get("land")]
    # Nieuwste eerst: die staan bovenaan in de lijst en zijn het interessantst.
    ontbreekt.sort(key=lambda oid: (orders[oid].get("dag") or ""), reverse=True)
    ontbreekt = ontbreekt[:MAX_DETAILS]
    if not ontbreekt:
        return 0
    log(f"Landgegevens ophalen voor {len(ontbreekt)} bestellingen")
    gehaald = 0
    for oid in ontbreekt:
        try:
            detail = client.order(oid)
        except bolapi.BolFout as e:
            if e.status in (401, 403):
                raise
            continue
        if not detail:
            continue
        land = _s(detail.get("shipmentDetails") or {}, "countryCode")
        if land:
            orders[oid]["land"] = land
            for r in regels.values():
                if r.get("oid") == oid:
                    r["land"] = land
            gehaald += 1
    log(f"  {gehaald} landen ingevuld")
    return gehaald


# ------------------------------------------------------------------ retours

def haal_retours(client, ruw, eans, volledig=False):
    retours = ruw.setdefault("retours", {})
    gezien = 0
    for methode in ("FBR", "FBB"):
        for afgehandeld in (False, True):
            max_paginas = 40 if (volledig or not afgehandeld) else 12
            for pagina in range(1, max_paginas + 1):
                lijst = client.retours_pagina(pagina=pagina,
                                              afgehandeld=afgehandeld,
                                              fulfilment=methode)
                for ret in lijst:
                    gezien += _bewaar_retour(retours, ret, eans)
                if len(lijst) < 50:
                    break
    log(f"Retouren klaar ({len(retours)} bewaard)")
    return gezien


def _bewaar_retour(retours, ret, eans):
    aangemeld = _s(ret, "registrationDateTime")
    rid = _s(ret, "returnId")
    raak = 0
    for item in ret.get("returnItems") or []:
        ean = _s(item, "ean") or _s(item.get("product") or {}, "ean")
        if ean not in eans:
            continue
        rma = _s(item, "rmaId") or f"{rid}-{ean}"
        verwerkt = _s(item, "processingDateTime", "handlingDateTime")
        retours[rma] = {
            "rid": rid,
            "oid": _s(item, "orderId") or _s(ret, "orderId"),
            "ean": ean,
            "aangemeld": aangemeld,
            "verwacht": int(_f(item.get("expectedQuantity"))),
            "terug": int(_f(item.get("returnedQuantity"))),
            "reden": _s(item.get("returnReason") or {}, "mainReason"),
            "detail": _s(item.get("returnReason") or {}, "detailedReason"),
            "toelichting": _s(item.get("returnReason") or {}, "customerComments"),
            "afgehandeld": bool(item.get("handled")),
            "uitkomst": _s(item, "handlingResult"),
            "resultaat": _s(item, "processingResult"),
            "verwerkt": verwerkt or (aangemeld if item.get("handled") else ""),
        }
        raak += 1
    return raak


# ----------------------------------------------------------------- facturen

TYPES = ("TURNOVER", "COMMISSION", "PICK_PACK", "STORAGE", "SHIPMENT_LABEL",
         "CORRECTION_TURNOVER")


def haal_facturen(client, ruw, eans, maanden):
    """
    De factuurspecificatie is de enige plek waar staat wat bol ECHT rekende:
    commissie, pick&pack en opslag per artikel. Gaat twee jaar terug.
    """
    factuur = ruw.setdefault("factuur", {})
    # Het AANTAL stuks dat bol factureerde. Dat is de enige betrouwbare noemer
    # voor "wat kost pick&pack per stuk": onze eigen bestelhistorie kan een
    # maand maar half kennen (bol geeft er drie terug), en dan zou een volle
    # maandfactuur gedeeld worden door een halve maand verkopen - een tarief
    # dat er zomaar een euro naast zit.
    aantallen = ruw.setdefault("factuur_aantal", {})
    vandaag = date.today()
    regels = 0
    for terug in range(maanden):
        van, tot = _maandvenster(vandaag, terug)
        try:
            lijst = client.facturen(van.isoformat(), tot.isoformat())
        except bolapi.BolFout as e:
            log(f"  facturen {van:%Y-%m} niet beschikbaar ({e.status})")
            continue
        for inv in lijst or []:
            if not isinstance(inv, dict):
                continue
            inv_id = _s(inv, "invoiceId", "id", "invoiceNumber")
            if not inv_id:
                continue
            # De VERKOOPmaand komt uit de factuurperiode, niet uit de
            # uitgiftedatum: een factuur over juni wordt in juli uitgegeven.
            periode = _ci(inv, "invoicePeriod") or {}
            maand = _epoch_maand(_ci(periode, "startDate")) or \
                _epoch_maand(_ci(inv, "issueDate", "invoiceDate", "date"))
            if not maand:
                continue
            # De maand schoonvegen voordat we hem opnieuw inlezen, zodat een
            # gecorrigeerde factuur geen dubbeltelling oplevert.
            for sleutel in [k for k in factuur if k.startswith(maand + "|")]:
                factuur.pop(sleutel, None)
            for sleutel in [k for k in aantallen if k.startswith(maand + "|")]:
                aantallen.pop(sleutel, None)
            for pagina in range(1, 21):
                try:
                    spec = client.factuurspecificatie(inv_id, pagina)
                except bolapi.BolFout as e:
                    log(f"  specificatie {inv_id} p{pagina} mislukt ({e.status})")
                    break
                rijen = _spec_regels(spec)
                if not rijen:
                    break
                for r in rijen:
                    if r["ean"] not in eans or r["soort"] not in TYPES:
                        continue
                    sleutel = f"{maand}|{r['ean']}|{r['soort']}"
                    factuur[sleutel] = round(factuur.get(sleutel, 0.0) + r["bedrag"], 4)
                    if r["soort"] in ("TURNOVER", "CORRECTION_TURNOVER"):
                        a = f"{maand}|{r['ean']}"
                        teken = 1 if r["soort"] == "TURNOVER" else -1
                        aantallen[a] = round(aantallen.get(a, 0.0) + teken * r["aantal"], 3)
                    regels += 1
                if len(rijen) < 100:
                    break
        log(f"  facturen {van:%Y-%m} verwerkt ({regels} regels tot nu toe)")
    log(f"Facturen klaar ({len(factuur)} posten)")
    return regels


def _epoch_maand(waarde):
    waarde = _uitpakken(waarde)
    if waarde in (None, ""):
        return ""
    tekst = str(waarde)
    if tekst.isdigit() and len(tekst) >= 12:      # milliseconden sinds 1970
        return datetime.utcfromtimestamp(int(tekst) / 1000.0).strftime("%Y-%m")
    return tekst[:7] if len(tekst) >= 7 else ""


def _spec_regels(spec):
    """
    De specificatie platslaan tot regels met soort, EAN en bedrag.

    Bol codeert het meeste in de regel-id:
        A00075J48J#7445931066009#TURNOVER   -> bestelling, EAN, soort
    Het soort is altijd het laatste stuk.
    """
    import re
    kandidaten = []
    if isinstance(spec, list):
        kandidaten = spec
    elif isinstance(spec, dict):
        for sleutel in ("invoiceSpecification", "specification", "lines",
                        "items", "invoiceSpecificationLines"):
            waarde = _ci(spec, sleutel)
            if isinstance(waarde, list):
                kandidaten = waarde
                break

    uit = []
    for regel in kandidaten:
        if not isinstance(regel, dict):
            continue
        stukken = [p for p in _s(regel, "id").split("#") if p]
        soort = stukken[-1] if stukken else ""
        ean = next((p for p in stukken[:-1] if re.fullmatch(r"\d{8,14}", p)), "")
        if not soort:
            ref = _s(regel, "invoiceLineRef")
            soort = ref.rsplit("#", 1)[-1] if "#" in ref else ""

        item = _ci(regel, "item") or {}
        eigenschappen = {}
        ruwe = _ci(item, "AdditionalItemProperty")
        for entry in (ruwe if isinstance(ruwe, list) else [ruwe] if ruwe else []):
            if isinstance(entry, dict):
                naam = _uitpakken(_ci(entry, "Name", "name"))
                waarde = _uitpakken(_ci(entry, "Value", "value"))
                if naam is not None and waarde is not None:
                    eigenschappen[str(naam).strip().lower()] = str(waarde).strip()
        ean = eigenschappen.get("ean") or ean

        bedrag = 0.0
        for sleutel in ("lineExtensionAmount", "amount", "netAmount",
                        "totalAmount", "grossAmount"):
            bedrag = _f(_uitpakken(_ci(regel, sleutel)))
            if bedrag:
                break
        aantal = _f(_uitpakken(_ci(regel, "invoicedQuantity", "quantity")))
        uit.append({"soort": soort or "UNKNOWN", "ean": ean, "bedrag": bedrag,
                    "aantal": abs(aantal)})
    return uit


# ------------------------------------------------------------- advertenties

ALIAS = {
    "dag": ("date", "day", "reportdate", "eventdate", "periodstartdate"),
    "ean": ("ean", "productean", "eannumber"),
    "kosten": ("cost", "spend", "totalcost"),
    "vertoningen": ("impressions", "totalimpressions"),
    "kliks": ("clicks", "totalclicks"),
}


def _kolom(rij, veld):
    for alias in ALIAS[veld]:
        if alias in rij and rij[alias] not in (None, ""):
            return rij[alias]
    return ""


def _getal(waarde, geheel=False):
    """
    Een getal uit een CSV lezen, ook als het Europees genoteerd is.

    Bij BEDRAGEN wordt '12.500' NIET als twaalfduizend gelezen: dat is
    waarschijnlijk twaalf euro vijftig, en van een bedrag duizend keer te veel
    maken is precies de fout die niemand op tijd ziet. Bij tellingen
    (vertoningen, kliks) kan een punt nooit een decimaalteken zijn.
    """
    if waarde in (None, ""):
        return 0
    if isinstance(waarde, (int, float)):
        return int(waarde) if geheel else float(waarde)
    tekst = str(waarde).strip().replace("€", "").replace(" ", "")
    if "," in tekst and "." in tekst:
        tekst = tekst.replace(".", "").replace(",", ".")
    elif "," in tekst:
        tekst = tekst.replace(",", ".")
    elif geheel:
        tekst = tekst.replace(".", "")
    try:
        return int(float(tekst)) if geheel else float(tekst)
    except ValueError:
        return 0


def haal_ads(ads_client, ruw, eans, dagen_terug):
    """
    Advertentiekosten per dag per EAN uit het AD_PERFORMANCE-bulkrapport.

    Bol laat de HUIDIGE dag niet in een rapport toe (die is nog niet
    afgerekend), dus we ankeren op gisteren en halen het lopende dagtotaal
    apart op. Rapporten mogen hoogstens ~35 dagen beslaan.
    """
    ads = ruw.setdefault("ads", {})
    algemeen = ruw.setdefault("adsalg", {})
    vandaag = date.today()
    laatste = vandaag - timedelta(days=1)

    blokken, d = [], 0
    while d < dagen_terug:
        lengte = min(ADS_BLOK, dagen_terug - d)
        blokken.append((laatste - timedelta(days=d + lengte - 1),
                        laatste - timedelta(days=d)))
        d += lengte

    totaal = 0.0
    for van, tot in blokken:
        try:
            rijen = ads_client.bulkrapport(van.isoformat(), tot.isoformat())
        except bolapi.BolFout as e:
            if e.status in (401, 403):
                log(f"Advertenties: geen toegang ({e.status}). "
                    f"Controleer de adverteerder-sleutels.")
                return totaal
            log(f"  advertentierapport {van}..{tot} overgeslagen: {e}")
            continue
        # De dagen in dit blok eerst schoonvegen: een dag die bol later
        # bijstelt mag niet dubbel blijven staan.
        for dag in _dagen(van, tot):
            for ean in eans:
                ads.pop(f"{dag.isoformat()}|{ean}", None)
        import re as _re
        for ruwe in rijen or []:
            rij = {_re.sub(r"[^a-z0-9]", "", str(k or "").lower()): v
                   for k, v in ruwe.items()}
            dag = str(_kolom(rij, "dag") or "")[:10]
            ean = str(_kolom(rij, "ean") or "").strip()
            if not dag or ean not in eans:
                continue
            sleutel = f"{dag}|{ean}"
            eerder = ads.get(sleutel) or [0.0, 0, 0]
            ads[sleutel] = [
                round(eerder[0] + _getal(_kolom(rij, "kosten")), 4),
                eerder[1] + _getal(_kolom(rij, "vertoningen"), geheel=True),
                eerder[2] + _getal(_kolom(rij, "kliks"), geheel=True),
            ]
            totaal += _getal(_kolom(rij, "kosten"))
        log(f"  advertenties t/m {tot} verwerkt")

    # Vandaag: het lopende totaal van de hele winkel, alleen ter informatie.
    try:
        vd = vandaag.isoformat()
        data = ads_client.dagtotaal(vd, vd) or {}
        kosten = _f(data.get("cost"))
        if kosten:
            algemeen[vd] = round(kosten, 2)
    except bolapi.BolFout:
        pass

    log(f"Advertenties klaar ({len(ads)} dagregels)")
    return totaal


# ------------------------------------------------------------------ voorraad

def haal_voorraad(client, ruw, eans):
    voorraad = ruw.setdefault("voorraad", {})
    log_ = ruw.setdefault("voorraadlog", {})
    vandaag = date.today().isoformat()
    gevonden = 0
    for pagina in range(1, 60):
        lijst = client.voorraad_pagina(pagina)
        for rij in lijst:
            ean = _s(rij, "ean")
            if ean not in eans:
                continue
            voorraad[ean] = {
                "bol": int(_f(rij.get("regularStock"))),
                "graded": int(_f(rij.get("gradedStock"))),
                "dag": vandaag,
                "titel": _s(rij, "title"),
            }
            log_[f"{vandaag}|{ean}"] = int(_f(rij.get("regularStock")))
            gevonden += 1
        if len(lijst) < 50:
            break
    log(f"Voorraad klaar ({gevonden} artikelen gevonden)")
    return gevonden


# ---------------------------------------------------------------------- main

def main():
    sleutel = os.environ.get("DATA_SLEUTEL", "").strip()
    if not sleutel:
        print("DATA_SLEUTEL ontbreekt.", file=sys.stderr)
        return 2
    try:
        instellingen = json.loads(os.environ.get("INSTELLINGEN") or "{}")
    except json.JSONDecodeError as e:
        print(f"INSTELLINGEN is geen geldige JSON: {e}", file=sys.stderr)
        return 2
    producten = instellingen.get("producten") or {}
    eans = set(producten)
    if not eans:
        print("Geen producten in INSTELLINGEN.", file=sys.stderr)
        return 2

    herstart = os.environ.get("HERSTART", "").strip().lower() in ("1", "ja", "true")
    bestaand = None
    try:
        bestaand = None if herstart else kluis.lees_bestand(DATA, sleutel)
    except Exception as e:
        # Niet doorgaan met een lege stand: dan zou de opgebouwde historie (die
        # bol zelf na drie maanden niet meer teruggeeft) in één ronde weg zijn.
        # Wil je de code echt veranderen, draai dan met HERSTART=1.
        log(f"Bestaande data.json kon niet geopend worden ({type(e).__name__}). "
            f"Klopt DATA_SLEUTEL nog? De historie wordt NIET overschreven; "
            f"deze ronde stopt hier. Bedoel je het wel, draai dan opnieuw met "
            f"'opnieuw beginnen' aangevinkt.")
        return 3
    if herstart:
        log("Opnieuw beginnen: de historie wordt vanaf bol opnieuw opgebouwd "
            "(bestellingen tot 3 maanden terug, facturen tot 2 jaar terug).")
    ruw = (bestaand or {}).get("ruw") or {}
    eerste_keer = not ruw.get("orderregels")

    client = bolapi.Client(os.environ["BOL_CLIENT_ID"],
                           os.environ["BOL_CLIENT_SECRET"], log=log)
    ads_client = bolapi.AdsClient(
        os.environ.get("BOL_ADS_CLIENT_ID") or os.environ["BOL_CLIENT_ID"],
        os.environ.get("BOL_ADS_CLIENT_SECRET") or os.environ["BOL_CLIENT_SECRET"],
        log=log)

    vandaag = date.today()
    vanaf = vandaag - timedelta(days=MAX_HISTORIE_DAGEN if eerste_keer
                                else ORDER_VENSTER)
    fouten = []

    stappen = [
        ("bestellingen", lambda: haal_orders(client, ruw, eans, vanaf)),
        ("landen", lambda: haal_orderdetails(client, ruw, eans)),
        ("retouren", lambda: haal_retours(client, ruw, eans, eerste_keer)),
        ("voorraad", lambda: haal_voorraad(client, ruw, eans)),
        ("facturen", lambda: haal_facturen(client, ruw, eans,
                                           24 if eerste_keer else FACTUUR_MAANDEN)),
        ("advertenties", lambda: haal_ads(ads_client, ruw, eans,
                                          int(os.environ.get("ADS_EERSTE", "180"))
                                          if eerste_keer else ADS_VENSTER)),
    ]
    for naam, stap in stappen:
        try:
            stap()
        except Exception as e:
            # Eén kapotte bron mag de rest van het dashboard niet meeslepen: de
            # oude cijfers voor dat onderdeel blijven gewoon staan.
            fouten.append(f"{naam}: {type(e).__name__}: {e}")
            log(f"FOUT in stap '{naam}': {e}")
            traceback.print_exc()

    # Advertentiedagen van voor de oudste bestelling die we kennen dragen niets
    # bij (zie rekenen.py) en laten het bestand alleen maar groeien.
    eerste = min((r.get("dag") or "9999" for r in ruw.get("orderregels", {}).values()),
                 default="")
    if eerste and eerste != "9999":
        # LET OP: hier stond ooit `for sleutel in ...`, en dat overschreef de
        # variabele `sleutel` met de DATA_SLEUTEL erin. Het bestand werd dan
        # versleuteld met een datum-EAN-combinatie in plaats van met de code van
        # de verkoper - en dat merk je pas als de app zegt dat de code niet
        # klopt. Vandaar korte, eigen namen hieronder.
        for k in [x for x in ruw.get("ads", {}) if x.split("|", 1)[0] < eerste]:
            ruw["ads"].pop(k, None)
        for k in [x for x in ruw.get("adsalg", {}) if x < eerste]:
            ruw["adsalg"].pop(k, None)

    berekend = rekenen.bereken(ruw, instellingen, vandaag)
    payload = dict(berekend)
    payload["ruw"] = ruw
    payload["bijgewerkt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["fouten"] = fouten
    payload["aanroepen"] = client.aanroepen + ads_client.aanroepen

    grootte = kluis.schrijf_bestand(DATA, payload, sleutel)
    t = berekend["totaal"]
    log(f"data.json geschreven ({grootte / 1024:.0f} kB) - "
        f"omzet {t['omzet_excl']:.2f}, winst {t['winst']:.2f}, "
        f"{t['stuks']} stuks, {t['bestellingen']} bestellingen")
    if fouten:
        log("Let op, niet alles is gelukt: " + " | ".join(fouten))
    return 0


if __name__ == "__main__":
    sys.exit(main())
