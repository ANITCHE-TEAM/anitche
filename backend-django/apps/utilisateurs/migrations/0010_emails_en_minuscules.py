from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower


def mettre_emails_en_minuscules(apps, schema_editor):
    """Met en minuscules les emails déjà enregistrés.

    Vérifie d'abord qu'aucun groupe de comptes ne partage le même email à
    la casse près : dans ce cas la migration s'arrête avant toute écriture,
    et les doublons se traitent à la main avant de la relancer.
    """
    Utilisateur = apps.get_model('utilisateurs', 'Utilisateur')
    collisions = (
        Utilisateur.objects.values(email_minuscules=Lower('email'))
        .annotate(comptes=Count('id'))
        .filter(comptes__gt=1)
        .count()
    )
    if collisions:
        raise RuntimeError(
            f"{collisions} email(s) portés par plusieurs comptes à la casse près : "
            "aucun email n'a été modifié. Traitez ces doublons "
            "(GROUP BY LOWER(email) HAVING COUNT(*) > 1) puis relancez la migration."
        )
    Utilisateur.objects.exclude(email=Lower('email')).update(email=Lower('email'))


class Migration(migrations.Migration):

    dependencies = [
        ('utilisateurs', '0009_retrait_compte_bancaire'),
    ]

    operations = [
        # Retour arrière sans effet : la casse d'origine n'est pas conservée.
        migrations.RunPython(mettre_emails_en_minuscules, migrations.RunPython.noop),
    ]
