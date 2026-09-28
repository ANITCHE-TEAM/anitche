"""Décodage des QR des passeports (docs/MODULE_SCAN_QR.md).

POST /qr/scan : contenu brut lu par la caméra, ou code saisi à la main ->
code normalisé et URL de la page de vérification du frontend. AUCUNE
certification : la page appelle Django (GET /api/passeports/verifier/<code>/),
qui certifie, compte et journalise le scan.

Route publique, limitée par IP (scope `qr_scan`). Aucune lecture de base,
aucun appel à Django, aucun état. La saisie n'est jamais recopiée, ni dans
la réponse ni dans les journaux.
"""
from fastapi import APIRouter, Depends, Request

from app.core.errors import CodedHTTPException, Error
from app.core.rate_limit import rate_limit
from app.modeles.scan_qr import DemandeScanQR, ReponseScanQR
from app.services import qr_decode

router = APIRouter(prefix="/qr", tags=["Scan QR"])


@router.post(
    "/scan",
    response_model=ReponseScanQR,
    summary="Décoder un QR ou un code passeport saisi (sans le certifier)",
    dependencies=[Depends(rate_limit("qr_scan"))],
    responses={400: {
        "model": Error,
        "description": "Contenu refusé, errors.code : code_passeport_invalide (ni code ni URL), "
        "qr_non_anitche (autre site ou URL piégée : ne jamais l'ouvrir), lien_non_passeport "
        "(page ANITCHE qui n'est pas un passeport). Corps invalide : errors.qr_data.",
    }},
)
async def scanner_qr(demande: DemandeScanQR, request: Request):
    """Code saisi (espaces, tirets et casse tolérés) ou URL de vérification
    de FRONTEND_BASE_URL. Un 200 ne dit pas que le passeport existe :
    ouvrir `url_verification_publique`, dont la page appelle Django."""
    frontend_base_url = request.app.state.settings.frontend_base_url
    try:
        code = qr_decode.decode(demande.qr_data, frontend_base_url)
    except qr_decode.QrRefused as refused:
        raise CodedHTTPException(400, refused.message, refused.refusal.value) from None
    return ReponseScanQR(
        code_passeport=code,
        url_verification_publique=qr_decode.verification_url(code, frontend_base_url),
    )
