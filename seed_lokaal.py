#!/usr/bin/env python3
"""
EENMALIG op je eigen pc draaien: vult data.json met je bestaande historie.

Bol geeft via de API maar drie maanden bestellingen terug. Alles daarvoor staat
alleen nog in de database van je eigen dashboard. Dit script leest die database,
haalt er de vier badmatten uit en schrijft ze in het formaat dat de webapp
gebruikt. Daarna houdt GitHub het bij.

    python seed_lokaal.py --sleutel "JOUW-GEHEIME-CODE"

Opties:
    --db PAD        andere database dan de standaard (%APPDATA%\\Seloo\\bol_data.db)
    --uit PAD       ander doelbestand dan data.json naast dit script

Aan het eind print het script de INSTELLINGEN-JSON die je als GitHub-secret
moet zetten. Kostprijzen en eigen voorraad komen uit je eigen database, dus je
hoeft niets over te typen.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ververs"))
import kluis          # noqa: E402
import rekenen        # noqa: E402

EANS = ["5430004400141", "5430004400158", "5430004400110", "5430004400080"]

KLEUREN = [("rood", "Rood"), ("beige", "Beige"), ("marine", "Marine"),
           ("navy", "Marine"),
           ("lichtgrijs", "Lichtgrijs"), ("licht grijs", "Lichtgrijs"),
           ("grijs", "Grijs"), ("zwart", "Zwart"), ("wit", "Wit"),
           ("antraciet", "Antraciet"), ("blauw", "Blauw"), ("groen", "Groen"),
           ("taupe", "Taupe"), ("bleu", "Marine")]


def standaard_db():
    appdata = os.environ.get("APPDATA")
    if appdata:
        pad = os.path.join(appdata, "Seloo", "bol_data.db")
        if os.path.exists(pad):
            return pad
    for kandidaat in (os.path.expanduser("~/Library/Application Support/Seloo/bol_data.db"),
                      os.path.expanduser("~/.local/share/Seloo/bol_data.db"),
                      "bol_data.db"):
        if os.path.exists(kandidaat):
            return kandidaat
    return ""


def korte_naam(titel):
    laag = (titel or "").lower()
    for stuk, naam in KLEUREN:
        if stuk in laag:
            return naam
    return (titel or "Badmat")[:24]


def kolommen(conn, tabel):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabel})")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sleutel", default=os.environ.get("DATA_SLEUTEL", ""))
    p.add_argument("--db", default="")
    p.add_argument("--uit", default="")
    args = p.parse_args()

    if not args.sleutel:
        print("Geef je geheime code mee: --sleutel \"...\"", file=sys.stderr)
        return 2
    db_pad = args.db or standaard_db()
    if not db_pad or not os.path.exists(db_pad):
        print(f"Database niet gevonden ({db_pad or 'geen pad'}). Gebruik --db.",
              file=sys.stderr)
        return 2
    uit = args.uit or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "data.json")

    conn = sqlite3.connect(f"file:{db_pad}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    vraagtekens = ",".join("?" * len(EANS))
    ruw = {}

    # ------------------------------------------------------- bestellingen
    regels, orders = {}, {}
    for r in conn.execute(f"""
            SELECT oi.*, o.ship_country
              FROM order_items oi
              LEFT JOIN orders o ON o.order_id = oi.order_id
             WHERE oi.ean IN ({vraagtekens})""", EANS):
        regels[r["order_item_id"]] = {
            "oid": r["order_id"],
            "dag": r["order_date"] or (r["order_placed_datetime"] or "")[:10],
            "tijd": r["order_placed_datetime"] or "",
            "ean": r["ean"],
            "titel": r["title"] or "",
            "aantal": int(r["quantity"] or 0),
            "verzonden": int(r["quantity_shipped"] or 0),
            "geannuleerd": int(r["quantity_cancelled"] or 0),
            "prijs": float(r["unit_price"] or 0),
            "commissie": float(r["commission"] or 0),
            "ff": r["fulfilment_method"] or "",
            "land": (r["ship_country"] or "").upper(),
        }
        orders[r["order_id"]] = {"dag": r["order_date"],
                                 "land": (r["ship_country"] or "").upper()}
    ruw["orderregels"] = regels
    ruw["orders"] = orders

    # ------------------------------------------------------------ retours
    kol = kolommen(conn, "return_items")
    verwerkt_kol = ("processed_date" if "processed_date" in kol
                    else "processing_datetime" if "processing_datetime" in kol
                    else None)
    retours = {}
    for r in conn.execute(f"SELECT * FROM return_items WHERE ean IN ({vraagtekens})",
                          EANS):
        verwerkt = (r[verwerkt_kol] or "") if verwerkt_kol else ""
        retours[r["rma_id"]] = {
            "rid": r["return_id"],
            "oid": r["order_id"] or "",
            "ean": r["ean"],
            "aangemeld": r["registration_datetime"] or r["return_date"] or "",
            "verwacht": int(r["expected_quantity"] or 0),
            "terug": int(r["returned_quantity"] or 0),
            "reden": r["main_reason"] or "",
            "detail": r["detailed_reason"] or "",
            "toelichting": r["customer_comments"] or "",
            "afgehandeld": bool(r["handled"]),
            "uitkomst": r["handling_result"] or "",
            "resultaat": r["processing_result"] or "",
            "verwerkt": verwerkt,
        }
    ruw["retours"] = retours

    # ------------------------------------------------------- advertenties
    ads = defaultdict(lambda: [0.0, 0, 0])
    try:
        for r in conn.execute(f"""
                SELECT cost_date, ean, SUM(cost) c, SUM(impressions) i, SUM(clicks) k
                  FROM ad_costs
                 WHERE ean IN ({vraagtekens}) AND source = 'api'
                 GROUP BY cost_date, ean""", EANS):
            ads[f"{r['cost_date']}|{r['ean']}"] = [round(float(r["c"] or 0), 4),
                                                   int(r["i"] or 0), int(r["k"] or 0)]
    except sqlite3.OperationalError:
        print("  (geen tabel ad_costs - advertentiehistorie wordt overgeslagen)")
    ruw["ads"] = dict(ads)

    # ----------------------------------------------------------- facturen
    # Per maand samengevat en weggeschreven als één "overgezette" factuur. Bol
    # factureert per periode (meerdere per maand); die losse facturen kennen we
    # hier niet meer, maar het TOTAAL per maand wel. Zodra de workflow de echte
    # facturen van een maand ophaalt, vervangt hij deze samenvatting.
    facturen = {}

    def vak(maand):
        return facturen.setdefault("overgezet:" + maand,
                                   {"maand": maand, "posten": {}, "aantal": {},
                                    "winkel": {"verzend": 0.0, "pickpack": 0.0,
                                               "stuks": 0.0}})

    for r in conn.execute(f"""
            SELECT invoice_month, ean, transaction_type, SUM(amount) bedrag
              FROM invoice_lines
             WHERE ean IN ({vraagtekens}) AND invoice_month IS NOT NULL
             GROUP BY invoice_month, ean, transaction_type""", EANS):
        vak(r["invoice_month"])["posten"][f"{r['ean']}|{r['transaction_type']}"] = \
            round(float(r["bedrag"] or 0), 4)

    # Het aantal stuks dat bol factureerde, per maand per artikel. Dat is de
    # noemer voor het tarief per stuk en de maatstaf of we een maand volledig
    # kennen; zie rekenen.py.
    for r in conn.execute(f"""
            SELECT invoice_month, ean,
                   SUM(CASE WHEN transaction_type = 'TURNOVER' THEN quantity
                            WHEN transaction_type = 'CORRECTION_TURNOVER' THEN -quantity
                            ELSE 0 END) aantal
              FROM invoice_lines
             WHERE ean IN ({vraagtekens}) AND invoice_month IS NOT NULL
             GROUP BY invoice_month, ean""", EANS):
        if r["aantal"]:
            vak(r["invoice_month"])["aantal"][r["ean"]] = round(float(r["aantal"]), 3)
    # WINKELBREDE fulfilmenttotalen: alleen drie getallen per maand (verzenden,
    # pick&pack, gefactureerde stuks). Verzendkosten staan op de factuur zonder
    # EAN, dus zonder deze totalen kent het dashboard ze niet - zie rekenen.py.
    # Er gaat geen enkel ander artikel mee in het bestand: dit zijn optellingen,
    # geen regels.
    for r in conn.execute("""
            SELECT invoice_month m,
              SUM(CASE WHEN transaction_type = 'SHIPMENT_LABEL'
                         OR LOWER(COALESCE(description,'')) LIKE '%verzend%'
                         OR LOWER(COALESCE(description,'')) LIKE '%pakketzegel%'
                       THEN -amount ELSE 0 END) verzend,
              SUM(CASE WHEN transaction_type = 'PICK_PACK'
                         OR LOWER(COALESCE(description,'')) LIKE '%pick%'
                       THEN -amount ELSE 0 END) pickpack,
              SUM(CASE WHEN transaction_type = 'TURNOVER'
                       THEN quantity ELSE 0 END) stuks
            FROM invoice_lines WHERE invoice_month IS NOT NULL GROUP BY 1"""):
        if not r["stuks"]:
            continue
        vak(r["m"])["winkel"] = {"verzend": round(float(r["verzend"] or 0), 2),
                                 "pickpack": round(float(r["pickpack"] or 0), 2),
                                 "stuks": round(float(r["stuks"]), 1)}
    ruw["facturen"] = facturen

    # ----------------------------------------------------------- voorraad
    voorraad, voorraadlog = {}, {}
    try:
        for r in conn.execute(f"""
                SELECT ean, snapshot_date, regular_stock, graded_stock, title
                  FROM inventory_snapshots WHERE ean IN ({vraagtekens})
                 ORDER BY snapshot_date""", EANS):
            voorraad[r["ean"]] = {"bol": int(r["regular_stock"] or 0),
                                  "graded": int(r["graded_stock"] or 0),
                                  "dag": r["snapshot_date"],
                                  "titel": r["title"] or ""}
            voorraadlog[f"{r['snapshot_date']}|{r['ean']}"] = int(r["regular_stock"] or 0)
    except sqlite3.OperationalError:
        pass
    ruw["voorraad"] = voorraad
    ruw["voorraadlog"] = voorraadlog

    # ------------------------------------------------ producten + voorraad
    kol_cp = kolommen(conn, "cost_prices")
    producten, eigen_voorraad = {}, {}
    for r in conn.execute(f"SELECT * FROM cost_prices WHERE ean IN ({vraagtekens})",
                          EANS):
        titel = r["bol_title"] if "bol_title" in kol_cp and r["bol_title"] else r["title"]
        producten[r["ean"]] = {
            "naam": korte_naam(titel),
            "titel": (titel or "")[:120],
            "kostprijs": round(float(r["cost_price"] or 0), 4),
            "btw": float(r["vat_rate"] or 21),
        }
        plek = (r["stock_location"] if "stock_location" in kol_cp else "thuis") or "thuis"
        eigen = int(r["own_stock"] or 0) if "own_stock" in kol_cp else 0
        eigen_voorraad[r["ean"]] = {"thuis": eigen if plek == "thuis" else 0,
                                    "fulfilment": eigen if plek == "fulfilment" else 0}
    for ean in EANS:
        producten.setdefault(ean, {"naam": ean[-4:], "titel": "",
                                   "kostprijs": 0.0, "btw": 21.0})
        eigen_voorraad.setdefault(ean, {"thuis": 0, "fulfilment": 0})

    instellingen = {"producten": producten, "voorraad": eigen_voorraad, "btw": 21}

    # ------------------------------------------------------------ wegschrijven
    berekend = rekenen.bereken(ruw, instellingen)
    payload = dict(berekend)
    payload["ruw"] = ruw
    payload["bijgewerkt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["fouten"] = []
    grootte = kluis.schrijf_bestand(uit, payload, args.sleutel)

    t = berekend["totaal"]
    print(f"\nDatabase : {db_pad}")
    print(f"Geschreven: {uit}  ({grootte / 1024:.0f} kB versleuteld)")
    print(f"Bestelregels {len(regels)}, retouren {len(retours)}, "
          f"advertentiedagen {len(ruw['ads'])}, factuurmaanden {len(ruw['facturen'])}")
    print(f"Omzet excl. EUR {t['omzet_excl']:.2f} | winst EUR {t['winst']:.2f} | "
          f"{t['stuks']} stuks | {t['bestellingen']} bestellingen")
    print("\n--- Zet dit als GitHub-secret INSTELLINGEN (alles in één regel) ---")
    print(json.dumps(instellingen, ensure_ascii=False, separators=(",", ":")))
    print("--- einde ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
