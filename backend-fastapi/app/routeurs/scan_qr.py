from fastapi import APIRouter
from app.modeles.scan_qr import DemandeScanQR, ReponseScanPasseport
from app.services.qr_service import ServiceScanQR

router = APIRouter(prefix="/qr", tags=["Scan & Vérification QR"])


@router.post(
    "/scan",
    response_model=ReponseScanPasseport,
    summary="Décoder et vérifier un QR code ou code passeport numérique",
)
def scanner_et_verifier(demande: DemandeScanQR):
    """Analyse la donnée scannée (URL complète ou identifiant brut) et renvoie la fiche de certification authentifiée."""
    return ServiceScanQR.decoder_et_verifier(demande)


@router.get(
    "/passeport/{code_passeport}",
    response_model=ReponseScanPasseport,
    summary="Consulter directement un passeport numérique par son code",
)
def consulter_passeport(code_passeport: str):
    """Vérifie la validité d'un code passeport au format PAS-2026-XXXXXXXX."""
    return ServiceScanQR.decoder_et_verifier(DemandeScanQR(qr_data=code_passeport))