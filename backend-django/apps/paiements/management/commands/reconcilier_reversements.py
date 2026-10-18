"""Réconciliation des reversements « en cours » avec le fournisseur.

    python manage.py reconcilier_reversements
        même travail que la tâche planifiée (lot, alertes de blocage)
    python manage.py reconcilier_reversements --reference REV-...
        un seul reversement en cours, sans condition d'âge
    python manage.py reconcilier_reversements --reference REV-... --lecture-seule
        affiche l'état renvoyé par le fournisseur, n'écrit rien

Un reversement que le fournisseur ne tranche pas se résout à la main
(docs/MODULE_PAIEMENTS.md, « Réconciliation »).
"""

from django.core.management.base import BaseCommand, CommandError

from apps.paiements import reversements
from apps.paiements.fournisseurs import FournisseurInconnu, obtenir_fournisseur
from apps.paiements.fournisseurs.base import (
    ErreurFournisseur,
    NotificationInvalide,
    OperationNonSupportee,
    TransactionIntrouvable,
)
from apps.paiements.models import Reversement
from apps.paiements.tasks import reconcilier_reversements


class Command(BaseCommand):
    help = "Redemande au fournisseur l'état des reversements en cours (tous, ou un seul avec --reference)."

    def add_arguments(self, parser):
        parser.add_argument("--reference", help="Référence du reversement (REV-...).")
        parser.add_argument("--lecture-seule", action="store_true",
                            help="Affiche l'état renvoyé par le fournisseur sans rien écrire (avec --reference).")

    def handle(self, *args, reference=None, lecture_seule=False, **options):
        if reference is None:
            if lecture_seule:
                raise CommandError("--lecture-seule s'utilise avec --reference.")
            self.stdout.write(reconcilier_reversements())
            return
        reversement = Reversement.objects.filter(reference=reference).first()
        if reversement is None:
            raise CommandError(f"Reversement {reference} introuvable.")
        if reversement.statut != Reversement.Statut.EN_COURS:
            raise CommandError(f"Reversement {reference} « {reversement.get_statut_display()} » : "
                               "seul un reversement en cours se réconcilie.")
        if lecture_seule:
            self.stdout.write(self.lire(reversement))
            return
        issue, change = reversements.reconcilier_reversement(reversement)
        reversement.refresh_from_db()
        self.stdout.write(f"{reference} : issue « {issue} », statut {reversement.statut} "
                          f"({'changé' if change else 'inchangé'}).")

    def lire(self, reversement):
        entete = f"{reversement.reference} ({reversement.fournisseur or 'aucun fournisseur'})"
        try:
            etat = obtenir_fournisseur(reversement.fournisseur).verifier_transfert(reversement)
        except (FournisseurInconnu, OperationNonSupportee, ErreurFournisseur, TransactionIntrouvable,
                NotificationInvalide) as erreur:
            return f"{entete} : {type(erreur).__name__} ({erreur}). Rien n'a été écrit."
        return (f"{entete} : {etat.statut}, montant confirmé {etat.montant} {etat.devise} "
                f"(à verser : {int(reversement.montant_a_verser)} {reversements.DEVISE_TRANSFERT}), "
                f"référence du fournisseur {etat.identifiant_externe or 'absente'}. Rien n'a été écrit.")
