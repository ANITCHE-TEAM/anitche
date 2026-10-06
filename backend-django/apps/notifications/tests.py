from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.commandes.models import Commande
from apps.livraison.models import Livraison
from apps.paiements.models import Paiement
from apps.paiements.services import valider_paiement
from .models import Notification, PreferenceNotification
from .services import ServiceNotification


class BaseNotificationTestCase(APITestCase):
    def setUp(self):
        # Client 1
        self.client1 = Utilisateur.objects.create_user(
            email="client1@anitche.ci",
            password="TestPassword123!",
            nom="Konan",
            prenom="Aya",
            role=Role.CLIENT,
        )

        # Client 2
        self.client2 = Utilisateur.objects.create_user(
            email="client2@anitche.ci",
            password="TestPassword123!",
            nom="Touré",
            prenom="Ali",
            role=Role.CLIENT,
        )

        # Vendeur 1
        self.vendeur1 = Utilisateur.objects.create_user(
            email="vendeur1@anitche.ci",
            password="TestPassword123!",
            nom="Kouassi",
            prenom="Jean",
            role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE,
        )
        self.boutique1 = Boutique.objects.create(
            proprietaire=self.vendeur1,
            nom="Boutique Ivoire",
            est_active=True,
        )

        # Commande pour client 1
        self.commande1 = Commande.objects.create(
            boutique=self.boutique1,
            client=self.client1,
            montant_total=Decimal("15000.00"),
            status=Commande.Status.CREEE,
        )


class NotificationAPITestCase(BaseNotificationTestCase):

    def setUp(self):
        super().setUp()
        self.notif1 = Notification.objects.create(
            destinataire=self.client1,
            titre="Bienvenue sur ANITCHE",
            message="Votre compte a été créé avec succès.",
            type_notification=Notification.TypeNotification.SYSTEME,
            est_lu=False,
        )
        self.notif2 = Notification.objects.create(
            destinataire=self.client1,
            titre="Promotion spéciale",
            message="Profitez de 10% sur l'artisanat.",
            type_notification=Notification.TypeNotification.SYSTEME,
            est_lu=True,
        )
        self.notif_client2 = Notification.objects.create(
            destinataire=self.client2,
            titre="Alerte client 2",
            message="Message privé",
            type_notification=Notification.TypeNotification.SYSTEME,
            est_lu=False,
        )

    def test_liste_notifications_isolation_client(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-liste")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Client 1 doit voir exactement ses 2 notifications
        self.assertEqual(len(response.data["results"]), 2)
        notif_ids = [n["id"] for n in response.data["results"]]
        self.assertIn(str(self.notif1.id), notif_ids)
        self.assertIn(str(self.notif2.id), notif_ids)
        self.assertNotIn(str(self.notif_client2.id), notif_ids)

    def test_filtre_notifications_non_lues(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-liste") + "?non_lues=true"

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.notif1.id))

    def test_compteur_non_lues(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-compteur")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["non_lues"], 1)

    def test_marquer_une_notification_lue(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-marquer-lue", kwargs={"pk": self.notif1.id})

        response = self.client.patch(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["est_lu"])
        self.assertIsNotNone(response.data["date_lecture"])

        self.notif1.refresh_from_db()
        self.assertTrue(self.notif1.est_lu)

    def test_marquer_toutes_notifications_lues(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-toutes-lues")

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["nb_modifiees"], 1)

        self.notif1.refresh_from_db()
        self.assertTrue(self.notif1.est_lu)

    def test_preferences_consultation_et_modification(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-preferences")

        # GET préférences
        res_get = self.client.get(url)
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertTrue(res_get.data["email_actif"])

        # PATCH préférences (désactiver les emails)
        res_patch = self.client.patch(url, {"email_actif": False}, format="json")
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertFalse(res_patch.data["email_actif"])

        prefs = PreferenceNotification.objects.get(utilisateur=self.client1)
        self.assertFalse(prefs.email_actif)


class NotificationSignauxTestCase(BaseNotificationTestCase):

    def test_signal_paiement_cree_notifications_client_et_vendeur(self):
        paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande1,
            montant=Decimal("15000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Cocody, Abidjan",
        )

        paiement.commandes.add(self.commande1)

        # Validation du paiement -> émet le signal paiement_valide
        valider_paiement(paiement)

        # 1. Notification pour le client
        notif_client = Notification.objects.filter(
            destinataire=self.client1,
            type_notification=Notification.TypeNotification.PAIEMENT,
        ).first()
        self.assertIsNotNone(notif_client)
        self.assertIn("15000.00", notif_client.message)

        # 2. Notification pour le vendeur
        notif_vendeur = Notification.objects.filter(
            destinataire=self.vendeur1,
            type_notification=Notification.TypeNotification.COMMANDE,
        ).first()
        self.assertIsNotNone(notif_vendeur)
        self.assertIn(self.commande1.numero_commande, notif_vendeur.message)

    def test_signal_livraison_changement_statut_cree_notification_client(self):
        from apps.livraison.services import changer_statut

        livreur = Utilisateur.objects.create_user(
            email="livreur@notif.ci", password="x", nom="L", prenom="Ivreur", role=Role.LIVREUR,
        )
        Commande.objects.filter(pk=self.commande1.pk).update(status=Commande.Status.PREPARATION)
        livraison = Livraison.objects.create(
            commande=self.commande1,
            adresse_livraison="Yopougon, Abidjan",
            status=Livraison.Status.EN_ATTENTE,
            livreur=livreur,
        )

        # Passage à EXPEDIEE (par le livreur assigné)
        changer_statut(livraison, Livraison.Status.EXPEDIEE, livreur)

        notif = Notification.objects.filter(
            destinataire=self.client1,
            type_notification=Notification.TypeNotification.LIVRAISON,
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn("expédié", notif.message)


# =====================================================================
# Envoi après commit, préférences, isolation et alertes métier
# (docs/MODULE_NOTIFICATIONS.md, § Sécurité).
# =====================================================================

from django.core import mail
from django.db import transaction
from django.test import override_settings

from apps.catalogue.models import Stock
from apps.commandes.services import annuler_commande
from apps.commandes.tests import DonneesCycleDeVie
from .services import alerter_stock_bas


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EnvoiApresCommitTests(BaseNotificationTestCase):
    """Email par Celery après le commit, jamais pour une action annulée."""

    def test_aucun_email_si_la_transaction_est_annulee(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    ServiceNotification.notifier_utilisateur(self.client1, "Paiement confirmé", "Merci.")
                    raise RuntimeError("échec après la notification")
            except RuntimeError:
                pass
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_envoye_apres_le_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as rappels:
            ServiceNotification.notifier_utilisateur(self.client1, "Paiement confirmé", "Merci.")
        self.assertEqual(len(mail.outbox), 0)  # rien pendant la transaction
        for rappel in rappels:
            rappel()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual((mail.outbox[0].to, mail.outbox[0].subject), (["client1@anitche.ci"], "[ANITCHE] Paiement confirmé"))

    def test_email_desactive_mais_in_app_toujours_cree(self):
        PreferenceNotification.objects.create(utilisateur=self.client1, email_actif=False)
        with self.captureOnCommitCallbacks(execute=True):
            ServiceNotification.notifier_utilisateur(self.client1, "Remboursement", "En cours.")
            ServiceNotification.notifier_utilisateur(self.client1, "Sécurité", "Code.", email_critique=True)
        self.assertEqual(Notification.objects.filter(destinataire=self.client1).count(), 2)
        self.assertEqual([m.subject for m in mail.outbox], ["[ANITCHE] Sécurité"])

    def test_alertes_administration_in_app_sans_email(self):
        admins = [Utilisateur.objects.create_user(email=f"admin{i}@anitche.ci", password="TestPassword123!", nom="A",
                                                  prenom="D", role=Role.ADMIN) for i in range(2)]
        Utilisateur.objects.create_user(email="ancien.admin@anitche.ci", password="TestPassword123!", nom="A",
                                        prenom="D", role=Role.ADMIN, is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            ServiceNotification.notifier_administration("Remboursement à traiter", "RMB-1")
        self.assertEqual(Notification.objects.filter(titre="Remboursement à traiter").count(), 2)
        self.assertEqual(set(Notification.objects.values_list("destinataire", flat=True)), {a.pk for a in admins})
        self.assertEqual(len(mail.outbox), 0)

    def test_journaux_sans_donnees_personnelles(self):
        with self.assertLogs("apps.notifications.services", level="INFO") as journaux:
            ServiceNotification.notifier_utilisateur(self.client1, "Livraison", "Votre colis arrive au 0707070707.")
        sortie = "\n".join(journaux.output)
        self.assertNotIn("client1@anitche.ci", sortie)
        self.assertNotIn("0707070707", sortie)


class PreferencesTests(BaseNotificationTestCase):
    """L'in-app ne se désactive pas ; seul l'email est un choix."""

    def test_in_app_et_sms_ne_sont_pas_des_preferences(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-preferences")
        reponse = self.client.patch(url, {"in_app_actif": False, "sms_actif": False, "email_actif": False}, format="json")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(set(reponse.data), {"id", "email_actif", "date_mise_a_jour"})
        ServiceNotification.notifier_utilisateur(self.client1, "Remboursement", "En cours.")
        self.assertEqual(Notification.objects.filter(destinataire=self.client1).count(), 1)


class IsolationTests(BaseNotificationTestCase):
    """Chaque notification n'est visible et
    modifiable que par son destinataire."""

    def test_notification_d_autrui_introuvable(self):
        notification = Notification.objects.create(destinataire=self.client1, titre="t", message="m")
        self.client.force_authenticate(user=self.client2)
        self.assertEqual(self.client.patch(reverse("notifications:notification-marquer-lue",
                                                   kwargs={"pk": notification.pk})).status_code, 404)
        self.assertEqual(self.client.get(reverse("notifications:notification-liste")).data["count"], 0)
        self.assertEqual(self.client.get(reverse("notifications:notification-compteur")).data["non_lues"], 0)
        self.client.post(reverse("notifications:notification-toutes-lues"))
        notification.refresh_from_db()
        self.assertFalse(notification.est_lu)


class StockBasTests(DonneesCycleDeVie, APITestCase):
    """seuil_alerte : alerte au vendeur au franchissement, une seule fois."""

    def setUp(self):
        self.creer_donnees()
        Stock.objects.filter(variante=self.variante1).update(quantite_disponible=7, seuil_alerte=5)

    def alertes(self):
        return Notification.objects.filter(destinataire=self.vendeur1, type_notification=Notification.TypeNotification.STOCK)

    def test_franchissement_du_seuil_au_checkout(self):
        self.assertEqual(self.commander((self.variante1, 2)).status_code, 201)  # 7 → 5 : seuil atteint
        self.assertEqual(self.alertes().count(), 1)
        self.assertIn("5 unité(s) restante(s)", self.alertes().get().message)

    def test_une_seule_alerte_sous_le_seuil(self):
        self.commander((self.variante1, 3))  # 7 → 4
        self.commander((self.variante1, 1))  # 4 → 3 : déjà sous le seuil
        self.assertEqual(self.alertes().count(), 1)

    def test_pas_d_alerte_au_dessus_du_seuil(self):
        self.commander((self.variante1, 1))  # 7 → 6
        self.assertFalse(self.alertes().exists())

    def test_rupture(self):
        self.commander((self.variante1, 3))  # 7 → 4 : stock bas
        self.commander((self.variante1, 4))  # 4 → 0 : rupture
        self.assertEqual(self.alertes().count(), 2)
        self.assertTrue(self.alertes().filter(titre__startswith="Rupture").exists())

    def test_autre_vendeur_jamais_alerte(self):
        self.commander((self.variante1, 3))
        self.assertFalse(Notification.objects.filter(destinataire=self.vendeur2,
                                                     type_notification=Notification.TypeNotification.STOCK).exists())

    def test_fonction_seule(self):
        stock = Stock.objects.select_related("variante__produit__boutique").get(variante=self.variante1)
        self.assertIsNone(alerter_stock_bas(stock, 10, 8))
        self.assertIsNotNone(alerter_stock_bas(stock, 6, 5))


class AnnulationTests(DonneesCycleDeVie, APITestCase):
    """Le client est prévenu de toute annulation ; le vendeur seulement
    si la commande était payée."""

    def setUp(self):
        self.creer_donnees()
        self.commande = Commande.objects.get(pk=self.commander((self.variante1, 1)).data[0]["id"])

    def test_expiration_non_payee_client_seul(self):
        annuler_commande(self.commande, Commande.MotifAnnulation.EXPIRATION)
        client = Notification.objects.get(destinataire=self.client_user, titre__contains="annulée")
        self.assertIn("Non payée dans le délai", client.message)
        self.assertFalse(Notification.objects.filter(destinataire=self.vendeur1, titre__contains="annulée").exists())

    def test_annulation_payee_client_et_vendeur(self):
        valider_paiement(self.payer(self.commande, statut=Paiement.Statut.EN_ATTENTE))
        self.commande.refresh_from_db()
        annuler_commande(self.commande, Commande.MotifAnnulation.CLIENT, acteur=self.client_user)
        client = Notification.objects.get(destinataire=self.client_user, titre__contains="annulée")
        self.assertIn("remboursement", client.message)
        self.assertTrue(Notification.objects.filter(destinataire=self.vendeur1, titre__contains="annulée").exists())
        self.assertEqual(client.lien_redirection, f"/commandes/{self.commande.pk}")
        vendeur = Notification.objects.get(destinataire=self.vendeur1, titre__contains="annulée")
        self.assertEqual(vendeur.lien_redirection, f"/vendeur/commandes/{self.commande.pk}")


# =====================================================================
# ROUTES LOGIQUES (apps/notifications/liens.py)
# =====================================================================

import importlib
import inspect
import re
import uuid
from types import SimpleNamespace

from django.apps import apps as registre_apps
from django.test import SimpleTestCase, TestCase

from . import liens

MIGRATION_LIENS = importlib.import_module("apps.notifications.migrations.0003_liens_routes_logiques")


class LiensTests(SimpleTestCase):
    """Chaque fonction produit un chemin conforme à l'un des gabarits de
    ROUTES, et aucun ne mène à l'admin Django."""

    def chemin_conforme(self, chemin):
        for gabarit in liens.ROUTES:
            motif = re.escape(gabarit).replace(r"\{uuid\}", r"[0-9a-f-]{36}").replace(r"\{id\}", r"\d+")
            if re.fullmatch(motif, chemin):
                return gabarit
        return None

    def test_chaque_fonction_produit_une_route_de_la_liste(self):
        objet_uuid = SimpleNamespace(pk=uuid.uuid4())
        objet_entier = SimpleNamespace(pk=42)
        produits = {}
        for nom, fonction in inspect.getmembers(liens, inspect.isfunction):
            if not nom.startswith("lien_"):
                continue
            parametres = inspect.signature(fonction).parameters
            argument = objet_entier if "produit" in parametres else objet_uuid
            chemin = fonction(argument) if parametres else fonction()
            with self.subTest(fonction=nom, chemin=chemin):
                gabarit = self.chemin_conforme(chemin)
                self.assertIsNotNone(gabarit)
                self.assertTrue(chemin.startswith("/"))
                self.assertNotIn("?", chemin)
                self.assertNotIn("//", chemin)
                self.assertFalse(chemin.startswith("/admin/"))
                produits[gabarit] = nom
        # Chaque gabarit est produit par une fonction (pas de route morte).
        self.assertEqual(set(produits), set(liens.ROUTES))

    def test_texte_d_aide_du_champ_liste_toutes_les_routes(self):
        aide = Notification._meta.get_field("lien_redirection").help_text
        for gabarit in liens.ROUTES:
            self.assertIn(gabarit, aide)


class MigrationLiensAdministrationTests(TestCase):
    """Migration 0003 : `/admin/…` devient `/administration/…`, le reste
    du chemin conservé ; les autres liens ne bougent pas ; retour arrière."""

    def setUp(self):
        self.destinataire = Utilisateur.objects.create_user(email="migr@anitche.ci", password="x", nom="M", prenom="G")

    def creer(self, lien):
        return Notification.objects.create(destinataire=self.destinataire, titre="t", message="m",
                                           lien_redirection=lien)

    def test_aller_retour(self):
        identifiant = uuid.uuid4()
        admin = self.creer(f"/admin/retours/{identifiant}")
        client = self.creer(f"/retours/{identifiant}")
        vide = self.creer("")
        MIGRATION_LIENS.vers_administration(registre_apps, None)
        for notification in (admin, client, vide):
            notification.refresh_from_db()
        self.assertEqual(admin.lien_redirection, f"/administration/retours/{identifiant}")
        self.assertEqual((client.lien_redirection, vide.lien_redirection), (f"/retours/{identifiant}", ""))
        # Rejouée, elle ne change plus rien.
        MIGRATION_LIENS.vers_administration(registre_apps, None)
        admin.refresh_from_db()
        self.assertEqual(admin.lien_redirection, f"/administration/retours/{identifiant}")

        MIGRATION_LIENS.vers_admin(registre_apps, None)
        for notification in (admin, client):
            notification.refresh_from_db()
        self.assertEqual(admin.lien_redirection, f"/admin/retours/{identifiant}")
        self.assertEqual(client.lien_redirection, f"/retours/{identifiant}")
