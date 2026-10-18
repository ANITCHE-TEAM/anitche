"""Résolution manuelle d'un reversement « en cours » que le fournisseur ne
tranche pas, après vérification dans son tableau de bord.

    python manage.py resoudre_reversement REV-... non-parti --par admin@... --motif "..."
        aucun transfert n'est parti : le reversement redevient disponible
    python manage.py resoudre_reversement REV-... verse --par admin@... --motif "..." --reference-externe ...
        le transfert a eu lieu : le reversement est versé, le vendeur notifié

Commande séparée de reconcilier_reversements, qui ne fait que redemander
l'état au fournisseur : celle-ci décide à sa place. Diagnostic d'abord :
reconcilier_reversements --reference REV-... --lecture-seule.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.paiements import reversements
from apps.paiements.models import Reversement
from apps.utilisateurs.models import Utilisateur


class Command(BaseCommand):
    help = "Résout à la main un reversement en cours (non-parti → disponible, verse → versé)."

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Référence du reversement (REV-...).")
        parser.add_argument("decision", choices=[reversements.NON_PARTI, reversements.VERSE_CONFIRME])
        parser.add_argument("--par", required=True, help="Adresse e-mail de l'administrateur qui décide.")
        parser.add_argument("--motif", required=True, help="Ce qui a été vérifié chez le fournisseur.")
        parser.add_argument("--reference-externe", default="",
                            help="Référence du transfert chez le fournisseur (obligatoire pour « verse »).")

    def handle(self, *args, reference, decision, par, motif, reference_externe="", **options):
        reversement = Reversement.objects.filter(reference=reference).first()
        if reversement is None:
            raise CommandError(f"Reversement {reference} introuvable.")
        administrateur = Utilisateur.objects.filter(email__iexact=par.strip()).first()
        if administrateur is None:
            raise CommandError(f"Aucun utilisateur {par}.")
        try:
            resolu = reversements.resoudre_transfert_incertain(reversement, administrateur, decision, motif,
                                                               reference_externe)
        except reversements.ErreurVersement as erreur:
            raise CommandError(str(erreur))
        self.stdout.write(f"{resolu.reference} : {resolu.get_statut_display()} (décision « {decision} »).")
