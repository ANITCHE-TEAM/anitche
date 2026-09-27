import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Intégrité de l'historique de commande : une variante produit ne doit
    plus pouvoir être supprimée tant qu'une ligne de commande y fait
    référence — voir le commentaire sur CommandeItem.variante dans
    models.py (passage de CASCADE à PROTECT).

    Aucune donnée existante n'est perdue par ce changement : seul le
    comportement à la suppression future d'une VarianteProduit change
    (le SGBD refusera désormais la suppression au lieu de la propager
    silencieusement à l'historique de commande).
    """

    dependencies = [
        ('commandes', '0003_commande_coupon_code_commande_montant_remise'),
        ('catalogue', '0002_alter_categorie_image_alter_imageproduit_image'),
    ]

    operations = [
        migrations.AlterField(
            model_name='commandeitem',
            name='variante',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='variante_article',
                to='catalogue.varianteproduit',
            ),
        ),
    ]
