"""Contrat du décodage QR (docs/MODULE_SCAN_QR.md).

Aucun certificat ici : la réponse désigne un passeport, elle n'atteste pas
qu'il existe. Le certificat est celui de Django
(GET /api/passeports/verifier/<code>/).
"""
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator
from pydantic_core import PydanticCustomError

# URL de prod : 49 caractères ; 512 laissent la place à des paramètres de
# suivi, sans laisser recopier ni traiter un contenu arbitraire.
MAX_QR_DATA_LENGTH = 512
BLANK_MESSAGE = "Ce champ ne peut être vide."  # texte de DRF


class DemandeScanQR(BaseModel):
    qr_data: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_QR_DATA_LENGTH)] = Field(
        description="Contenu brut lu dans le QR (URL de vérification) ou code saisi à la main. "
        "1 à 512 caractères, espaces de début et de fin retirés.",
        examples=["https://anitche.com/qr/verifier/PAS-2026-1A2B3C4D", "PAS 2026 1a2b3c4d"],
    )

    @field_validator("qr_data")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value:
            raise PydanticCustomError("blank", BLANK_MESSAGE)
        return value


class ReponseScanQR(BaseModel):
    code_passeport: str = Field(
        description="Code normalisé PAS-AAAA-XXXXXXXX. N'atteste PAS que le passeport existe : "
        "la page de vérification appelle Django, qui certifie.",
        examples=["PAS-2026-1A2B3C4D"],
    )
    url_verification_publique: str = Field(
        description="Page de vérification du frontend : FRONTEND_BASE_URL + /qr/verifier/ + code, "
        "même valeur que le champ de Django.",
        examples=["https://anitche.com/qr/verifier/PAS-2026-1A2B3C4D"],
    )
