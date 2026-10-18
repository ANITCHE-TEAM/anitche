"""Fixe l'état « côté fournisseur » d'un paiement ou d'un transfert simulé
(développement).

Rejoue une notification perdue sans l'envoyer : la réconciliation (tâches
apps.paiements.tasks.reconcilier_paiements et reconcilier_reversements, ou
l'expiration de la commande) trouvera cet état chez le fournisseur simulé et
l'appliquera.

    python manage.py simuler_etat_paiement PAY-... succes
    python manage.py simuler_etat_paiement PAY-... injoignable
    python manage.py simuler_etat_paiement PAY-... introuvable
    python manage.py simuler_etat_paiement REV-... succes   (transfert d'un reversement)

Réservée au fournisseur simulé, lui-même refusé en production.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.paiements.fournisseurs.base import ECHEC, EN_ATTENTE, SUCCES
from apps.paiements.fournisseurs.simule import INJOIGNABLE, INTROUVABLE, FournisseurSimule, definir_etat_distant
from apps.paiements.models import Paiement, Reversement


class Command(BaseCommand):
    help = ("Fixe l'état d'un paiement ou d'un transfert chez le fournisseur simulé (notification perdue, "
            "fournisseur injoignable, transaction introuvable).")

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Référence du paiement (PAY-...) ou du reversement (REV-...).")
        parser.add_argument("statut", choices=[SUCCES, ECHEC, EN_ATTENTE, INJOIGNABLE, INTROUVABLE])
        parser.add_argument("--montant", type=int,
                            help="Montant confirmé (défaut : celui du paiement, ou le montant à verser).")
        parser.add_argument("--devise", default="XOF")

    def handle(self, *args, reference, statut, montant=None, devise="XOF", **options):
        if reference.startswith("REV-"):
            objet = Reversement.objects.filter(reference=reference).first()
            du, identifiant = (int(objet.montant_a_verser), objet.reference_externe) if objet else (None, "")
        else:
            objet = Paiement.objects.filter(reference=reference).first()
            du, identifiant = (int(objet.montant), objet.transaction_id_externe or "") if objet else (None, "")
        if objet is None:
            raise CommandError(f"{reference} introuvable.")
        if objet.fournisseur != FournisseurSimule.code:
            raise CommandError(f"{reference} ouvert chez {objet.fournisseur or 'aucun fournisseur'}, "
                               "pas chez le fournisseur simulé.")
        montant = du if montant is None else montant
        definir_etat_distant(reference, statut, montant, devise, identifiant)
        self.stdout.write(f"{reference} : état simulé « {statut} » ({montant} {devise}).")
