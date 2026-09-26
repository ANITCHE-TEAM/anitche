from django.db import migrations


def repasser_en_standard(apps, schema_editor):
    """« Certifié Authentique ANITCHE » était la valeur par défaut, choisie
    par le vendeur lui-même, dans l'ancien code : aucun passeport existant
    n'a été certifié par l'administration. Tous repassent en « standard » ;
    l'administration réattribue le label après vérification."""
    PasseportProduit = apps.get_model('passeport_qr', 'PasseportProduit')
    PasseportProduit.objects.filter(statut_certification='certifie_authentique').update(statut_certification='standard')


class Migration(migrations.Migration):

    dependencies = [
        ('passeport_qr', '0004_desactive_par'),
    ]

    operations = [
        # Irréversible par nature : on ne sait plus lesquels étaient certifiés.
        migrations.RunPython(repasser_en_standard, migrations.RunPython.noop),
    ]
