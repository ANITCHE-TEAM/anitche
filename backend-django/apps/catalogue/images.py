"""Miniatures des images produit : WebP de LARGEUR_MINIATURE px de large au
plus (jamais agrandi), qualité QUALITE_WEBP, orientation EXIF appliquée.

Générées à l'envoi (ImageProduitListCreateView) et, pour les images déjà
enregistrées, par la commande `generer_miniatures`. Une génération qui
échoue laisse `miniature` vide : l'image reste enregistrée et le client
affiche l'original.
"""

import logging
from io import BytesIO
from pathlib import PurePosixPath

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import ImageProduit

LARGEUR_MINIATURE = 480
QUALITE_WEBP = 75

logger = logging.getLogger(__name__)


def contenu_miniature(fichier):
    """Octets WebP de la miniature d'un fichier image ouvert en binaire."""
    with Image.open(fichier) as originale:
        image = ImageOps.exif_transpose(originale)
        # WebP : RGB, ou RGBA pour garder la transparence (PNG, palette).
        transparente = image.mode in ('RGBA', 'LA', 'PA') or 'transparency' in image.info
        image = image.convert('RGBA' if transparente else 'RGB')
        if image.width > LARGEUR_MINIATURE:
            hauteur = max(1, round(image.height * LARGEUR_MINIATURE / image.width))
            image = image.resize((LARGEUR_MINIATURE, hauteur), Image.Resampling.LANCZOS)
        tampon = BytesIO()
        image.save(tampon, format='WEBP', quality=QUALITE_WEBP)
    return tampon.getvalue()


def generer_miniature(image_produit):
    """Crée le fichier de la miniature et l'enregistre sur la ligne (une
    seule colonne mise à jour, sans repasser par ImageProduit.save). Renvoie
    True si la miniature existe, False si la génération a échoué."""
    try:
        with image_produit.image.open('rb') as fichier:
            contenu = contenu_miniature(fichier)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        logger.warning("Miniature impossible pour l'image produit %s.", image_produit.pk, exc_info=True)
        return False
    nom = f"{PurePosixPath(image_produit.image.name).stem}.webp"
    image_produit.miniature.save(nom, ContentFile(contenu), save=False)
    ImageProduit.objects.filter(pk=image_produit.pk).update(miniature=image_produit.miniature.name)
    return True
