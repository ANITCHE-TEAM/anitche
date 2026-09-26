from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commandes', '0002_alter_commande_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='commande',
            name='coupon_code',
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name='commande',
            name='montant_remise',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
