import unicodedata

from django.db import migrations, models


def _normaliser(nom):
    # Copie figée de models.normaliser_nom_boutique : une migration ne doit
    # pas dépendre du code applicatif, qui peut évoluer après coup.
    decompose = unicodedata.normalize('NFKD', nom.casefold())
    return ''.join(caractere for caractere in decompose if caractere.isalnum())


def remplir_nom_normalise(apps, schema_editor):
    Boutique = apps.get_model('vendeurs', 'Boutique')
    deja_vus = {}
    conflits = []
    for boutique in Boutique.objects.order_by('pk').only('pk', 'nom'):
        nom_normalise = _normaliser(boutique.nom)
        if not nom_normalise or nom_normalise in deja_vus:
            conflits.append((boutique.pk, boutique.nom, deja_vus.get(nom_normalise)))
            continue
        deja_vus[nom_normalise] = boutique.pk
        Boutique.objects.filter(pk=boutique.pk).update(nom_normalise=nom_normalise)

    if conflits:
        # Arrêt explicite plutôt qu'un renommage automatique : le choix du
        # nom à conserver est une décision métier, pas une décision de
        # migration. Corriger les noms listés puis relancer `migrate`.
        details = '; '.join(
            f"id={pk} « {nom} »" + (f" (proche de id={proche})" if proche else " (aucune lettre ni chiffre)")
            for pk, nom, proche in conflits
        )
        raise RuntimeError(f"Noms de boutique à corriger avant migration : {details}")


class Migration(migrations.Migration):

    dependencies = [
        ('vendeurs', '0003_boutique_est_suspendue'),
    ]

    operations = [
        migrations.AddField(
            model_name='boutique',
            name='nom_normalise',
            field=models.CharField(editable=False, max_length=120, null=True),
        ),
        migrations.RunPython(remplir_nom_normalise, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='boutique',
            name='nom_normalise',
            field=models.CharField(editable=False, max_length=120, unique=True),
        ),
    ]
