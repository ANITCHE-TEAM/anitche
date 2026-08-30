import re
from typing import Dict, Any, Optional
from app.modeles.scan_qr import DemandeScanQR, ReponseScanPasseport

# Registre en mémoire des passeports certifiés pour validation ultra-rapide
PASSEPORTS_REGISTRE: Dict[str, Dict[str, Any]] = {
    "PAS-2026-TIASSALE01": {
        "produit_id": 1,
        "produit_nom": "Robe Baoulé Traditionnelle",
        "boutique_nom": "Atelier Baoulé Tiassalé",
        "artisan_createur": "Maître Tisserand Kouassi",
        "origine_geographique": "Tiassalé, Région de l'Agnéby-Tiassa, Côte d'Ivoire",
        "materiaux_utilises": "Pagne Baoulé 100% coton teinté aux pigments végétaux naturels, fils de chaîne indigo",
        "statut_certification": "certifie_authentique",
        "statut_certification_libelle": "Certifié Authentique ANITCHE",
        "url_verification": "https://anitche.ci/qr/verifier/PAS-2026-TIASSALE01",
        "nb_scans": 12,
    },
    "PAS-2026-BASSAM02": {
        "produit_id": 4,
        "produit_nom": "Sac à Main Cuir et Pagne Kita",
        "boutique_nom": "Maroquinerie Bassam",
        "artisan_createur": "Atelier Cuir & Kita Bassam",
        "origine_geographique": "Grand-Bassam, Ville Historique, Côte d'Ivoire",
        "materiaux_utilises": "Cuir de zébu pleine fleur tanné végétal, empiècements tissés main",
        "statut_certification": "label_local",
        "statut_certification_libelle": "Fabriqué en Côte d'Ivoire (Label Artisanal)",
        "url_verification": "https://anitche.ci/qr/verifier/PAS-2026-BASSAM02",
        "nb_scans": 5,
    },
    "PAS-2026-MASQUE03": {
        "produit_id": 3,
        "produit_nom": "Masque Baoulé en Bois d'Iroko",
        "boutique_nom": "Atelier Baoulé Tiassalé",
        "artisan_createur": "Sculpteur N'Goran",
        "origine_geographique": "Tiassalé, Côte d'Ivoire",
        "materiaux_utilises": "Bois d'Iroko massif, cire d'abeille et kaolin",
        "statut_certification": "certifie_authentique",
        "statut_certification_libelle": "Certifié Authentique ANITCHE",
        "url_verification": "https://anitche.ci/qr/verifier/PAS-2026-MASQUE03",
        "nb_scans": 27,
    },
}


class ServiceScanQR:
    """Service de décodage et vérification instantanée des passeports numériques QR."""

    @classmethod
    def extraire_code_passeport(cls, qr_data: str) -> Optional[str]:
        if not qr_data:
            return None

        data_clean = qr_data.strip()

        # Recherche de motif 'PAS-XXXX-XXXXXXXX' dans l'URL ou la chaîne brute
        pattern = r"(PAS-\d{4}-[A-Za-z0-9]+)"
        match = re.search(pattern, data_clean, re.IGNORECASE)
        if match:
            return match.group(1).upper()

        if data_clean.startswith("PAS-"):
            return data_clean.upper()

        return None

    @classmethod
    def decoder_et_verifier(cls, demande: DemandeScanQR) -> ReponseScanPasseport:
        code = cls.extraire_code_passeport(demande.qr_data)

        if not code:
            return ReponseScanPasseport(
                valide=False,
                code_passeport=demande.qr_data[:30],
                produit_id=None,
                produit_nom="Produit Inconnu",
                boutique_nom="Non Répertoriée",
                origine_geographique="Origine non garantie",
                materiaux_utilises="Non documentés",
                statut_certification="non_certifie",
                statut_certification_libelle="Non Certifié / Code Invalide",
                url_verification="",
                nb_scans=0,
                message="Le QR code scanné ne correspond à aucun passeport numérique ANITCHE reconnu.",
            )

        passeport = PASSEPORTS_REGISTRE.get(code)

        if not passeport:
            # Passeport reconnu au format PAS mais non enregistré
            return ReponseScanPasseport(
                valide=False,
                code_passeport=code,
                produit_id=None,
                produit_nom="Passeport Non Répertorié",
                boutique_nom="Inconnue",
                origine_geographique="Inconnue",
                materiaux_utilises="Inconnus",
                statut_certification="non_certifie",
                statut_certification_libelle="Certificat Non Trouvé",
                url_verification=f"https://anitche.ci/qr/verifier/{code}",
                nb_scans=0,
                message=f"Le code '{code}' est un identifiant conforme mais non actif dans la base d'authenticité.",
            )

        # Incrément du compteur de scans
        passeport["nb_scans"] += 1

        return ReponseScanPasseport(
            valide=True,
            code_passeport=code,
            produit_id=passeport["produit_id"],
            produit_nom=passeport["produit_nom"],
            boutique_nom=passeport["boutique_nom"],
            artisan_createur=passeport.get("artisan_createur"),
            origine_geographique=passeport["origine_geographique"],
            materiaux_utilises=passeport["materiaux_utilises"],
            statut_certification=passeport["statut_certification"],
            statut_certification_libelle=passeport["statut_certification_libelle"],
            url_verification=passeport["url_verification"],
            nb_scans=passeport["nb_scans"],
            message="Authenticité et traçabilité vérifiées avec succès par ANITCHE.",
        )
