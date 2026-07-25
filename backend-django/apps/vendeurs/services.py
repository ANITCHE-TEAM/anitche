"""Transitions du cycle de vie vendeur.

Toute la logique de décision (valider / refuser) est centralisée ici pour que
l'API, le django-admin et d'éventuelles tâches Celery appliquent exactement les
mêmes règles.

Règles appliquées :
  - la demande doit être en attente (`statut_kyc = en_attente`), état posé par
    `Utilisateur.soumettre_demande_vendeur()` côté module utilisateurs ;
  - une validation passe le compte à `statut_kyc = valide` ET `role = vendeur`,
    dans la même transaction : jamais de rôle et de statut désaccordés ;
  - un refus passe le compte à `statut_kyc = refuse` et laisse le rôle intact
    (l'utilisateur reste client) ;
  - la décision est tracée dans le dossier KYC existant
    (`date_traitement`, `commentaire_admin`).
"""

from django.db import transaction
from django.utils import timezone

from apps.utilisateurs.models import Role, StatutKYC, Utilisateur

#: Rôles depuis lesquels un compte peut devenir vendeur. Le modèle utilisateur
#: ne porte qu'un seul rôle : on refuse d'écraser celui d'un livreur ou d'un
#: administrateur (cas non prévu par le projet, à arbitrer si le besoin arrive).
ROLES_ELIGIBLES_VENDEUR = (Role.CLIENT, Role.VENDEUR)


class TransitionVendeurImpossible(Exception):
    """La décision demandée ne correspond pas à l'état courant du compte."""


def _tracer_decision(utilisateur, commentaire):
    dossier = getattr(utilisateur, 'dossier_kyc', None)
    if dossier is None:
        return
    dossier.date_traitement = timezone.now()
    if commentaire:
        dossier.commentaire_admin = commentaire
    dossier.save(update_fields=['date_traitement', 'commentaire_admin'])


def _charger_demande_verrouillee(utilisateur):
    compte = Utilisateur.objects.select_for_update().get(pk=utilisateur.pk)
    if compte.statut_kyc != StatutKYC.EN_ATTENTE:
        raise TransitionVendeurImpossible(
            "Aucune demande vendeur en attente pour ce compte "
            f"(statut actuel : {compte.get_statut_kyc_display()})."
        )
    return compte


@transaction.atomic
def valider_demande_vendeur(utilisateur, commentaire=''):
    """Valide la demande : le compte devient vendeur avec un KYC validé."""
    compte = _charger_demande_verrouillee(utilisateur)

    if compte.role not in ROLES_ELIGIBLES_VENDEUR:
        raise TransitionVendeurImpossible(
            f"Un compte « {compte.get_role_display()} » ne peut pas être converti en vendeur."
        )

    compte.statut_kyc = StatutKYC.VALIDE
    compte.role = Role.VENDEUR
    compte.save(update_fields=['statut_kyc', 'role'])

    _tracer_decision(compte, commentaire)
    return compte


@transaction.atomic
def refuser_demande_vendeur(utilisateur, commentaire=''):
    """Refuse la demande : le KYC passe à refusé, le rôle n'est pas modifié.

    Le compte reste client et peut soumettre une nouvelle demande
    (`soumettre_demande_vendeur()` autorise la reprise depuis l'état refusé).
    """
    compte = _charger_demande_verrouillee(utilisateur)

    compte.statut_kyc = StatutKYC.REFUSE
    compte.save(update_fields=['statut_kyc'])

    _tracer_decision(compte, commentaire)
    return compte
