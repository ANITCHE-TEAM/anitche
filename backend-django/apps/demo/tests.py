"""Commande seed_demo : contenu créé, idempotence, --reset limité aux données
de démo, et refus hors développement (y compris absence en production)."""

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from apps.catalogue.models import Categorie, Produit
from apps.commandes.models import Commande
from apps.demo import donnees
from apps.fidelite.models import CompteFidelite, CouponReduction, GainFidelite
from apps.paiements.models import Paiement
from apps.panier.models import Panier
from apps.retours.models import DemandeRetour
from apps.support.models import SupportTicket
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique


def lancer_seed(*arguments):
    sortie = StringIO()
    call_command('seed_demo', *arguments, stdout=sortie)
    return sortie.getvalue()


def comptes_demo():
    return Utilisateur.objects.filter(email__endswith=f'@{donnees.DOMAINE_DEMO}')


@override_settings(DEBUG=True)
class SeedDemoTests(TestCase):
    """Un seul passage du scénario pour la classe (lecture seule), sauf les
    tests d'idempotence et de --reset qui le relancent."""

    @classmethod
    def setUpTestData(cls):
        # Données hors démo, qui ne doivent jamais être touchées par --reset.
        cls.compte_reel = Utilisateur.objects.create_user(
            email='cliente.reelle@anitche.ci', password='TestPassword123!', nom='R', prenom='C', email_verifie=True,
        )
        cls.categorie_reelle = Categorie.objects.create(nom='Alimentation')
        cls.sortie = lancer_seed()
        cls.client_demo = Utilisateur.objects.get(email=donnees.CLIENT['email'])

    def test_un_compte_par_role_email_verifie_et_mot_de_passe_de_demo(self):
        roles = sorted(comptes_demo().values_list('role', flat=True))
        self.assertEqual(roles, sorted([
            Role.ADMIN, Role.SUPPORT, Role.CLIENT, Role.LIVREUR, *[Role.VENDEUR] * 4,
        ]))
        for compte in comptes_demo():
            self.assertTrue(compte.email_verifie)
            self.assertTrue(compte.check_password(donnees.MOT_DE_PASSE_DEMO))
        self.assertEqual(
            comptes_demo().filter(role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE).count(), 4,
        )

    def test_quatre_boutiques_publiques_avec_catalogue_achetable(self):
        boutiques = Boutique.objects.publiques().filter(proprietaire__in=comptes_demo())
        self.assertEqual(boutiques.count(), 4)
        produits = Produit.objects.filter(boutique__in=boutiques)
        self.assertEqual(produits.count(), 12)
        for produit in produits:
            self.assertTrue(produit.est_achetable)
            self.assertIsNotNone(produit.categorie)
            image = produit.images.get(est_principale=True)
            self.assertTrue(default_storage.exists(image.image.name))
            self.assertTrue(all(variante.stock.quantite_disponible >= 0 for variante in produit.variantes.all()))
        for boutique in boutiques:
            self.assertTrue(default_storage.exists(boutique.logo.name))

    def test_commandes_a_plusieurs_statuts_payees_par_le_fournisseur_simule(self):
        statuts = sorted(Commande.objects.filter(client=self.client_demo).values_list('status', flat=True))
        S = Commande.Status
        self.assertEqual(statuts, sorted([S.LIVREE, S.LIVREE, S.PREPARATION, S.CONFIRMEE, S.ANNULEE]))
        paiements = Paiement.objects.filter(client=self.client_demo, statut=Paiement.Statut.VALIDE)
        self.assertEqual(paiements.count(), 4)
        self.assertTrue(all(paiement.fournisseur == 'simule' for paiement in paiements))
        self.assertEqual(
            Commande.objects.get(client=self.client_demo, status=S.ANNULEE).motif_annulation,
            Commande.MotifAnnulation.CLIENT,
        )

    def test_retour_points_credites_coupon_et_ticket(self):
        retour = DemandeRetour.objects.get(client=self.client_demo)
        self.assertEqual(retour.statut, DemandeRetour.Statut.APPROUVE)

        # 65 points crédités (horloge décalée), 50 convertis en coupon ; la
        # commande avec un retour ouvert garde son gain en attente.
        gains = GainFidelite.objects.filter(compte__utilisateur=self.client_demo)
        self.assertEqual(gains.get(statut=GainFidelite.Statut.CREDITE).points, 65)
        self.assertEqual(gains.filter(statut=GainFidelite.Statut.EN_ATTENTE).count(), 1)
        self.assertEqual(CompteFidelite.objects.get(utilisateur=self.client_demo).solde_points, 15)
        self.assertEqual(CouponReduction.objects.filter(client=self.client_demo).count(), 1)

        ticket = SupportTicket.objects.get(created_by=self.client_demo)
        self.assertEqual(ticket.order.status, Commande.Status.PREPARATION)
        self.assertTrue(ticket.messages.filter(author__role=Role.SUPPORT).exists())

    def test_mots_de_passe_affiches_sous_bandeau_dev_uniquement(self):
        self.assertIn("DEV UNIQUEMENT", self.sortie)
        self.assertLess(self.sortie.index("DEV UNIQUEMENT"), self.sortie.index(donnees.MOT_DE_PASSE_DEMO))
        self.assertIn(donnees.ADMIN['email'], self.sortie)

    def test_idempotente(self):
        avant = (comptes_demo().count(), Commande.objects.count(), Produit.objects.count())
        sortie = lancer_seed()
        self.assertIn("déjà présentes", sortie)
        self.assertEqual((comptes_demo().count(), Commande.objects.count(), Produit.objects.count()), avant)

    def test_reset_recree_la_demo_sans_toucher_au_reste(self):
        ancien_admin = Utilisateur.objects.get(email=donnees.ADMIN['email'])
        ancienne_image = Produit.objects.filter(boutique__proprietaire__in=comptes_demo()).first().images.first()
        paniers_orphelins = Panier.objects.filter(utilisateur__isnull=True).count()

        lancer_seed('--reset')

        self.assertEqual(Panier.objects.filter(utilisateur__isnull=True).count(), paniers_orphelins)

        self.assertTrue(Utilisateur.objects.filter(pk=self.compte_reel.pk).exists())
        self.assertTrue(Categorie.objects.filter(pk=self.categorie_reelle.pk).exists())
        self.assertEqual(comptes_demo().count(), 8)
        self.assertNotEqual(Utilisateur.objects.get(email=donnees.ADMIN['email']).pk, ancien_admin.pk)
        self.assertEqual(Commande.objects.filter(client__email=donnees.CLIENT['email']).count(), 5)
        # Fichiers des anciennes données supprimés du stockage.
        self.assertFalse(default_storage.exists(ancienne_image.image.name))

    def test_reset_refuse_si_des_donnees_hors_demo_en_dependent(self):
        """Une commande d'un vrai compte dans une boutique de démo : rien n'est supprimé."""
        boutique = Boutique.objects.filter(proprietaire__in=comptes_demo()).first()
        Commande.objects.create(boutique=boutique, client=self.compte_reel, montant_total=1000)

        with self.assertRaisesMessage(CommandError, "des données hors démo dépendent"):
            lancer_seed('--reset')
        self.assertEqual(comptes_demo().count(), 8)
        self.assertTrue(Boutique.objects.filter(pk=boutique.pk).exists())


class SeedDemoRefusTests(TestCase):
    """Refus hors développement, sans rien écrire."""

    def verifier_refus(self, raison):
        with self.assertRaisesMessage(CommandError, raison):
            lancer_seed()
        self.assertFalse(comptes_demo().exists())

    def test_refus_si_debug_desactive(self):
        # Le lanceur de tests force DEBUG=False.
        self.verifier_refus("DEBUG est désactivé")

    @override_settings(DEBUG=True, SETTINGS_MODULE='config.settings.prod')
    def test_refus_avec_les_settings_de_production(self):
        self.verifier_refus("settings de production")

    @override_settings(DEBUG=True, PAIEMENT_FOURNISSEUR='cinetpay')
    def test_refus_si_le_fournisseur_n_est_pas_simule(self):
        self.verifier_refus("fournisseur simulé")

    @override_settings(DEBUG=True, PAIEMENT_FOURNISSEUR='cinetpay')
    def test_refus_du_reset_ne_supprime_rien(self):
        Utilisateur.objects.create_user(
            email=donnees.CLIENT['email'], password='x', nom='D', prenom='C',
        )
        with self.assertRaises(CommandError):
            lancer_seed('--reset')
        self.assertEqual(comptes_demo().count(), 1)


class SeedDemoAbsenteEnProductionTests(SimpleTestCase):
    def test_app_et_commande_absentes_des_settings_de_production(self):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": "x" * 50,
            "FIELD_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
            "ALLOWED_HOSTS": "api.anitche.com",
            "CORS_ALLOWED_ORIGINS": "https://anitche.com",
            "BACKEND_BASE_URL": "https://api.anitche.com",
            "PAIEMENT_FOURNISSEUR": "cinetpay",
            "CINETPAY_API_KEY": "sk_live_cle",
            "CINETPAY_API_PASSWORD": "mdp",
        }
        script = (
            "import django, json\n"
            "django.setup()\n"
            "from django.conf import settings\n"
            "from django.core.management import get_commands\n"
            "print(json.dumps({'app': 'apps.demo' in settings.INSTALLED_APPS, 'commande': 'seed_demo' in get_commands()}))\n"
        )
        resultat = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(settings.BASE_DIR), env=env, capture_output=True, text=True,
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr[-800:])
        self.assertEqual(json.loads(resultat.stdout.strip().splitlines()[-1]), {"app": False, "commande": False})
