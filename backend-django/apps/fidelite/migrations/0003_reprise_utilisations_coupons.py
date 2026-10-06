"""Reprise des coupons existants dans le nouveau modèle d'utilisation.

- Coupon nominatif : 1 utilisation au plus ; déjà utilisé → compteur à 1.
- Coupon public : illimité (chaque client une fois). Il était marqué
  « utilisé » au premier usage, ce qui le fermait à tous les autres clients :
  il est rouvert, et les clients qui l'ont déjà utilisé (commandes portant
  son code) reçoivent leur UtilisationCoupon.
"""

from django.db import migrations


def reprendre(apps, schema_editor):
    CouponReduction = apps.get_model("fidelite", "CouponReduction")
    UtilisationCoupon = apps.get_model("fidelite", "UtilisationCoupon")
    Commande = apps.get_model("commandes", "Commande")

    for coupon in CouponReduction.objects.all():
        utilisations = {}
        for client_id, groupe_id in (
            Commande.objects.filter(coupon_code__iexact=coupon.code)
            .order_by("created_at")
            .values_list("client_id", "groupe_id")
        ):
            utilisations.setdefault(client_id, groupe_id)
        for client_id, groupe_id in utilisations.items():
            UtilisationCoupon.objects.get_or_create(coupon=coupon, client_id=client_id, defaults={"groupe_id": groupe_id})

        if coupon.client_id:
            coupon.utilisations_max = 1
            coupon.nombre_utilisations = 1 if (coupon.est_utilise or utilisations) else 0
            coupon.est_utilise = coupon.nombre_utilisations >= 1
        else:
            coupon.utilisations_max = None
            coupon.nombre_utilisations = len(utilisations)
            coupon.est_utilise = False
        coupon.save(update_fields=["utilisations_max", "nombre_utilisations", "est_utilise"])


class Migration(migrations.Migration):

    dependencies = [
        ("fidelite", "0002_gains_utilisations_contraintes"),
    ]

    operations = [
        migrations.RunPython(reprendre, migrations.RunPython.noop),
    ]
