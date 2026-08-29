"""
Versleuteling van het gegevensbestand.

De webapp staat op GitHub Pages en is daarmee technisch openbaar: iedereen die
de URL kent kan het bestand downloaden. Daarom staat er niets leesbaars in.
data.json is een AES-256-GCM-blok; de sleutel zit in het #-deel van de link die
je doorstuurt en komt dus nooit bij GitHub terecht (browsers sturen het
fragment niet mee in het verzoek).

De browser doet exact hetzelfde met WebCrypto, dus deze twee moeten gelijk
blijven: PBKDF2-SHA256, 200.000 rondes, 32 bytes sleutel, 12 bytes IV.
"""

import base64
import gzip
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

RONDES = 200_000
# Onder deze grens slaan we het inpakken over. gzip vraagt DecompressionStream
# in de browser (Safari 16.4+); bij een klein bestand is dat de moeite en het
# risico niet waard.
GZIP_VANAF = 250_000


def _sleutel(wachtwoord, zout):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=zout,
                     iterations=RONDES)
    return kdf.derive(wachtwoord.encode("utf-8"))


def versleutel(obj, wachtwoord):
    rauw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ingepakt = len(rauw) >= GZIP_VANAF
    if ingepakt:
        rauw = gzip.compress(rauw, 6)
    zout = os.urandom(16)
    iv = os.urandom(12)
    ct = AESGCM(_sleutel(wachtwoord, zout)).encrypt(iv, rauw, None)
    return {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "rondes": RONDES,
        "gz": ingepakt,
        "zout": base64.b64encode(zout).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "data": base64.b64encode(ct).decode("ascii"),
    }


def ontsleutel(blok, wachtwoord):
    zout = base64.b64decode(blok["zout"])
    iv = base64.b64decode(blok["iv"])
    ct = base64.b64decode(blok["data"])
    rauw = AESGCM(_sleutel(wachtwoord, zout)).decrypt(iv, ct, None)
    if blok.get("gz"):
        rauw = gzip.decompress(rauw)
    return json.loads(rauw.decode("utf-8"))


def lees_bestand(pad, wachtwoord):
    """Geeft None als het bestand er niet is of niet te openen valt."""
    if not os.path.exists(pad):
        return None
    with open(pad, "r", encoding="utf-8") as f:
        blok = json.load(f)
    return ontsleutel(blok, wachtwoord)


def schrijf_bestand(pad, obj, wachtwoord):
    blok = versleutel(obj, wachtwoord)

    # ZELFCONTROLE: meteen weer openen met hetzelfde wachtwoord.
    #
    # Dit is er niet uit voorzichtigheid maar omdat het echt misging: een
    # lusvariabele die toevallig ook `sleutel` heette overschreef het
    # wachtwoord, waarna het bestand met een datum-EAN-combinatie versleuteld
    # werd. Alles leek te lukken - tot de app zei dat de code niet klopte, en
    # de volgende ronde weigerde door te gaan omdat hij de historie niet kon
    # openen. Kost een paar tienden van een seconde; voorkomt een bestand dat
    # niemand meer open krijgt.
    controle = ontsleutel(blok, wachtwoord)
    if controle is None:
        raise ValueError("Zelfcontrole mislukt: het bestand is niet leesbaar.")

    tijdelijk = pad + ".tmp"
    with open(tijdelijk, "w", encoding="utf-8") as f:
        json.dump(blok, f, separators=(",", ":"))
    os.replace(tijdelijk, pad)
    return os.path.getsize(pad)
