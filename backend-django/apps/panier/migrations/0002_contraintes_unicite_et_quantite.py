from django.db import migrations, models
from django.db.models import Count


def verifier_absence_de_doublons(apps, schema_editor):
    """Arrêt explicite si des données existantes violent les nouvelles
    contraintes. Aucune fusion ni suppression automatique : décider quel
    panier ou quelle ligne conserver est une décision métier. Corriger les
    ids listés puis relancer `migrate`."""
    Panier = apps.get_model('panier', 'Panier')
    PanierItem = apps.get_model('panier', 'PanierItem')
    problemes = []

    for champ in ('utilisateur', 'session_key'):
        doublons = (
            Panier.objects.filter(**{f'{champ}__isnull': False})
            .values(champ).annotate(nb=Count('id')).filter(nb__gt=1)
        )
        for doublon in doublons:
            ids = list(Panier.objects.filter(**{champ: doublon[champ]}).values_list('id', flat=True))
            problemes.append(f"paniers en double pour {champ}={doublon[champ]} : {ids}")

    lignes_en_double = (
        PanierItem.objects.values('panier', 'variante')
        .annotate(nb=Count('id')).filter(nb__gt=1)
    )
    for doublon in lignes_en_double:
        ids = list(
            PanierItem.objects.filter(panier=doublon['panier'], variante=doublon['variante'])
            .values_list('id', flat=True)
        )
        problemes.append(
            f"lignes en double (panier={doublon['panier']}, variante={doublon['variante']}) : {ids}"
        )

    lignes_vides = list(PanierItem.objects.filter(quantite__lt=1).values_list('id', flat=True))
    if lignes_vides:
        problemes.append(f"lignes de quantité 0 : {lignes_vides}")

    if problemes:
        raise RuntimeError(
            "Données panier à corriger avant migration :\n- " + "\n- ".join(map(str, problemes))
        )


class Migration(migrations.Migration):

    dependencies = [
        ('panier', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(verifier_absence_de_doublons, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='panier',
            constraint=models.UniqueConstraint(
                condition=models.Q(utilisateur__isnull=False),
                fields=('utilisateur',),
                name='panier_unique_par_utilisateur',
            ),
        ),
        migrations.AddConstraint(
            model_name='panier',
            constraint=models.UniqueConstraint(
                condition=models.Q(session_key__isnull=False),
                fields=('session_key',),
                name='panier_unique_par_session',
            ),
        ),
        migrations.AddConstraint(
            model_name='panieritem',
            constraint=models.UniqueConstraint(
                fields=('panier', 'variante'),
                name='panier_item_unique_par_variante',
            ),
        ),
        migrations.AddConstraint(
            model_name='panieritem',
            constraint=models.CheckConstraint(
                condition=models.Q(quantite__gte=1),
                name='panier_item_quantite_positive',
            ),
        ),
    ]
