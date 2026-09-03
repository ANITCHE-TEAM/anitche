from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role
from apps.vendeurs.models import Boutique
from apps.commandes.models import Commande
from apps.paiements.models import Paiement
from .models import CompteFidelite, TransactionFidelite, CouponReduction


class BaseFideliteTestCase(APITestCase):
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
        self.vendeur = Utilisateur.objects.create_user(
            email="vendeur@anitche.ci",
            password="TestPassword123!",
            nom="Kouassi",
            prenom="Jean",
            role=Role.VENDEUR,
        )
        self.boutique = Boutique.objects.create(
            proprietaire=self.vendeur,
            nom="Boutique Artisanat",
            est_active=True,
        )

        # Commande pour client 1
        self.commande = Commande.objects.create(
            boutique=self.boutique,
            client=self.client1,
            montant_total=Decimal("25000.00"),
            status=Commande.Status.CREEE,
        )


class FideliteAPITestCase(BaseFideliteTestCase):

    def test_gain_points_sur_validation_paiement(self):
        paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande,
            montant=Decimal("25000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Cocody, Abidjan",
        )

        # Validation du paiement -> émet le signal paiement_valide
        paiement.valider(transaction_id_externe="wave_trx_888")

        compte = CompteFidelite.objects.get(utilisateur=self.client1)
        # 25 000 FCFA // 1000 = 25 points
        self.assertEqual(compte.solde_points, 25)
        self.assertEqual(compte.points_cumules_total, 25)
        self.assertEqual(compte.palier, CompteFidelite.Palier.BRONZE)

        # Vérification de la transaction enregistrée
        transaction = TransactionFidelite.objects.get(compte=compte)
        self.assertEqual(transaction.points, 25)
        self.assertEqual(transaction.type_transaction, TransactionFidelite.TypeTransaction.GAIN)

    def test_paliers_fidelite_evolution(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)

        compte.crediter_points(600)
        self.assertEqual(compte.palier, CompteFidelite.Palier.ARGENT)

        compte.crediter_points(1500)  # Total 2100
        self.assertEqual(compte.palier, CompteFidelite.Palier.OR)

        compte.crediter_points(3000)  # Total 5100
        self.assertEqual(compte.palier, CompteFidelite.Palier.PLATINE)

    def test_mon_compte_fidelite_api(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(150)

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-mon-compte")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["solde_points"], 150)
        self.assertEqual(response.data["palier"], "bronze")

    def test_convertir_points_en_coupon_succes(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(200)

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-convertir")
        data = {"option": "100_PTS_10PCT"}

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["code"].startswith("FID-"))
        self.assertEqual(Decimal(str(response.data["valeur"])), Decimal("10.00"))
        self.assertEqual(response.data["type_reduction"], "pourcentage")

        # Vérification du solde restant : 200 - 100 = 100 points
        compte.refresh_from_db()
        self.assertEqual(compte.solde_points, 100)

    def test_convertir_points_solde_insuffisant(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(30)  # Moins que les 50 requis

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-convertir")
        data = {"option": "50_PTS_5PCT"}

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verifier_coupon_reduction_pourcentage(self):
        coupon = CouponReduction.objects.create(
            code="PROMO10",
            client=self.client1,
            type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("10.00"),
            montant_minimum_commande=Decimal("5000.00"),
        )

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {
            "code": "PROMO10",
            "montant_commande": "20000.00",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["valide"])
        # 10% sur 20 000 FCFA = 2 000 FCFA de remise
        self.assertEqual(Decimal(response.data["remise"]), Decimal("2000.00"))
        self.assertEqual(Decimal(response.data["montant_final"]), Decimal("18000.00"))

    def test_rejet_coupon_montant_minimum(self):
        CouponReduction.objects.create(
            code="MIN15000",
            client=self.client1,
            type_reduction=CouponReduction.TypeReduction.MONTANT_FIXE,
            valeur=Decimal("2000.00"),
            montant_minimum_commande=Decimal("15000.00"),
        )

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {
            "code": "MIN15000",
            "montant_commande": "8000.00",  # Inférieur au minimum
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["valide"])

    def test_rejet_coupon_autre_client(self):
        CouponReduction.objects.create(
            code="NOMINATIF_AYA",
            client=self.client1,
            type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("15.00"),
        )

        # Client 2 tente d'utiliser le coupon de Client 1
        self.client.force_authenticate(user=self.client2)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {
            "code": "NOMINATIF_AYA",
            "montant_commande": "20000.00",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["valide"])
