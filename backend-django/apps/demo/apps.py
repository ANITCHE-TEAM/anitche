from django.apps import AppConfig


class DemoConfig(AppConfig):
    """Données de démonstration (commande seed_demo).

    Installée seulement par config/settings/dev.py et test.py : absente de
    base.py, donc jamais chargée en production.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.demo'
    verbose_name = "Démonstration"
