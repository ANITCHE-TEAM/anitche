from django.db import migrations, models


class Migration(migrations.Migration):
    """Après 0006 : toutes les demandes en attente ont une échéance."""

    dependencies = [
        ("retours", "0006_echeances_demandes_existantes"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='demanderetour',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('statut', 'demande'), _negated=True), ('date_limite_vendeur__isnull', False), _connector='OR'), name='retour_demande_avec_echeance_vendeur'),
        ),
        migrations.AddConstraint(
            model_name='demanderetour',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('statut__in', ('refuse_vendeur', 'refuse_moderation')), _negated=True), ('date_limite_contestation__isnull', False), _connector='OR'), name='retour_refus_avec_echeance_contestation'),
        ),
    ]
