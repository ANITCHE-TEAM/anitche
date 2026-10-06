from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Briques partagées (chiffrement, validateurs, IP client, erreurs) et
    réglages transverses de DRF appliqués au démarrage."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'

    def ready(self):
        from rest_framework.fields import BooleanField, empty

        # Multipart / formulaire : DRF lit un booléen ABSENT comme False (héritage
        # des cases à cocher HTML, qui n'envoient rien quand elles sont décochées).
        # Pour une API, cela créait une boutique fermée dès qu'un logo imposait
        # le multipart, et un PUT multipart fermait la boutique. Absent = non
        # fourni, comme en JSON : défaut du modèle (ou défaut déclaré du champ)
        # à la création, valeur actuelle conservée en mise à jour.
        # Vérifié par config/tests.py::BooleensMultipartTests.
        BooleanField.default_empty_html = empty

        # Type des fichiers servis par FileResponse, déduit de l'extension par
        # mimetypes. Python 3.12 ne connaît pas .webp (ajouté en 3.13), et
        # l'image Docker n'a pas forcément /etc/mime.types : une photo WebP
        # partirait sinon en application/octet-stream, contrairement au schéma
        # (config/schema.py, FICHIER_IMAGE). Sans effet si le type est déjà connu.
        import mimetypes

        mimetypes.add_type('image/webp', '.webp')
