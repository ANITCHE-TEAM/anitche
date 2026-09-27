"""Frais vendeur : barème en vigueur et calcul figé par ligne de commande.

commission   = prix unitaire × quantité × taux / 100, arrondie au franc
               (demi-franc au-dessus), sur le prix AVANT remise : un coupon
               est supporté par ANITCHE, jamais par le vendeur ;
frais fixes  = frais fixe par article × quantité ;
net vendeur  = brut − commission − frais fixes, jamais négatif (sur un
               article très bon marché, les frais sont plafonnés au prix).
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q
from django.utils import timezone

from .models import BaremeFrais


class BaremeIntrouvable(Exception):
    """Aucun barème de la plateforme n'est en vigueur (configuration)."""


def bareme_en_vigueur(boutique, moment=None):
    """Barème propre à la boutique s'il y en a un en vigueur, sinon celui
    de la plateforme. Le plus récent l'emporte en cas de chevauchement."""
    moment = moment or timezone.now()
    en_vigueur = BaremeFrais.objects.filter(
        Q(date_fin__isnull=True) | Q(date_fin__gt=moment), date_debut__lte=moment,
    ).order_by("-date_debut")
    bareme = en_vigueur.filter(boutique=boutique).first() or en_vigueur.filter(boutique__isnull=True).first()
    if bareme is None:
        raise BaremeIntrouvable("Aucun barème de frais de la plateforme n'est en vigueur.")
    return bareme


def calculer_frais_ligne(prix_unitaire, quantite, bareme):
    """Valeurs à figer dans le CommandeItem."""
    brut = Decimal(prix_unitaire) * quantite
    commission = (brut * bareme.taux_commission / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    commission = min(commission, brut)
    frais_fixes = min(Decimal(bareme.frais_fixe_article) * quantite, brut - commission)
    return {
        "taux_commission": bareme.taux_commission,
        "frais_fixe_unitaire": bareme.frais_fixe_article,
        "montant_commission": commission,
        "montant_frais_fixes": frais_fixes,
        "montant_net_vendeur": brut - commission - frais_fixes,
    }
