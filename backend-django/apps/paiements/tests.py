from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, GroupeCommande, CommandeItem
from apps.livraison.models import Livraison
from .models import Paiement, JournalWebhook
from .services import ServicePaiement


class BasePaiementTestCase(APITestCase):
    def setUp(self):
        # Client 1
        self.client1 = Utilisateur.objects.create_user(
            email="client1@anitche.ci",
            password="TestPassword123!",
            nom="Konan",
            prenom="Aya",
            role=Role.CLIENT,
            statut_kyc=StatutKYC.NON_SOUMIS,
        )

        # Client 2
        self.client2 = Utilisateur.objects.create_user(
            email="client2@anitche.ci",
            password="TestPassword123!",
            nom="Touré",
            prenom="Ali",
            role=Role.CLIENT,
            statut_kyc=StatutKYC.NON_SOUMIS,
        )

        # Administrateur
        self.admin = Utilisateur.objects.create_user(
            email="admin@anitche.ci",
            password="AdminPassword123!",
            nom="Admin",
            prenom="Sys",
            role=Role.ADMIN,
            is_staff=True,
        )

        # Vendeur 1 & Boutique 1
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
            nom="Boutique Artisanat CI",
            est_active=True,
        )

        # Vendeur 2 & Boutique 2
        self.vendeur2 = Utilisateur.objects.create_user(
            email="vendeur2@anitche.ci",
            password="TestPassword123!",
            nom="Diop",
            prenom="Fatou",
            role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE,
        )
        self.boutique2 = Boutique.objects.create(
            proprietaire=self.vendeur2,
            nom="Mode & Pagnes Abidjan",
            est_active=True,
        )

        # Commande individuelle pour client 1
        self.commande1 = Commande.objects.create(
            boutique=self.boutique1,
            client=self.client1,
            montant_total=Decimal("15000.00"),
            status=Commande.Status.CREEE,
        )

        # Groupe de commandes multi-boutiques pour client 1
        self.groupe = GroupeCommande.objects.create(client=self.client1)
        self.commande_groupe1 = Commande.objects.create(
            groupe=self.groupe,
            boutique=self.boutique1,
            client=self.client1,
            montant_total=Decimal("8000.00"),
            status=Commande.Status.CREEE,
        )
        self.commande_groupe2 = Commande.objects.create(
            groupe=self.groupe,
            boutique=self.boutique2,
            client=self.client1,
            montant_total=Decimal("12000.00"),
            status=Commande.Status.CREEE,
        )


class PaiementAPITestCase(BasePaiementTestCase):

    def test_initier_paiement_commande_unique_wave(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("paiements:initier-paiement")
        data = {
            "commande_id": str(self.commande1.id),
            "methode": "wave",
            "telephone": "+2250700000001",
            "adresse_livraison": "Cocody Angré 8e Tranche, Abidjan",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("reference", response.data)
        self.assertTrue(response.data["reference"].startswith("PAY-"))
        self.assertEqual(response.data["statut"], "en_attente")
        self.assertEqual(Decimal(str(response.data["montant"])), Decimal("15000.00"))
        self.assertIsNotNone(response.data["url_paiement"])

        # Vérification en base
        paiement = Paiement.objects.get(reference=response.data["reference"])
        self.assertEqual(paiement.client, self.client1)
        self.assertEqual(paiement.commande, self.commande1)
        self.assertEqual(paiement.methode, Paiement.Methode.WAVE)

    def test_initier_paiement_groupe_commande(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("paiements:initier-paiement")
        data = {
            "groupe_commande_id": str(self.groupe.id),
            "methode": "orange_money",
            "telephone": "+2250700000002",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Montant attendu = 8000 + 12000 = 20000 FCFA
        self.assertEqual(Decimal(str(response.data["montant"])), Decimal("20000.00"))
        self.assertEqual(str(response.data["groupe_commande"]), str(self.groupe.id))

    def test_initier_paiement_espece_livraison_confirme_automatiquement(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("paiements:initier-paiement")
        data = {
            "commande_id": str(self.commande1.id),
            "methode": "espece_livraison",
            "adresse_livraison": "Plateau Dokui, Abidjan",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Pour le Cash on Delivery, la commande passe automatiquement à CONFIRMEE et la livraison est initialisée
        self.commande1.refresh_from_db()
        self.assertEqual(self.commande1.status, Commande.Status.CONFIRMEE)

        livraison = Livraison.objects.get(commande=self.commande1)
        self.assertEqual(livraison.status, Livraison.Status.EN_ATTENTE)
        self.assertEqual(livraison.adresse_livraison, "Plateau Dokui, Abidjan")

    def test_rejet_paiement_commande_autre_client(self):
        self.client.force_authenticate(user=self.client2)  # Client 2 tente de payer la commande du Client 1
        url = reverse("paiements:initier-paiement")
        data = {
            "commande_id": str(self.commande1.id),
            "methode": "wave",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejet_paiement_commande_deja_confirmee(self):
        self.commande1.status = Commande.Status.CONFIRMEE
        self.commande1.save()

        self.client.force_authenticate(user=self.client1)
        url = reverse("paiements:initier-paiement")
        data = {
            "commande_id": str(self.commande1.id),
            "methode": "wave",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_liste_paiements_isolation_clients(self):
        p1 = Paiement.objects.create(
            client=self.client1,
            commande=self.commande1,
            montant=Decimal("15000.00"),
            methode=Paiement.Methode.WAVE,
        )

        commande_client2 = Commande.objects.create(
            boutique=self.boutique1,
            client=self.client2,
            montant_total=Decimal("5000.00"),
            status=Commande.Status.CREEE,
        )
        p2 = Paiement.objects.create(
            client=self.client2,
            commande=commande_client2,
            montant=Decimal("5000.00"),
            methode=Paiement.Methode.ORANGE_MONEY,
        )

        # Client 1 ne voit que ses paiements
        self.client.force_authenticate(user=self.client1)
        url = reverse("paiements:paiement-liste")
        res1 = self.client.get(url)
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res1.data), 1)
        self.assertEqual(res1.data[0]["id"], str(p1.id))

        # Admin voit tous les paiements
        self.client.force_authenticate(user=self.admin)
        res_admin = self.client.get(url)
        self.assertEqual(res_admin.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_admin.data), 2)


class WebhookPaiementTestCase(BasePaiementTestCase):

    def setUp(self):
        super().setUp()
        self.paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande1,
            montant=Decimal("15000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Marcory Zone 4, Abidjan",
        )

    def test_webhook_succes_valide_commande_et_cree_livraison(self):
        url = reverse("paiements:webhook-paiement", kwargs={"fournisseur": "wave"})
        payload = {
            "evenement_id": "evt_wave_123456",
            "reference": self.paiement.reference,
            "statut": "succes",
            "transaction_id_externe": "wave_trx_998877",
            "metadata": {"frais": "150"},
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 1. Statut du paiement mis à jour
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.VALIDE)
        self.assertIsNotNone(self.paiement.date_validation)
        self.assertEqual(self.paiement.transaction_id_externe, "wave_trx_998877")

        # 2. Statut de la commande passé à CONFIRMEE
        self.commande1.refresh_from_db()
        self.assertEqual(self.commande1.status, Commande.Status.CONFIRMEE)

        # 3. Fiche Livraison créée automatiquement
        livraison = Livraison.objects.get(commande=self.commande1)
        self.assertEqual(livraison.status, Livraison.Status.EN_ATTENTE)
        self.assertEqual(livraison.adresse_livraison, "Marcory Zone 4, Abidjan")

        # 4. Entrée enregistrée dans le JournalWebhook
        journal = JournalWebhook.objects.get(evenement_id="evt_wave_123456")
        self.assertEqual(journal.statut_traitement, JournalWebhook.StatutTraitement.TRAITE)

    def test_webhook_idempotence(self):
        url = reverse("paiements:webhook-paiement", kwargs={"fournisseur": "wave"})
        payload = {
            "evenement_id": "evt_wave_unique_99",
            "reference": self.paiement.reference,
            "statut": "succes",
        }

        # Premier appel
        res1 = self.client.post(url, payload, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Deuxième appel avec le même événement ID
        res2 = self.client.post(url, payload, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(JournalWebhook.objects.filter(evenement_id="evt_wave_unique_99").count(), 1)

    def test_webhook_echec_met_a_jour_statut(self):
        url = reverse("paiements:webhook-paiement", kwargs={"fournisseur": "orange_money"})
        payload = {
            "evenement_id": "evt_om_failure_01",
            "reference": self.paiement.reference,
            "statut": "echec",
            "metadata": {"motif": "Solde insuffisant"},
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.statut, Paiement.Statut.ECHOUE)
        self.assertEqual(self.paiement.metadata.get("motif_echec"), "Solde insuffisant")

        # La commande reste en état CREEE
        self.commande1.refresh_from_db()
        self.assertEqual(self.commande1.status, Commande.Status.CREEE)
