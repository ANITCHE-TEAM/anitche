import os
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

# Signatures binaires (magic bytes) des formats acceptés. Sert à vérifier que
# le CONTENU du fichier correspond réellement à son extension déclarée —
# renommer un .php ou un .html en .png ne suffit pas à passer ce contrôle.
_SIGNATURES = {
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".webp": [b"RIFF"],  # suivi de 4 octets de taille puis b"WEBP" (vérifié séparément)
    ".pdf": [b"%PDF-"],
}


@deconstructible
class ValidateurFichierSecurise:
    """Validateur réutilisable pour sécuriser les uploads de fichiers et d'images.

    Vérifie, dans l'ordre : l'extension déclarée, la taille, PUIS la signature
    binaire réelle du contenu (défense contre un fichier malveillant renommé
    avec une extension autorisée)."""

    EXTENSIONS_AUTORISEES_DEFAUT = [".jpg", ".jpeg", ".png", ".webp", ".pdf"]
    TAILLE_MAX_MO_DEFAUT = 5  # 5 Mo max

    def __init__(self, extensions=None, taille_max_mo=None):
        self.extensions = extensions or self.EXTENSIONS_AUTORISEES_DEFAUT
        self.taille_max_mo = taille_max_mo or self.TAILLE_MAX_MO_DEFAUT

    def __call__(self, value):
        # 1. Vérification de l'extension déclarée
        ext = os.path.splitext(value.name)[1].lower()
        if ext not in self.extensions:
            raise ValidationError(
                f"Format de fichier non autorisé ({ext}). Formats acceptés : {', '.join(self.extensions)}."
            )

        # 2. Vérification de la taille maximale
        taille_octets = value.size
        taille_max_octets = self.taille_max_mo * 1024 * 1024
        if taille_octets > taille_max_octets:
            raise ValidationError(
                f"Le fichier dépasse la taille maximale autorisée de {self.taille_max_mo} Mo."
            )

        # 3. Vérification du contenu réel (magic bytes) : l'extension seule
        # ne prouve rien, un attaquant peut renommer n'importe quel fichier.
        entete = value.read(16)
        value.seek(0)  # remettre le curseur au début pour la suite du pipeline (save, etc.)

        signatures_attendues = _SIGNATURES.get(ext)
        if signatures_attendues and not any(entete.startswith(sig) for sig in signatures_attendues):
            raise ValidationError(
                f"Le contenu du fichier ne correspond pas à un format {ext} valide."
            )
        if ext == ".webp" and (len(entete) < 12 or entete[8:12] != b"WEBP"):
            raise ValidationError("Le contenu du fichier ne correspond pas à un format .webp valide.")


validateur_image_standard = ValidateurFichierSecurise(
    extensions=[".jpg", ".jpeg", ".png", ".webp"],
    taille_max_mo=5,
)

validateur_document_kyc = ValidateurFichierSecurise(
    extensions=[".jpg", ".jpeg", ".png", ".pdf"],
    taille_max_mo=10,
)

# Même profil que le KYC (images + PDF, 10 Mo) mais nommé indépendamment du
# contexte métier KYC — pour les pièces jointes génériques (support, etc.)
# où seul le format compte, pas la finalité.
validateur_document_standard = validateur_document_kyc
