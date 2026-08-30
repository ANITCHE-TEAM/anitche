import os
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible


@deconstructible
class ValidateurFichierSecurise:
    """Validateur réutilisable pour sécuriser les uploads de fichiers et d'images."""

    EXTENSIONS_AUTORISEES_DEFAUT = [".jpg", ".jpeg", ".png", ".webp", ".pdf"]
    TAILLE_MAX_MO_DEFAUT = 5  # 5 Mo max

    def __init__(self, extensions=None, taille_max_mo=None):
        self.extensions = extensions or self.EXTENSIONS_AUTORISEES_DEFAUT
        self.taille_max_mo = taille_max_mo or self.TAILLE_MAX_MO_DEFAUT

    def __call__(self, value):
        # 1. Vérification de l'extension
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


validateur_image_standard = ValidateurFichierSecurise(
    extensions=[".jpg", ".jpeg", ".png", ".webp"],
    taille_max_mo=5,
)

validateur_document_kyc = ValidateurFichierSecurise(
    extensions=[".jpg", ".jpeg", ".png", ".pdf"],
    taille_max_mo=10,
)
