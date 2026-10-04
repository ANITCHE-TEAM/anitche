"""Fixe l'état « côté fournisseur » d'un paiement simulé (développement).

Rejoue un webhook perdu sans l'envoyer : la réconciliation (tâche
apps.paiements.tasks.reconcilier_paiements, ou l'expiration de la commande)
trouvera cet état chez le fournisseur simulé et l'appliquera.

    python manage.py simuler_etat_paiement PAY-... succes
    python manage.py simuler_etat_paiement PAY-... injoignable

Réservée au fournisseur simulé, lui-même refusé en production.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.paiements.fournisseurs.base import ECHEC, EN_ATTENTE, SUCCES
from apps.paiements.fournisseurs.simule import INJOIGNABLE, FournisseurSimule, definir_etat_distant
from apps.paiements.models import Paiement


class Command(BaseCommand):
    help = "Fixe l'état d'un paiement chez le fournisseur simulé (webhook perdu, fournisseur injoignable)."

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Référence du paiement (PAY-...).")
        parser.add_argument("statut", choices=[SUCCES, ECHEC, EN_ATTENTE, INJOIGNABLE])
        parser.add_argument("--montant", type=int, help="Montant confirmé (défaut : celui du paiement).")
        parser.add_argument("--devise", default="XOF")

    def handle(self, *args, reference, statut, montant=None, devise="XOF", **options):
        paiement = Paiement.objects.filter(reference=reference).first()
        if paiement is None:
            raise CommandError(f"Paiement {reference} introuvable.")
        if paiement.fournisseur != FournisseurSimule.code:
            raise CommandError(f"Paiement {reference} ouvert chez {paiement.fournisseur}, pas chez le fournisseur simulé.")
        montant = int(paiement.montant) if montant is None else montant
        definir_etat_distant(reference, statut, montant, devise, paiement.transaction_id_externe or "")
        self.stdout.write(f"{reference} : état simulé « {statut} » ({montant} {devise}).")
