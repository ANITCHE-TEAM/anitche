"""Reprise des données existantes après la refonte du module paiements.

1. Paiement.commandes : reprise de metadata.commandes_couvertes (ou, à
   défaut, de la commande ou du groupe ciblé).
2. Statuts retirés : « a_rembourser » → validé + un Remboursement à traiter
   par entrée de metadata.remboursements_dus ; « rembourse » → validé.
3. Paiement à la livraison supprimé : les paiements « espece_livraison »
   encore actifs passent annulés.
4. Clés internes retirées de metadata (désormais dans des relations).
5. Barème de frais de la plateforme par défaut : 12 % + 200 FCFA par
   article, modifiable ensuite par l'administration (API admin/baremes).
"""

import uuid
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

CLES_INTERNES = ("commandes_couvertes", "remboursements_dus")


def reference(prefixe):
    return f"{prefixe}-{timezone.now().year}-{uuid.uuid4().hex[:10].upper()}"


def reprendre(apps, schema_editor):
    Paiement = apps.get_model("paiements", "Paiement")
    Remboursement = apps.get_model("paiements", "Remboursement")
    BaremeFrais = apps.get_model("paiements", "BaremeFrais")
    Commande = apps.get_model("commandes", "Commande")

    for paiement in Paiement.objects.all():
        metadata = dict(paiement.metadata or {})
        identifiants = metadata.get("commandes_couvertes") or []
        commandes = list(Commande.objects.filter(pk__in=identifiants))
        if not commandes and paiement.commande_id:
            commandes = [paiement.commande]
        if not commandes and paiement.groupe_commande_id:
            commandes = list(Commande.objects.filter(groupe_id=paiement.groupe_commande_id))
        paiement.commandes.set(commandes)

        if paiement.statut == "a_rembourser":
            for du in metadata.get("remboursements_dus", []):
                commande = Commande.objects.filter(pk=du.get("commande")).first()
                if commande is not None:
                    Remboursement.objects.get_or_create(
                        paiement=paiement, commande=commande, retour=None,
                        defaults={
                            "reference": reference("RMB"),
                            "montant": Decimal(str(du.get("montant") or commande.montant_total)),
                            "motif": "commande_annulee",
                        },
                    )
            paiement.statut = "valide"
        elif paiement.statut == "rembourse":
            paiement.statut = "valide"

        if paiement.methode == "espece_livraison" and paiement.statut == "en_attente":
            paiement.statut = "annule"
            metadata["motif_annulation"] = "paiement à la livraison supprimé"

        paiement.metadata = {cle: valeur for cle, valeur in metadata.items() if cle not in CLES_INTERNES}
        paiement.save(update_fields=["statut", "metadata"])

    if not BaremeFrais.objects.filter(boutique__isnull=True).exists():
        BaremeFrais.objects.create(
            libelle="Barème par défaut de la plateforme",
            taux_commission=Decimal("12.00"),
            frais_fixe_article=200,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("paiements", "0003_fournisseurs_remboursements_frais_reversements"),
    ]

    operations = [
        migrations.RunPython(reprendre, migrations.RunPython.noop),
    ]
