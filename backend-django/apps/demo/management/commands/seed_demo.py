"""Données de démonstration pour le développement du frontend.

    python manage.py seed_demo           # crée les données (sans effet si elles existent déjà)
    python manage.py seed_demo --reset   # supprime les données de démo, puis les recrée

Contenu : catégories, 4 boutiques de vendeurs validés (produits, variantes,
stock, images générées), un compte par rôle, des commandes à plusieurs
statuts (livrée, en préparation, payée, annulée), un retour, des points de
fidélité crédités, un coupon issu de points et un ticket support. Détail et
comptes : docs/GUIDE_FRONTEND.md.

Refusée hors développement : DEBUG désactivé, settings de production ou
fournisseur de paiement autre que le simulé. L'app apps.demo n'est de toute
façon installée que par dev.py et test.py.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError, Q

from apps.catalogue.models import ImageProduit
from apps.commandes.models import Commande, GroupeCommande
from apps.demo import donnees
from apps.demo.scenario import ErreurScenario, ScenarioDemo
from apps.paiements.models import AjustementVendeur, BaremeFrais, Paiement, Remboursement, Reversement
from apps.panier.models import Panier
from apps.passeport_qr.models import PasseportProduit
from apps.support.models import SupportTicket
from apps.utilisateurs.models import DocumentKYC, Utilisateur
from apps.vendeurs.models import Boutique

BANDEAU = "=" * 72


def verifier_environnement_de_demo():
    """Lève CommandError si l'environnement n'est pas un environnement de développement."""
    raisons = []
    if not settings.DEBUG:
        raisons.append("DEBUG est désactivé")
    if str(getattr(settings, 'SETTINGS_MODULE', '')).endswith('.prod'):
        raisons.append("les settings de production sont chargés")
    if getattr(settings, 'PAIEMENT_FOURNISSEUR', '') != 'simule':
        raisons.append("le fournisseur de paiement n'est pas le fournisseur simulé")
    if raisons:
        raise CommandError(
            "seed_demo est réservée au développement : " + ", ".join(raisons) + ". Aucune donnée n'a été écrite."
        )


def utilisateurs_demo():
    return Utilisateur.objects.filter(email__iendswith=f'@{donnees.DOMAINE_DEMO}')


def fichiers_demo(utilisateurs):
    """Fichiers enregistrés pour les comptes de démo (images, logos, pièces KYC)."""
    champs = []
    for image in ImageProduit.objects.filter(produit__boutique__proprietaire__in=utilisateurs):
        champs.append(image.image)
    for boutique in Boutique.objects.filter(proprietaire__in=utilisateurs):
        champs += [boutique.logo, boutique.banniere]
    for dossier in DocumentKYC.objects.filter(utilisateur__in=utilisateurs):
        champs += [dossier.piece_identite_recto, dossier.piece_identite_verso, dossier.selfie]
    return [(champ.storage, champ.name) for champ in champs if champ]


def supprimer_donnees_demo():
    """Supprime les données rattachées aux comptes @demo.anitche.test, et elles seules.

    L'historique financier est protégé (PROTECT) : il est supprimé ici dans
    l'ordre des dépendances, toujours restreint aux comptes et boutiques de
    démo. Si une donnée hors démo en dépend encore (par exemple une commande
    d'un vrai compte dans une boutique de démo), la suppression échoue et la
    transaction annule tout. Les catégories sont conservées (référentiel partagé).
    """
    utilisateurs = utilisateurs_demo()
    if not utilisateurs.exists():
        return 0, []
    boutiques = Boutique.objects.filter(proprietaire__in=utilisateurs)
    commandes = Commande.objects.filter(client__in=utilisateurs)
    fichiers = fichiers_demo(utilisateurs)
    nombre = utilisateurs.count()

    SupportTicket.objects.filter(
        Q(created_by__in=utilisateurs) | Q(product__boutique__in=boutiques)
    ).delete()
    PasseportProduit.objects.filter(boutique__in=boutiques).delete()
    AjustementVendeur.objects.filter(Q(boutique__in=boutiques) | Q(commande__in=commandes)).delete()
    Remboursement.objects.filter(commande__in=commandes).delete()
    Reversement.objects.filter(Q(boutique__in=boutiques) | Q(commande__in=commandes)).delete()
    Paiement.objects.filter(client__in=utilisateurs).delete()
    commandes.delete()
    GroupeCommande.objects.filter(client__in=utilisateurs).delete()
    BaremeFrais.objects.filter(boutique__in=boutiques).delete()
    # SET_NULL vers l'utilisateur : sans ceci, les paniers resteraient orphelins.
    Panier.objects.filter(utilisateur__in=utilisateurs).delete()
    utilisateurs.delete()
    return nombre, fichiers


class Command(BaseCommand):
    help = "Crée les données de démonstration (développement uniquement). --reset : les recrée."

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset', action='store_true',
            help="Supprime d'abord les données de démo (comptes @demo.anitche.test et ce qui leur est rattaché).",
        )

    def handle(self, *args, reset=False, **options):
        verifier_environnement_de_demo()

        fichiers = []
        try:
            with transaction.atomic():
                if reset:
                    nombre, fichiers = supprimer_donnees_demo()
                    self.stdout.write(f"Données de démo supprimées ({nombre} compte(s)).")
                if utilisateurs_demo().exists():
                    self.stdout.write(self.style.WARNING(
                        "Données de démo déjà présentes : rien n'a été modifié (--reset pour les recréer)."
                    ))
                else:
                    ScenarioDemo(journal=self.stdout.write).executer()
                    self.stdout.write(self.style.SUCCESS("Données de démo créées."))
        except ProtectedError as erreur:
            raise CommandError(
                "Suppression impossible : des données hors démo dépendent des données de démo "
                f"({len(erreur.protected_objects)} objet(s), ex. {next(iter(erreur.protected_objects))}). "
                "Rien n'a été supprimé."
            )
        except ErreurScenario as erreur:
            raise CommandError(f"Création interrompue, rien n'a été enregistré : {erreur}")

        # Fichiers supprimés seulement une fois la suppression en base validée.
        for stockage, nom in fichiers:
            stockage.delete(nom)

        self.afficher_comptes()

    def afficher_comptes(self):
        comptes = [
            ('Administrateur', donnees.ADMIN['email']),
            ('Support', donnees.SUPPORT['email']),
            ('Client', donnees.CLIENT['email']),
            ('Livreur', donnees.LIVREUR['email']),
        ] + [
            (f"Vendeur « {definition['nom']} »", definition['vendeur']['email'])
            for definition in donnees.BOUTIQUES
        ]
        lignes = [
            BANDEAU,
            "DEV UNIQUEMENT : comptes de démonstration (mot de passe public, jamais en production)",
            BANDEAU,
            *(f"  {role:<32} {email}" for role, email in comptes),
            f"  {'Mot de passe (tous les comptes)':<32} {donnees.MOT_DE_PASSE_DEMO}",
            BANDEAU,
        ]
        self.stdout.write("\n".join(lignes))
