"""
Van ruwe bol-gegevens naar de cijfers die het dashboard toont.

DE WINSTFORMULE, in één regel:

    winst = omzet excl. btw
          - commissie
          - pick&pack (fulfilment door bol)
          - opslag
          - advertenties
          - inkoop
          - het verlies op retouren

Waar elk bedrag vandaan komt:

  omzet, stuks, commissie   de bestellingen zelf (per dag, direct na de
                            bestelling bekend)
  pick&pack en opslag       de MAANDFACTUUR van bol, per EAN. Die komt pas na
                            afloop van de maand; voor de lopende maand rekenen
                            we met het tarief per stuk uit de laatste drie
                            gefactureerde maanden. Zo'n maand staat in het
                            dashboard als 'nog niet gefactureerd'.
  advertenties              het dagrapport van bol, per EAN. Alleen de kosten
                            die bol aan deze vier artikelen toewijst;
                            merkcampagnes die aan de hele winkel hangen tellen
                            hier bewust niet mee (die zijn niet aan een artikel
                            toe te rekenen).
  inkoop                    kostprijs per stuk maal het aantal verkochte stuks.
  retouren                  een VERWERKTE en GOEDGEKEURDE retour draait de
                            omzet en de commissie terug. De inkoopwaarde komt
                            terug tenzij bol het artikel als verloren of
                            vernietigd afmeldt - dan ben je de mat kwijt.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

# Uitkomsten die betekenen dat het artikel NIET terug op voorraad komt.
VOORRAAD_KWIJT = ("RETURN_ITEM_LOST", "RETURN_ITEM_DESTROYED")
# Alleen een goedgekeurde retour is echt terugbetaald aan de klant.
TERUGBETAALD = ("ACCEPTED",)

KPI_VELDEN = ("omzet_excl", "omzet_incl", "stuks", "commissie", "pickpack",
              "opslag", "ads", "inkoop", "retour_stuks", "retour_verlies",
              "winst")


def _leeg():
    kpi = {v: 0.0 for v in KPI_VELDEN}
    kpi["stuks"] = 0
    kpi["retour_stuks"] = 0
    kpi["bestellingen"] = 0
    return kpi


def _tel_op(doel, bron):
    for k, v in bron.items():
        if k in ("bestellingen", "_orders"):
            continue
        if isinstance(v, (int, float)):
            doel[k] = doel.get(k, 0) + v


def _afronden(kpi):
    for k, v in list(kpi.items()):
        if isinstance(v, float):
            kpi[k] = round(v, 2)
    omzet = kpi.get("omzet_excl") or 0
    kpi["marge"] = round(100.0 * kpi["winst"] / omzet, 1) if omzet else 0.0
    stuks = kpi.get("stuks") or 0
    kpi["retour_pct"] = round(100.0 * kpi["retour_stuks"] / stuks, 1) if stuks else 0.0
    return kpi


def _maand(dag):
    return (dag or "")[:7]


def _maanden_tussen(eerste, laatste):
    uit = []
    j, m = int(eerste[:4]), int(eerste[5:7])
    ej, em = int(laatste[:4]), int(laatste[5:7])
    while (j, m) <= (ej, em):
        uit.append(f"{j:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, j = 1, j + 1
    return uit


def _dagen_in_maand(maand):
    j, m = int(maand[:4]), int(maand[5:7])
    volgende = date(j + (m == 12), 1 if m == 12 else m + 1, 1)
    return (volgende - date(j, m, 1)).days


def bereken(ruw, instellingen, vandaag=None):
    vandaag = vandaag or date.today()
    vandaag_iso = vandaag.isoformat()
    producten = instellingen.get("producten") or {}
    eans = [e for e in producten] or sorted({r["ean"] for r in ruw.get("orderregels", {}).values()})
    standaard_btw = float(instellingen.get("btw", 21))

    def btw_van(ean):
        return float((producten.get(ean) or {}).get("btw", standaard_btw))

    def kostprijs(ean):
        return float((producten.get(ean) or {}).get("kostprijs", 0) or 0)

    # ---------------------------------------------------------- bestellingen
    regels = [r for r in ruw.get("orderregels", {}).values()
              if r.get("ean") in eans and (r.get("aantal", 0) - r.get("geannuleerd", 0)) > 0]
    regels.sort(key=lambda r: (r.get("dag") or "", r.get("oid") or ""))

    per_me = defaultdict(_leeg)          # (maand, ean)
    per_dag = defaultdict(lambda: defaultdict(float))
    orders_per_me = defaultdict(set)
    stuks_per_maand_ean = defaultdict(int)

    for r in regels:
        ean, dag = r["ean"], r.get("dag") or ""
        maand = _maand(dag)
        netto = r["aantal"] - r.get("geannuleerd", 0)
        btw = btw_van(ean)
        omzet_incl = float(r.get("prijs", 0)) * netto
        omzet_excl = omzet_incl / (1 + btw / 100.0)
        # Bol vult het commissieveld voor de HELE bestelde regel, maar rekent
        # niets over een geannuleerd stuk. Dus naar rato van het netto aantal.
        commissie = (float(r.get("commissie", 0)) * netto / r["aantal"]) if r["aantal"] else 0.0

        k = per_me[(maand, ean)]
        k["omzet_incl"] += omzet_incl
        k["omzet_excl"] += omzet_excl
        k["commissie"] += commissie
        k["stuks"] += netto
        k["inkoop"] += kostprijs(ean) * netto
        orders_per_me[(maand, ean)].add(r.get("oid"))
        stuks_per_maand_ean[(maand, ean)] += netto

        d = per_dag[dag]
        d["omzet_excl"] += omzet_excl
        d["omzet_incl"] += omzet_incl
        d["stuks"] += netto
        d["commissie"] += commissie
        d["inkoop"] += kostprijs(ean) * netto

    # --------------------------------------------------------- factuurkosten
    #
    # Bedragen op de factuur staan negatief (het is een kostenpost); hier gaan
    # ze positief verder, zodat er verderop gewoon mee afgetrokken wordt.
    factuur = ruw.get("factuur", {}) or {}
    fact_aantal = ruw.get("factuur_aantal", {}) or {}
    gefactureerd = set()
    fact_bedrag = defaultdict(float)     # (maand, ean, soort) -> positief bedrag
    for sleutel, bedrag in factuur.items():
        maand, ean, soort = sleutel.split("|", 2)
        if soort in ("PICK_PACK", "COMMISSION", "STORAGE", "SHIPMENT_LABEL"):
            fact_bedrag[(maand, ean, soort)] += abs(float(bedrag))
        if soort in ("PICK_PACK", "COMMISSION", "TURNOVER"):
            gefactureerd.add(maand)

    # Tarief per stuk uit de laatste drie gefactureerde maanden. Een maand
    # zonder enige fulfilmentkosten telt niet mee: die zegt niets over wat FBB
    # nu kost en trekt het gemiddelde alleen omlaag.
    def tarief(soort):
        maanden = sorted(m for m in gefactureerd
                         if any(fact_bedrag.get((m, e, soort)) for e in eans))
        maanden = maanden[-3:]
        kosten = sum(fact_bedrag.get((m, e, soort), 0.0) for m in maanden for e in eans)
        # Delen door het aantal stuks DAT BOL FACTUREERDE, niet door onze eigen
        # telling: van een maand die wij maar half kennen (bol geeft drie
        # maanden bestellingen terug) zou het tarief anders veel te hoog
        # uitvallen, en dat tarief bepaalt de kosten van de lopende maand.
        stuks = sum(fact_aantal.get(f"{m}|{e}", 0) for m in maanden for e in eans)
        if not stuks:
            stuks = sum(stuks_per_maand_ean.get((m, e), 0) for m in maanden for e in eans)
        return (kosten / stuks) if stuks else 0.0

    tarief_pickpack = tarief("PICK_PACK") + tarief("SHIPMENT_LABEL")

    # Opslag hangt aan voorraad, niet aan verkoop. Voor een maand die nog niet
    # gefactureerd is nemen we het gemiddelde van de laatste gefactureerde
    # maanden per artikel, naar rato van de dagen die al verstreken zijn.
    def opslag_gemiddeld(ean):
        maanden = sorted(m for m in gefactureerd if fact_bedrag.get((m, ean, "STORAGE")))
        maanden = maanden[-3:]
        if not maanden:
            return 0.0
        return sum(fact_bedrag[(m, ean, "STORAGE")] for m in maanden) / len(maanden)

    # ---------------------------------------------------------- advertenties
    ads_per_me = defaultdict(float)
    ads_per_dag_ean = defaultdict(float)
    kliks = defaultdict(int)
    vertoningen = defaultdict(int)
    # ADVERTENTIES VAN VOOR DE EERSTE BEKENDE BESTELLING TELLEN NIET MEE.
    #
    # Het advertentierapport gaat twaalf maanden terug, de bestellingen bij bol
    # maar drie. Zonder deze grens krijg je maanden met alleen advertentiekosten
    # en geen omzet - een verlies dat er nooit geweest is, puur omdat bol de
    # bestellingen van toen niet meer teruggeeft.
    eerste_dag = min((r.get("dag") or "9999" for r in regels), default="")
    for sleutel, waarde in (ruw.get("ads", {}) or {}).items():
        dag, ean = sleutel.split("|", 1)
        if ean not in eans or (eerste_dag and dag < eerste_dag):
            continue
        kosten = float(waarde[0] if isinstance(waarde, list) else waarde)
        ads_per_me[(_maand(dag), ean)] += kosten
        ads_per_dag_ean[(dag, ean)] += kosten
        per_dag[dag]["ads"] += kosten
        if isinstance(waarde, list) and len(waarde) >= 3:
            vertoningen[(_maand(dag), ean)] += int(waarde[1] or 0)
            kliks[(_maand(dag), ean)] += int(waarde[2] or 0)

    # --------------------------------------------------------------- retours
    retour_rijen = []
    for rma, r in (ruw.get("retours", {}) or {}).items():
        if r.get("ean") not in eans:
            continue
        aantal = int(r.get("terug") or 0) or int(r.get("verwacht") or 0)
        verwerkt = r.get("verwerkt") or ""
        goedgekeurd = (r.get("resultaat") or "") in TERUGBETAALD
        ean = r["ean"]
        # De verkoopprijs van dit artikel: bij voorkeur de bestelling waar de
        # retour bij hoort, anders de gemiddelde prijs van dat artikel. Bol
        # vult orderItemId op retouren niet in, dus koppelen we op bestelling
        # plus EAN.
        prijs, commissie_stuk = _prijs_van_retour(r, regels, ean)
        btw = btw_van(ean)
        omzet_terug = aantal * prijs / (1 + btw / 100.0)
        comm_terug = aantal * commissie_stuk
        voorraad_terug = 0.0 if (r.get("uitkomst") or "") in VOORRAAD_KWIJT else 1.0
        inkoop_terug = aantal * kostprijs(ean) * voorraad_terug
        verlies = omzet_terug - comm_terug - inkoop_terug

        rij = {
            "rma": rma, "ean": ean, "aangemeld": (r.get("aangemeld") or "")[:10],
            "verwerkt": verwerkt[:10], "aantal": aantal,
            "reden": r.get("reden") or "", "toelichting": r.get("detail") or "",
            "resultaat": r.get("resultaat") or "",
            "afgehandeld": bool(r.get("afgehandeld")),
            "verlies": round(verlies, 2) if goedgekeurd else 0.0,
            "oid": r.get("oid") or "",
        }
        retour_rijen.append(rij)

        if goedgekeurd and verwerkt:
            maand = _maand(verwerkt)
            k = per_me[(maand, ean)]
            k["retour_stuks"] += aantal
            k["retour_verlies"] += verlies
            per_dag[verwerkt[:10]]["retour_verlies"] += verlies

    retour_rijen.sort(key=lambda r: (r["aangemeld"] or r["verwerkt"] or ""), reverse=True)

    # ------------------------------------------- maandkosten invullen + winst
    alle_maanden = sorted({m for (m, _e) in per_me} | {m for (m, _e) in ads_per_me})
    if alle_maanden:
        alle_maanden = _maanden_tussen(alle_maanden[0], max(alle_maanden[-1],
                                                            _maand(vandaag_iso)))
    schatting_maanden, deel_maanden = [], []
    eerste_verkoop = min((m for (m, _e) in per_me if per_me[(m, _e)]["stuks"]),
                         default=None)
    for maand in alle_maanden:
        maand_is_gefactureerd = maand in gefactureerd
        if not maand_is_gefactureerd:
            schatting_maanden.append(maand)
        for ean in eans:
            k = per_me[(maand, ean)]
            stuks = k["stuks"]
            if maand_is_gefactureerd:
                # HOEVEEL VAN DEZE MAAND KENNEN WE?
                #
                # De factuur beslaat de hele maand; onze bestelhistorie soms
                # maar een deel ervan (bol geeft drie maanden terug, ouder komt
                # uit de eenmalige overzet). Zetten we dan de volle maandkosten
                # naast een halve maand omzet, dan staat die maand op verlies
                # terwijl er niets aan de hand is. Daarom schalen we de kosten
                # mee met het deel van de maand dat we echt kennen.
                gefactureerde_stuks = fact_aantal.get(f"{maand}|{ean}", 0)
                deel = 1.0
                if gefactureerde_stuks and stuks < gefactureerde_stuks * 0.98:
                    deel = stuks / gefactureerde_stuks
                    if maand not in deel_maanden:
                        deel_maanden.append(maand)
                k["pickpack"] = deel * (
                    fact_bedrag.get((maand, ean, "PICK_PACK"), 0.0)
                    + fact_bedrag.get((maand, ean, "SHIPMENT_LABEL"), 0.0))
                k["opslag"] = deel * fact_bedrag.get((maand, ean, "STORAGE"), 0.0)
                k["onvolledig"] = deel < 1.0
            else:
                k["pickpack"] = tarief_pickpack * stuks
                # Opslag hangt aan voorraad, niet aan verkoop. Alleen ramen voor
                # maanden waarin er ook echt iets gebeurde: anders krijgt een
                # maand van voor de eerste verkoop opslagkosten uit het niets.
                raam = stuks > 0 or maand == _maand(vandaag_iso)
                if eerste_verkoop and maand < eerste_verkoop:
                    raam = False
                deel = 1.0
                if maand == _maand(vandaag_iso):
                    deel = vandaag.day / float(_dagen_in_maand(maand))
                k["opslag"] = opslag_gemiddeld(ean) * deel if raam else 0.0
            k["ads"] = ads_per_me.get((maand, ean), 0.0)
            k["bestellingen"] = len(orders_per_me.get((maand, ean), ()))
            k["winst"] = (k["omzet_excl"] - k["commissie"] - k["pickpack"]
                          - k["opslag"] - k["ads"] - k["inkoop"] - k["retour_verlies"])
            k["geschat"] = not maand_is_gefactureerd

    # -------------------------------------------------------------- optellen
    per_maand, per_product, totaal = {}, {}, _leeg()
    totaal_orders, orders_per_maand = set(), defaultdict(set)
    for (maand, ean), k in per_me.items():
        for o in orders_per_me.get((maand, ean), ()):
            totaal_orders.add(o)
            orders_per_maand[maand].add(o)

    for maand in alle_maanden:
        m_kpi = _leeg()
        producten_maand = {}
        for ean in eans:
            k = dict(per_me[(maand, ean)])
            k["vertoningen"] = vertoningen.get((maand, ean), 0)
            k["kliks"] = kliks.get((maand, ean), 0)
            producten_maand[ean] = _afronden(k)
            _tel_op(m_kpi, k)
            p = per_product.setdefault(ean, _leeg())
            _tel_op(p, k)
            p["bestellingen"] = p.get("bestellingen", 0) + k.get("bestellingen", 0)
        m_kpi["bestellingen"] = len(orders_per_maand.get(maand, ()))
        m_kpi["geschat"] = maand not in gefactureerd
        per_maand[maand] = {"totaal": _afronden(m_kpi), "producten": producten_maand}
        _tel_op(totaal, m_kpi)

    totaal["bestellingen"] = len(totaal_orders)
    totaal = _afronden(totaal)
    for ean in per_product:
        per_product[ean] = _afronden(per_product[ean])

    # ------------------------------------------------------------ dagreeksen
    dagen = []
    for dag in sorted(per_dag):
        d = per_dag[dag]
        dagen.append({
            "dag": dag,
            "omzet": round(d.get("omzet_excl", 0.0), 2),
            "stuks": int(d.get("stuks", 0)),
            "ads": round(d.get("ads", 0.0), 2),
        })

    # ------------------------------------------------- bestellingen voor de lijst
    retour_per_order = defaultdict(list)
    for r in retour_rijen:
        if r["oid"]:
            retour_per_order[r["oid"]].append(r)

    orders = {}
    for r in regels:
        oid = r.get("oid")
        o = orders.setdefault(oid, {"id": oid, "dag": r.get("dag"),
                                    "tijd": (r.get("tijd") or "")[11:16],
                                    "land": r.get("land") or "",
                                    "regels": [], "bedrag": 0.0, "stuks": 0})
        netto = r["aantal"] - r.get("geannuleerd", 0)
        o["regels"].append({"ean": r["ean"], "aantal": netto,
                            "prijs": round(float(r.get("prijs", 0)), 2)})
        o["bedrag"] += float(r.get("prijs", 0)) * netto
        o["stuks"] += netto
    bestellingen = []
    for oid, o in orders.items():
        o["bedrag"] = round(o["bedrag"], 2)
        rets = retour_per_order.get(oid) or []
        o["retour"] = len(rets)
        bestellingen.append(o)
    bestellingen.sort(key=lambda o: (o["dag"] or "", o["tijd"] or ""), reverse=True)

    # --------------------------------------------------------------- voorraad
    laatste_28 = defaultdict(int)
    grens = (vandaag - timedelta(days=28)).isoformat()
    for r in regels:
        if (r.get("dag") or "") >= grens:
            laatste_28[r["ean"]] += r["aantal"] - r.get("geannuleerd", 0)

    extra = instellingen.get("voorraad") or {}
    voorraad = []
    for ean in eans:
        bij_bol = (ruw.get("voorraad", {}) or {}).get(ean) or {}
        e = extra.get(ean) or {}
        bol_aantal = int(bij_bol.get("bol") or 0)
        graded = int(bij_bol.get("graded") or 0)
        fulfilment = int(e.get("fulfilment") or 0)
        thuis = int(e.get("thuis") or 0)
        totaal_stuks = bol_aantal + graded + fulfilment + thuis
        per_dag_verkoop = laatste_28.get(ean, 0) / 28.0
        voorraad.append({
            "ean": ean, "bol": bol_aantal, "graded": graded,
            "fulfilment": fulfilment, "thuis": thuis, "totaal": totaal_stuks,
            "gemeten": bij_bol.get("dag") or "",
            "per_dag": round(per_dag_verkoop, 2),
            "dagen": int(bol_aantal / per_dag_verkoop) if per_dag_verkoop else None,
            "dagen_totaal": int(totaal_stuks / per_dag_verkoop) if per_dag_verkoop else None,
        })

    return {
        "producten": [dict({"ean": e}, **(producten.get(e) or {})) for e in eans],
        "maanden": alle_maanden,
        "totaal": totaal,
        "per_maand": per_maand,
        "per_product": per_product,
        "dagen": dagen,
        "bestellingen": bestellingen,
        "retours": retour_rijen,
        "voorraad": voorraad,
        "tarieven": {
            "pickpack_per_stuk": round(tarief_pickpack, 3),
            "gefactureerde_maanden": sorted(gefactureerd),
            "geschatte_maanden": schatting_maanden,
            "onvolledige_maanden": deel_maanden,
        },
    }


def _prijs_van_retour(retour, regels, ean):
    """
    Prijs en commissie per stuk voor een retour.

    Ladder, van precies naar bruikbaar:
      1. dezelfde bestelling én hetzelfde artikel
      2. de gewogen gemiddelde prijs van dat artikel over alle verkopen
    Bol vult orderItemId op retouren niet in, dus stap 1 gaat op bestelnummer.
    Zonder deze ladder is de geldimpact van elke retour stilzwijgend nul.
    """
    oid = retour.get("oid")
    if oid:
        treffers = [r for r in regels if r.get("oid") == oid and r.get("ean") == ean]
        stuks = sum(r["aantal"] for r in treffers)
        if stuks:
            prijs = sum(float(r.get("prijs", 0)) * r["aantal"] for r in treffers) / stuks
            comm = sum(float(r.get("commissie", 0)) for r in treffers) / stuks
            return prijs, comm
    treffers = [r for r in regels if r.get("ean") == ean]
    stuks = sum(r["aantal"] for r in treffers)
    if not stuks:
        return 0.0, 0.0
    prijs = sum(float(r.get("prijs", 0)) * r["aantal"] for r in treffers) / stuks
    comm = sum(float(r.get("commissie", 0)) for r in treffers) / stuks
    return prijs, comm
