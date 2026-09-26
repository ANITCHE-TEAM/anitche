from django.apps import AppConfig


class FideliteConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.fidelite'
    verbose_name = 'Fidélité & Récompenses'

    def ready(self):
        import apps.fidelite.signals
