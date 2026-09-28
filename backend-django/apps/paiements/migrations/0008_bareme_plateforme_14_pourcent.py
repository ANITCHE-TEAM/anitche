"""Nouveau barème de la plateforme (décision d'équipe, septembre 2026).

14 % du prix effectif de l'article (promotion comprise), plus un frais fixe
par article de 100 FCFA si ce prix est inférieur ou égal à 3 000 FCFA,
200 FCFA au-delà. Montants TVA incluse.

- Le barème de la plateforme en vigueur (boutique vide) est clôturé :
  date_fin = maintenant, rien d'autre. Ses valeurs ne sont jamais
  modifiées, et les commandes passées gardent les frais figés dans leurs
  CommandeItem (12 % + 200 FCFA).
- Le nouveau barème commence au même instant : ni trou, ni chevauchement.
- Les barèmes propres à une boutique ne sont pas touchés : ils restent
  prioritaires tant qu'ils sont en vigueur.
- Idempotente : identifiant fixe, rien n'est fait si le barème existe déjà.
- Retour arrière : le nouveau barème est supprimé (aucune ligne n'y fait
  référence, les frais sont copiés dans CommandeItem) et le barème
  clôturé par cette migration retrouve une date_fin vide.
"""

import uuid
from decimal import Decimal

from django.db import migrations
from django.db.models import Q
from django.utils import timezone

ID_BAREME_14 = uuid.UUID("3f0c1a52-7d4e-4b8a-9c14-0e5b2d6a8f71")
LIBELLE = "Plateforme : 14 % + 100 FCFA/article jusqu'à 3 000 FCFA (200 FCFA au-delà), TVA incluse"


def creer_bareme_14(apps, schema_editor):
    BaremeFrais = apps.get_model("paiements", "BaremeFrais")
    if BaremeFrais.objects.filter(pk=ID_BAREME_14).exists():
        return
    maintenant = timezone.now()
    BaremeFrais.objects.filter(
        Q(date_fin__isnull=True) | Q(date_fin__gt=maintenant),
        boutique__isnull=True,
        date_debut__lt=maintenant,
    ).update(date_fin=maintenant)
    BaremeFrais.objects.create(
        pk=ID_BAREME_14,
        boutique=None,
        libelle=LIBELLE,
        taux_commission=Decimal("14.00"),
        frais_fixe_article=200,
        seuil_petit_article=3000,
        frais_fixe_petit_article=100,
        date_debut=maintenant,
    )


def supprimer_bareme_14(apps, schema_editor):
    BaremeFrais = apps.get_model("paiements", "BaremeFrais")
    bareme = BaremeFrais.objects.filter(pk=ID_BAREME_14).first()
    if bareme is None:
        return
    BaremeFrais.objects.filter(boutique__isnull=True, date_fin=bareme.date_debut).update(date_fin=None)
    bareme.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("paiements", "0007_bareme_frais_petit_article"),
    ]

    operations = [
        migrations.RunPython(creer_bareme_14, supprimer_bareme_14),
    ]
