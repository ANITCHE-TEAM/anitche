from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.commandes.models import Commande
from apps.livraison.models import Livraison
from apps.paiements.models import Paiement
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
        self.assertEqual(len(response.data), 2)
        notif_ids = [n["id"] for n in response.data]
        self.assertIn(str(self.notif1.id), notif_ids)
        self.assertIn(str(self.notif2.id), notif_ids)
        self.assertNotIn(str(self.notif_client2.id), notif_ids)

    def test_filtre_notifications_non_lues(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("notifications:notification-liste") + "?non_lues=true"

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(self.notif1.id))

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
        self.assertTrue(res_get.data["in_app_actif"])

        # PATCH préférences (désactiver SMS)
        res_patch = self.client.patch(url, {"sms_actif": False}, format="json")
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertFalse(res_patch.data["sms_actif"])

        prefs = PreferenceNotification.objects.get(utilisateur=self.client1)
        self.assertFalse(prefs.sms_actif)


class NotificationSignauxTestCase(BaseNotificationTestCase):

    def test_signal_paiement_cree_notifications_client_et_vendeur(self):
        paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande1,
            montant=Decimal("15000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Cocody, Abidjan",
        )

        # Validation du paiement -> émet le signal paiement_valide
        paiement.valider(transaction_id_externe="wave_trx_123")

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
        livraison = Livraison.objects.create(
            commande=self.commande1,
            adresse_livraison="Yopougon, Abidjan",
            status=Livraison.Status.EN_ATTENTE,
        )

        # Passage à EXPEDIEE
        livraison.changer_status(Livraison.Status.EXPEDIEE)

        notif = Notification.objects.filter(
            destinataire=self.client1,
            type_notification=Notification.TypeNotification.LIVRAISON,
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn("expédié", notif.message)
