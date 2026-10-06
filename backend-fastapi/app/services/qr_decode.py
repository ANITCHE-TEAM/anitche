"""Décodage du contenu d'un QR de passeport ou d'un code saisi à la main
(docs/MODULE_SCAN_QR.md).

FastAPI dit seulement SI le contenu désigne un passeport ANITCHE, et
LEQUEL (code normalisé). Il ne certifie rien : aucune lecture de base,
aucun appel à Django. L'existence du passeport, la certification, le
comptage et le journal des scans restent à Django
(GET /api/passeports/verifier/<code>/), appelé ensuite par le frontend.

Fonctions pures, sans état. Règles, dans cet ordre :

1. code saisi : espaces et tirets retirés, ASCII seulement, puis « PAS »,
   4 chiffres et 8 hexadécimaux (casse ignorée) -> forme canonique ;
2. sinon, contenu qui ne commence pas par un schéma (« xxx: ») ->
   code_passeport_invalide ;
3. URL piégée (espace, caractère de contrôle ou invisible, antislash,
   identifiants, hôte non ASCII, port invalide) ou d'une autre origine
   (schéma, hôte, port) que FRONTEND_BASE_URL -> qr_non_anitche ;
4. bonne origine, mais chemin autre que <chemin de base>/qr/verifier/<code>
   (casse ignorée, « / » final permis, paramètres et fragment ignorés) ->
   lien_non_passeport.

Origine comparée champ par champ après urlsplit, jamais par préfixe :
« https://anitche.com.evil.example » et « https://anitche.com@evil.example »
commencent par « https://anitche.com ».
"""
import re
import unicodedata
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import SplitResult, urlsplit

# Page publique de vérification du frontend : même chemin que Django
# (CHEMIN_VERIFICATION_PUBLIQUE, backend-django/apps/passeport_qr/models.py).
VERIFICATION_PATH = "/qr/verifier/"

# Format de Django (generer_code_passeport) : PAS-<année>-<8 hexadécimaux
# majuscules>. Classes explicites et re.ASCII : ni chiffres Unicode (« ２ »),
# ni équivalences de casse Unicode (« ſ » ~ « s »).
CODE_PATTERN = re.compile(r"PAS-[0-9]{4}-[0-9A-F]{8}", re.ASCII)
COMPACT_CODE_PATTERN = re.compile(r"PAS([0-9]{4})([0-9A-F]{8})", re.ASCII)
SCHEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:", re.ASCII)

# Saisie du code imprimé sous le QR : espaces (dont insécables) et tirets
# (dont les tirets typographiques des claviers et traitements de texte :
# ‐ ‑ ‒ – — −) ignorés.
TYPED_CODE_SEPARATORS = MappingProxyType(
    str.maketrans("", "", " \t\r\n\u00a0\u202f-\u2010\u2011\u2012\u2013\u2014\u2212")
)

DEFAULT_PORTS = MappingProxyType({"http": 80, "https": 443})


class QrRefusal(StrEnum):
    """Motif de refus, renvoyé tel quel au frontend (`errors.code`)."""

    INVALID_CODE = "code_passeport_invalide"
    NOT_ANITCHE = "qr_non_anitche"
    NOT_PASSPORT_LINK = "lien_non_passeport"


# Constantes immuables (aucun état de module). Jamais la saisie dans le
# message : elle n'est recopiée nulle part.
REFUSAL_MESSAGES = MappingProxyType({
    QrRefusal.INVALID_CODE: "Code passeport invalide. Format attendu : PAS-AAAA-XXXXXXXX.",
    QrRefusal.NOT_ANITCHE: "Ce QR code ne provient pas d'ANITCHE.",
    QrRefusal.NOT_PASSPORT_LINK: "Ce lien ANITCHE n'est pas un passeport produit.",
})


class QrRefused(Exception):
    def __init__(self, refusal: QrRefusal):
        super().__init__(refusal.value)
        self.refusal = refusal

    @property
    def message(self) -> str:
        return REFUSAL_MESSAGES[self.refusal]


def decode(qr_data: str, frontend_base_url: str) -> str:
    """Code canonique « PAS-AAAA-XXXXXXXX » désigné par le contenu, ou
    QrRefused. N'atteste PAS que le passeport existe."""
    text = qr_data.strip()
    code = normalize_typed_code(text)
    if code is not None:
        return code
    if not SCHEME_PATTERN.match(text):
        raise QrRefused(QrRefusal.INVALID_CODE)
    return _code_from_url(text, frontend_base_url)


def normalize_typed_code(text: str) -> str | None:
    """Forme canonique d'un code saisi, ou None."""
    compact = text.translate(TYPED_CODE_SEPARATORS)
    # ASCII vérifié AVANT les majuscules : "ſ".upper() == "S",
    # "ı".upper() == "I".
    if not compact.isascii():
        return None
    match = COMPACT_CODE_PATTERN.fullmatch(compact.upper())
    return f"PAS-{match[1]}-{match[2]}" if match else None


def verification_url(code: str, frontend_base_url: str) -> str:
    """URL de la page de vérification : même construction que Django
    (PasseportProduit.url_verification_publique)."""
    return frontend_base_url.rstrip("/") + VERIFICATION_PATH + code


def _is_suspicious(char: str) -> bool:
    # Un navigateur lit « \ » comme « / », retire tabulations et sauts de
    # ligne : l'hôte qu'il ouvrirait différerait de celui lu par urlsplit.
    # Catégories C : contrôle, format (espace sans chasse, inversion de sens
    # d'écriture), privées, non attribuées.
    return char == "\\" or char.isspace() or unicodedata.category(char).startswith("C")


def _origin(parts: SplitResult) -> tuple[str, str | None, int | None]:
    port = parts.port  # ValueError si non numérique ou hors bornes
    return parts.scheme, parts.hostname, DEFAULT_PORTS.get(parts.scheme) if port is None else port


def _code_from_url(url: str, frontend_base_url: str) -> str:
    if any(_is_suspicious(char) for char in url):
        raise QrRefused(QrRefusal.NOT_ANITCHE)
    try:
        parts = urlsplit(url)
        origin = _origin(parts)
    except ValueError:  # port invalide, crochets IPv6 mal formés...
        raise QrRefused(QrRefusal.NOT_ANITCHE) from None

    base = urlsplit(frontend_base_url)
    if "@" in parts.netloc or not parts.netloc.isascii() or origin != _origin(base):
        raise QrRefused(QrRefusal.NOT_ANITCHE)

    prefix = base.path.rstrip("/").lower() + VERIFICATION_PATH
    path = parts.path
    if not path.isascii() or not path.lower().startswith(prefix):
        raise QrRefused(QrRefusal.NOT_PASSPORT_LINK)
    code = path[len(prefix):].removesuffix("/").upper()
    if not CODE_PATTERN.fullmatch(code):
        raise QrRefused(QrRefusal.NOT_PASSPORT_LINK)
    return code
