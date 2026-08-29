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


def _tekst(waarde):
    if waarde in (None, "") or isinstance(waarde, (list, dict)):
        return ""
    return str(waarde)


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

    ruw.setdefault("stand", {})["orders_tot"] = vandaag.isoformat()
    log(f"Bestellingen klaar ({len(regels)} regels bewaard, {nieuw} nieuw)")
    return nieuw


def haal_orderdetails(client, ruw, eans):
    """
    Per bestelling het VOLLEDIGE detail ophalen: prijs, commissie en land.

    DIT IS GEEN LUXE. De bestellingenlijst van bol geeft wel het aantal en de
    EAN, maar GEEN unitPrice en GEEN commission - die staan alleen in het
    detail van een bestelling. Zonder deze stap komt elke nieuwe bestelling
    binnen op nul euro: het dashboard telde tien verkopen in augustus en zette
    de omzet ervan op honderd euro in plaats van tweehonderdvijftig. Een
    ontbrekend bedrag is erger dan een ontbrekende bestelling, want het valt
    niet op - de bestelling staat er gewoon, alleen gratis.

    Een plafond per ronde zorgt dat een inhaalslag nooit de hele run opsnoept;
    wat overblijft komt de volgende ronde.
    """
    orders = ruw.setdefault("orders", {})
    regels = ruw.get("orderregels", {})

    per_order = {}
    for oii, r in regels.items():
        per_order.setdefault(r.get("oid"), []).append((oii, r))

    nodig = []
    for oid, rijen in per_order.items():
        if not oid:
            continue
        geen_prijs = any(not r.get("prijs") and
                         (r.get("aantal", 0) - r.get("geannuleerd", 0)) > 0
                         for _oii, r in rijen)
        if geen_prijs or not (orders.get(oid) or {}).get("land"):
            nodig.append(oid)
    # Nieuwste eerst: die zijn het interessantst en het meest kansrijk (bol
    # geeft een oude bestelling op een gegeven moment niet meer terug).
    nodig.sort(key=lambda oid: (orders.get(oid, {}).get("dag") or ""), reverse=True)
    nodig = nodig[:MAX_DETAILS]
    if not nodig:
        return 0

    log(f"Details ophalen voor {len(nodig)} bestellingen (prijs, commissie, land)")
    gehaald = 0
    for oid in nodig:
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
            orders.setdefault(oid, {})["land"] = land
        for item in detail.get("orderItems") or []:
            oii = _s(item, "orderItemId")
            r = regels.get(oii)
            if not r:
                continue
            r["aantal"] = int(_f(item.get("quantity"))) or r.get("aantal", 0)
            r["verzonden"] = int(_f(item.get("quantityShipped")))
            r["geannuleerd"] = int(_f(item.get("quantityCancelled")))
            r["prijs"] = _f(item.get("unitPrice")) or r.get("prijs", 0)
            r["commissie"] = _f(item.get("commission")) or r.get("commissie", 0)
            if land:
                r["land"] = land
        gehaald += 1
    ontbreekt_nog = sum(1 for _oii, r in
                        [(a, b) for rijen in per_order.values() for a, b in rijen]
                        if not r.get("prijs")
                        and (r.get("aantal", 0) - r.get("geannuleerd", 0)) > 0)
    log(f"  {gehaald} bestellingen bijgewerkt"
        + (f", {ontbreekt_nog} regels nog zonder prijs" if ontbreekt_nog else ""))
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

# HET SOORT STAAT NIET ALTIJD IN DE REGEL-ID.
#
# Bol codeert het meestal als laatste stuk van de id (`...#TURNOVER`), maar in
# de JSON-specificatie lang niet altijd: verzendregels en opslagregels kwamen
# binnen zonder herkenbaar soort. Wat er dan nog staat is de omschrijving. Zonder
# deze terugval verdween eerst alle verzendkosten en daarna alle opslag uit het
# dashboard - allebei stil, want een ontbrekende kostenpost geeft geen foutmelding,
# alleen een te mooie winst.
OMSCHRIJVING_SOORT = (
    ("pakketzegel", "SHIPMENT_LABEL"), ("verzend", "SHIPMENT_LABEL"),
    ("pick", "PICK_PACK"),
    ("voorraadkosten", "STORAGE"), ("opslag", "STORAGE"),
    ("commissie", "COMMISSION"),
    ("verkoopprijs", "TURNOVER"),
)


def _soort_van(regel):
    """Het soort van een factuurregel, uit de id of anders uit de omschrijving."""
    soort = regel.get("soort") or ""
    if soort in TYPES:
        return soort
    omschrijving = (regel.get("omschrijving") or "").lower()
    correctie = "correctie" in omschrijving
    for stuk, gevonden in OMSCHRIJVING_SOORT:
        if stuk in omschrijving:
            if gevonden == "TURNOVER" and correctie:
                return "CORRECTION_TURNOVER"
            if gevonden == "COMMISSION" and correctie:
                return "COMMISSION"
            return gevonden
    return soort


def migreer_facturen(ruw):
    """
    De oude, platte factuuropslag omzetten naar opslag PER FACTUUR.

    Waarom dat verschil er toe doet: bol factureert niet per maand maar per
    PERIODE, en er zitten er meerdere in een maand. De oude opzet bewaarde
    alleen `maand|ean|soort` en veegde bij het ophalen eerst de hele maand
    leeg. Haalde een ronde dan één factuur van die maand op, dan verdween wat
    de andere facturen van diezelfde maand hadden bijgedragen - en dat is
    precies wat er gebeurde: van vier gefactureerde maanden bleven er twee
    over, en de winst van mei en juni ging op een schatting draaien terwijl de
    echte bedragen bekend waren.

    Nu bewaart elke factuur zijn eigen bijdrage. Opnieuw ophalen vervangt
    alleen die ene factuur.
    """
    facturen = ruw.setdefault("facturen", {})
    plat = ruw.pop("factuur", None)
    plat_aantal = ruw.pop("factuur_aantal", None)
    if not plat and not plat_aantal:
        return facturen
    for sleutel, bedrag in (plat or {}).items():
        maand, ean, soort = sleutel.split("|", 2)
        f = facturen.setdefault("overgezet:" + maand,
                                {"maand": maand, "posten": {}, "aantal": {}})
        f["posten"][f"{ean}|{soort}"] = bedrag
    for sleutel, aantal in (plat_aantal or {}).items():
        maand, ean = sleutel.split("|", 1)
        f = facturen.setdefault("overgezet:" + maand,
                                {"maand": maand, "posten": {}, "aantal": {}})
        f["aantal"][ean] = aantal
    return facturen


def haal_facturen(client, ruw, eans, maanden):
    """
    De factuurspecificatie is de enige plek waar staat wat bol ECHT rekende:
    commissie, pick&pack en opslag per artikel. Gaat twee jaar terug.

    Per factuur opgeslagen, niet per maand - zie migreer_facturen().
    """
    facturen = migreer_facturen(ruw)
    vandaag = date.today()
    regels = 0
    echte_maanden = set()
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
            # Alleen DEZE factuur schoonvegen voordat we hem opnieuw inlezen,
            # zodat een gecorrigeerde factuur geen dubbeltelling oplevert maar
            # de andere facturen van dezelfde periode blijven staan.
            # `winkel` houdt drie WINKELBREDE totalen bij: wat bol in deze
            # factuur rekende aan verzenden en pick&pack, en hoeveel stuks hij
            # factureerde. Meer niet - geen artikelen, geen namen, geen omzet
            # van de rest van het assortiment. Zie hieronder waarom dat moet.
            deze = {"maand": maand, "posten": {}, "aantal": {},
                    "winkel": {"verzend": 0.0, "pickpack": 0.0, "stuks": 0.0}}
            gezien = set()
            for pagina in range(1, 21):
                try:
                    spec = client.factuurspecificatie(inv_id, pagina)
                except bolapi.BolFout as e:
                    log(f"  specificatie {inv_id} p{pagina} mislukt ({e.status})")
                    break
                rijen = _spec_regels(spec)
                if not rijen:
                    break
                # NIET ELKE SPECIFICATIE PAGINEERT.
                #
                # Bol negeert de page-parameter op dit endpoint en stuurt elke
                # keer dezelfde regels terug. De lus telde die dus twintig keer
                # op: de aprilfactuur kwam uit op veertig verkochte stuks en
                # vijfennegentig euro pick&pack, terwijl er negen matten waren
                # verkocht. Elke regel heeft een eigen id; levert een pagina
                # niets nieuws op, dan zijn we klaar.
                vers = [r for r in rijen if r["id"] and r["id"] not in gezien]
                if not vers:
                    break
                gezien.update(r["id"] for r in vers)
                for r in vers:
                    r["soort"] = _soort_van(r)
                    # VERZENDKOSTEN DRAGEN GEEN EAN.
                    #
                    # Bol rekent verzenden per ZENDING, niet per artikel: op dit
                    # account stonden 2.617 verzendregels voor 11.008 euro, geen
                    # enkele met een EAN. De filter hieronder gooide die dus
                    # allemaal weg, en het dashboard liet de winst van de rode
                    # mat op 167 euro staan terwijl het er 50 was. Daarom worden
                    # de winkelbrede totalen apart geteld, vóór de filter, en
                    # daaruit komt een tarief per verkocht stuk - precies zoals
                    # de eigen app van de verkoper het doet.
                    if r["soort"] == "SHIPMENT_LABEL":
                        deze["winkel"]["verzend"] += abs(r["bedrag"])
                    elif r["soort"] == "PICK_PACK":
                        deze["winkel"]["pickpack"] += abs(r["bedrag"])
                    elif r["soort"] == "TURNOVER":
                        deze["winkel"]["stuks"] += r["aantal"]

                    if r["ean"] not in eans or r["soort"] not in TYPES:
                        continue
                    sleutel = f"{r['ean']}|{r['soort']}"
                    deze["posten"][sleutel] = round(
                        deze["posten"].get(sleutel, 0.0) + r["bedrag"], 4)
                    if r["soort"] in ("TURNOVER", "CORRECTION_TURNOVER"):
                        teken = 1 if r["soort"] == "TURNOVER" else -1
                        deze["aantal"][r["ean"]] = round(
                            deze["aantal"].get(r["ean"], 0.0) + teken * r["aantal"], 3)
                    regels += 1
                if len(rijen) < 100:
                    break
            if deze["posten"] or deze["winkel"]["stuks"]:
                facturen[str(inv_id)] = deze
                echte_maanden.add(maand)
        log(f"  facturen {van:%Y-%m} verwerkt ({regels} regels tot nu toe)")

    # Waar we nu ECHTE facturen van hebben, mag de overgezette samenvatting uit
    # de oude database weg - anders staat hetzelfde bedrag er twee keer in.
    for maand in echte_maanden:
        facturen.pop("overgezet:" + maand, None)
    # Het fulfilmenttarief hardop zeggen: dit is de post die het stilst fout
    # kan gaan (verzendregels zonder EAN), dus hij hoort in het logboek.
    per_maand = {}
    for f in facturen.values():
        w = f.get("winkel") or {}
        if not w.get("stuks"):
            continue
        vak = per_maand.setdefault(f.get("maand") or "?",
                                   {"verzend": 0.0, "pickpack": 0.0, "stuks": 0.0})
        for veld in vak:
            vak[veld] += float(w.get(veld) or 0)
    soorten = {}
    for f in facturen.values():
        for sleutel in (f.get("posten") or {}):
            soort = sleutel.split("|", 1)[1]
            soorten[soort] = soorten.get(soort, 0) + 1
    if soorten:
        log("  posten per soort: " +
            ", ".join(f"{k} {v}" for k, v in sorted(soorten.items())))
    for maand in sorted(per_maand)[-4:]:
        v = per_maand[maand]
        if v["stuks"]:
            log(f"  fulfilment {maand}: verzenden EUR {v['verzend'] / v['stuks']:.2f} + "
                f"pick&pack EUR {v['pickpack'] / v['stuks']:.2f} per stuk "
                f"({int(v['stuks'])} stuks gefactureerd)")
    log(f"Facturen klaar ({len(facturen)} facturen bewaard)")
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
        # De omschrijving erbij: het soort uit de regel-id is niet altijd
        # ingevuld, en dan is "Verzendkosten" of "Pick&pack kosten" het enige
        # dat de regel nog verraadt.
        omschrijving = (_tekst(_uitpakken(_ci(item, "Description")))
                        or _tekst(_uitpakken(_ci(item, "Name"))) or "")
        uit.append({"id": _s(regel, "id") or _s(regel, "invoiceLineRef"),
                    "soort": soort or "UNKNOWN", "ean": ean, "bedrag": bedrag,
                    "aantal": abs(aantal), "omschrijving": omschrijving.lower()})
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

    # Eén keer de volle twee jaar facturen, daarna alleen de laatste maanden.
    # De markering staat in het bestand zelf, zodat een correctie in de code
    # (zoals het ontdubbelen van factuurregels) de oude maanden ook echt
    # opnieuw ophaalt in plaats van de foute versie te laten staan.
    FACTUURVERSIE = 2
    stand = ruw.setdefault("stand", {})
    volledig = stand.get("facturen_versie") == FACTUURVERSIE
    factuurmaanden = FACTUUR_MAANDEN if volledig else 24
    if not volledig:
        log("Facturen: eenmalig de volle historie ophalen (twee jaar terug).")

    vandaag = date.today()
    if eerste_keer:
        vanaf = vandaag - timedelta(days=MAX_HISTORIE_DAGEN)
    else:
        # Het venster rekt mee met het gat. Stond de vorige ronde drie weken
        # geleden (of komt de historie uit een database die al even stil lag),
        # dan zou een vast venster van tien dagen de tussenliggende
        # bestellingen voorgoed overslaan - bol geeft ze na drie maanden niet
        # meer terug.
        vanaf = vandaag - timedelta(days=ORDER_VENSTER)
        # Vanaf WAAR WE LAATST HEBBEN OPGEHAALD, niet vanaf de laatste
        # bestelling die we kennen. Dat scheelt: na een stille week is de
        # laatste bestelling ook van een week geleden en lijkt er geen gat te
        # zijn, terwijl de dagen ertussen nooit zijn opgehaald. Zo bleef er een
        # gat van 5 tot 20 augustus staan waar wél verkocht was.
        tot = (ruw.get("stand") or {}).get("orders_tot")
        if tot:
            try:
                vanaf = min(vanaf, date.fromisoformat(tot) - timedelta(days=2))
            except ValueError:
                pass
        else:
            # Nog nooit een markering gezet (bv. historie uit de overzet): één
            # keer het volle venster, dan weten we het zeker.
            vanaf = vandaag - timedelta(days=MAX_HISTORIE_DAGEN)
        vanaf = max(vanaf, vandaag - timedelta(days=MAX_HISTORIE_DAGEN))
    fouten = []

    stappen = [
        ("bestellingen", lambda: haal_orders(client, ruw, eans, vanaf)),
        ("landen", lambda: haal_orderdetails(client, ruw, eans)),
        ("retouren", lambda: haal_retours(client, ruw, eans, eerste_keer)),
        ("voorraad", lambda: haal_voorraad(client, ruw, eans)),
        ("facturen", lambda: haal_facturen(client, ruw, eans, factuurmaanden)),
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

    # De markering pas zetten als de factuurstap ook echt gelukt is.
    if not volledig and not any(f.startswith("facturen:") for f in fouten):
        ruw.setdefault("stand", {})["facturen_versie"] = FACTUURVERSIE

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
