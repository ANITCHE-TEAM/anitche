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
