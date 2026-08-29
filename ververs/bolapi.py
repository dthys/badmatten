"""
Slanke bol.com-client voor het badmatten-dashboard.

Alleen wat dit dashboard nodig heeft: bestellingen, retouren, facturen,
voorraad en advertentiekosten. Geen pip-installatie nodig, alleen de
standaardbibliotheek.

Auth: OAuth2 client_credentials tegen https://login.bol.com/token
Docs: https://api.bol.com/retailer/public/Retailer-API/v10/
"""

import base64
import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://login.bol.com/token?grant_type=client_credentials"
BASE_URL = "https://api.bol.com"
ACCEPT_V10 = "application/vnd.retailer.v10+json"
ACCEPT_ADV_V11 = "application/vnd.advertiser.v11+json"


class BolFout(Exception):
    def __init__(self, status, bericht, body=None):
        super().__init__(f"[{status}] {bericht}")
        self.status = status
        self.bericht = bericht
        self.body = body


class Client:
    """
    Eén client voor beide API's. `request` regelt token, ritme en herkansingen.

    De rem is bewust ingebouwd en niet optioneel: de factuur-endpoints van bol
    hebben een veel strengere limiet dan de rest, en een 429 kost in de praktijk
    drie kwartier minuut aan wachten. Vooraf pauzeren is goedkoper dan achteraf
    een 429 uitzitten.
    """

    def __init__(self, client_id, client_secret, log=None):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self._token = None
        self._verloopt = 0.0
        self._laatste = 0.0
        self.per_seconde = 4.0
        self.factuurpauze = 1.5
        self.adspauze = 0.5
        self.log = log or (lambda m: None)
        self.aanroepen = 0

    # ------------------------------------------------------------------ auth

    def _nieuw_token(self):
        creds = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        basic = base64.b64encode(creds).decode("ascii")
        req = urllib.request.Request(
            TOKEN_URL, data=b"", method="POST",
            headers={"Authorization": f"Basic {basic}",
                     "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 401:
                raise BolFout(401, "client_id of client_secret klopt niet.", body)
            raise BolFout(e.code, "Geen toegangstoken gekregen.", body)
        except urllib.error.URLError as e:
            raise BolFout(0, f"Netwerkfout bij login.bol.com: {e.reason}")
        self._token = payload["access_token"]
        # bol-tokens leven 299 seconden; 30 seconden eerder vernieuwen.
        self._verloopt = time.time() + int(payload.get("expires_in", 299)) - 30

    def _token_nu(self):
        if not self._token or time.time() >= self._verloopt:
            self._nieuw_token()
        return self._token

    def sleutels_kloppen(self):
        self._nieuw_token()
        return True

    # --------------------------------------------------------------- request

    def _rem(self):
        wacht = (1.0 / self.per_seconde) - (time.time() - self._laatste)
        if wacht > 0:
            time.sleep(wacht)
        self._laatste = time.time()

    def request(self, pad, params=None, method="GET", accept=ACCEPT_V10,
                body=None, pogingen=4):
        schoon = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = BASE_URL + pad
        if schoon:
            url += "?" + urllib.parse.urlencode(schoon)

        data = None
        headers = {"Accept": accept}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        # BOL EIST EEN CONTENT-TYPE OOK OP EEN POST ZONDER BODY.
        #
        # Dit stond hier eerst alleen bij een body erbij, en dat kostte precies
        # de advertentiekosten: het aanvragen van een bulkrapport is een POST
        # zonder body, en bol antwoordde met 400 zonder verdere uitleg. De rest
        # van het dashboard liep gewoon door, dus het zag eruit als 'die week
        # niet geadverteerd' in plaats van als een fout.
        if body is not None or method in ("POST", "PUT", "PATCH"):
            headers["Content-Type"] = accept

        laatste_fout = None
        for poging in range(pogingen):
            self._rem()
            headers["Authorization"] = f"Bearer {self._token_nu()}"
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
            try:
                self.aanroepen += 1
                with urllib.request.urlopen(req, timeout=90) as resp:
                    ruw = resp.read()
                    if not ruw:
                        return None
                    return json.loads(ruw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                tekst = e.read().decode("utf-8", "replace")
                if e.code == 404:
                    return None
                if e.code == 401:
                    # Token verlopen terwijl we bezig waren: opnieuw halen mag
                    # altijd, bol heeft de aanvraag zeker niet uitgevoerd.
                    self._token = None
                    laatste_fout = BolFout(401, "Niet geautoriseerd.", tekst)
                    continue
                if e.code == 429 or e.code >= 500:
                    wacht = min(60, 2 ** poging * 5)
                    self.log(f"bol gaf {e.code}; {wacht}s wachten en opnieuw")
                    time.sleep(wacht)
                    laatste_fout = BolFout(e.code, "Tijdelijke fout bij bol.", tekst)
                    continue
                uitleg = tekst.strip().replace("\n", " ")[:300]
                raise BolFout(e.code, f"Aanvraag geweigerd door bol: {uitleg}", tekst)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                laatste_fout = BolFout(0, f"Netwerkfout: {getattr(e, 'reason', e)}")
                time.sleep(3)
        raise laatste_fout or BolFout(0, "Aanvraag mislukt.")

    # ---------------------------------------------------------- bestellingen

    def orders_pagina(self, pagina=1, status="ALL", fulfilment="ALL",
                      laatste_wijziging=None):
        """
        GET /retailer/orders

        fulfilment=ALL is essentieel: bol zet die parameter standaard op FBR en
        laat dan stilzwijgend elke Logistiek-via-bol-bestelling weg.
        """
        data = self.request("/retailer/orders", {
            "page": pagina, "fulfilment-method": fulfilment, "status": status,
            "latest-change-date": laatste_wijziging})
        return (data or {}).get("orders", []) or []

    def order(self, order_id):
        return self.request(f"/retailer/orders/{order_id}")

    # --------------------------------------------------------------- retours

    def retours_pagina(self, pagina=1, afgehandeld=None, fulfilment=None):
        data = self.request("/retailer/returns", {
            "page": pagina,
            "handled": None if afgehandeld is None else str(bool(afgehandeld)).lower(),
            "fulfilment-method": fulfilment})
        return (data or {}).get("returns", []) or []

    # -------------------------------------------------------------- facturen

    def facturen(self, van, tot):
        """GET /retailer/invoices - bol staat hoogstens 32 dagen per aanvraag toe."""
        time.sleep(self.factuurpauze)
        data = self.request("/retailer/invoices",
                            {"period-start-date": van, "period-end-date": tot})
        if isinstance(data, list):
            return data
        return (data or {}).get("invoiceListItems", []) or []

    def factuurspecificatie(self, factuur_id, pagina=1):
        """
        De transacties op een factuur: TURNOVER en COMMISSION per bestelling en
        EAN, plus de logistieke kosten. Gaat twee jaar terug en is daarmee de
        enige bron voor historie ouder dan de drie maanden van /orders.
        """
        time.sleep(self.factuurpauze)
        return self.request(f"/retailer/invoices/{factuur_id}/specification",
                            {"page": pagina})

    # -------------------------------------------------------------- voorraad

    def voorraad_pagina(self, pagina=1):
        """GET /retailer/inventory - de FBB-voorraad bij bol, per EAN."""
        data = self.request("/retailer/inventory", {"page": pagina})
        return (data or {}).get("inventory", []) or []


class AdsClient(Client):
    """
    Dezelfde OAuth, maar met de ADVERTEERDER-sleutels. Die staan los van de
    gewone retailer-sleutels; heeft de verkoper ze niet, dan blijven de
    advertentiekosten leeg en zegt het dashboard dat erbij.
    """

    _BULK = "/advertiser/reporting/bulk-reports"

    def dagtotaal(self, van, tot):
        """Kosten op adverteerder-niveau, zonder product. Voor 'vandaag'."""
        time.sleep(self.adspauze)
        return self.request(
            "/advertiser/sponsored-products/reporting/performance/advertiser",
            {"period-start-date": van, "period-end-date": tot},
            accept=ACCEPT_ADV_V11)

    def bulkrapport(self, van, tot, soort="AD_PERFORMANCE",
                    wacht_seconden=240, interval=5):
        """
        Het AD_PERFORMANCE-rapport: kosten PER DAG PER EAN. Bol zet het
        asynchroon klaar, dus: aanvragen, pollen, downloaden. Periode maximaal
        ~35 dagen, tot twaalf maanden terug.

        Geeft een lijst CSV-regels (dicts) terug, of [] als er niets is.
        """
        time.sleep(self.adspauze)
        gestart = self.request(
            self._BULK,
            {"report-type": soort, "start-date": van, "end-date": tot},
            method="POST", accept=ACCEPT_ADV_V11) or {}
        proces = gestart.get("processStatusId") or gestart.get("entityId") or ""
        if not proces:
            return []

        rapport_id = None
        einde = time.time() + wacht_seconden
        while time.time() < einde:
            st = self.request(f"/shared/process-status/{proces}",
                              accept=ACCEPT_V10) or {}
            status = st.get("status") or ""
            if status == "SUCCESS":
                rapport_id = st.get("entityId") or ""
                break
            if status in ("FAILURE", "TIMEOUT", "ERROR"):
                raise BolFout(0, f"Advertentierapport mislukt: {status}")
            time.sleep(interval)
        if not rapport_id:
            raise BolFout(0, "Advertentierapport was niet op tijd klaar.")

        meta = self.request(f"{self._BULK}/{rapport_id}",
                            accept=ACCEPT_ADV_V11) or {}
        url = meta.get("url")
        if not url:
            return []
        # Losse download-URL bij Google Cloud Storage, zonder auth.
        with urllib.request.urlopen(url, timeout=180) as resp:
            tekst = resp.read().decode("utf-8", "replace")
        return list(csv.DictReader(io.StringIO(tekst)))
