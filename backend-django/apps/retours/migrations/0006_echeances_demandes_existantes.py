from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def poser_echeances(apps, schema_editor):
    """Demandes en attente d'une réponse du vendeur : l'échéance court à
    partir de la migration (au plus tôt), sinon la première exécution de la
    tâche escaladerait d'un coup toutes les demandes anciennes. Les demandes
    rejetées restent définitives : aucune contestation rétroactive (leur
    reversement a déjà repris). Aucun historique n'est reconstitué."""
    DemandeRetour = apps.get_model("retours", "DemandeRetour")
    delai = timedelta(hours=settings.RETOUR_DELAI_REPONSE_VENDEUR_HEURES)
    plancher = timezone.now() + delai
    for demande in DemandeRetour.objects.filter(statut="demande", date_limite_vendeur__isnull=True).only(
        "pk", "date_creation",
    ):
        demande.date_limite_vendeur = max(demande.date_creation + delai, plancher)
        demande.save(update_fields=["date_limite_vendeur"])


class Migration(migrations.Migration):

    dependencies = [
        ("retours", "0005_circuit_decision"),
    ]

    operations = [
        migrations.RunPython(poser_echeances, migrations.RunPython.noop),
    ]
