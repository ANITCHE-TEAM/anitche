from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from .models import PasseportProduit, HistoriqueScanPasseport


class BasePasseportTestCase(APITestCase):
    def setUp(self):
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
            nom="Artisanat Tiassalé",
            est_active=True,
        )

        # Vendeur 2
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
            nom="Atelier Wax Abidjan",
            est_active=True,
        )

        # Produit de Vendeur 1
        self.produit1 = Produit.objects.create(
            boutique=self.boutique1,
            nom="Masque Baoulé Traditionnel",
            prix_base=Decimal("35000.00"),
        )
        self.variante1 = VarianteProduit.objects.create(
            produit=self.produit1,
            nom="Bois d'Iroko Authentique",
            prix=Decimal("35000.00"),
        )


class PasseportAPITestCase(BasePasseportTestCase):

    def test_vendeur_creer_passeport_produit(self):
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("passeport_qr:passeport-vendeur-liste-creer")
        data = {
            "produit_id": self.produit1.id,
            "variante_id": self.variante1.id,
            "numero_lot": "LOT-2026-08-01",
            "origine_geographique": "Tiassalé, Côte d'Ivoire",
            "materiaux_utilises": "Bois d'Iroko sculpté main, pigments végétaux naturels",
            "artisan_createur": "Maître Kouamé",
            "statut_certification": "certifie_authentique",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["code_passeport"].startswith("PAS-"))
        self.assertEqual(response.data["artisan_createur"], "Maître Kouamé")
        self.assertIn("anitche.ci/qr/verifier/", response.data["url_verification_publique"])

        # Vérification en base
        passeport = PasseportProduit.objects.get(code_passeport=response.data["code_passeport"])
        self.assertEqual(passeport.boutique, self.boutique1)
        self.assertEqual(passeport.produit, self.produit1)
        self.assertEqual(passeport.nb_scans, 0)

    def test_rejet_creation_passeport_produit_autre_boutique(self):
        # Vendeur 2 tente de créer un passeport pour le produit de Vendeur 1
        self.client.force_authenticate(user=self.vendeur2)
        url = reverse("passeport_qr:passeport-vendeur-liste-creer")
        data = {
            "produit_id": self.produit1.id,
            "numero_lot": "LOT-HACK",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_consultation_publique_incremente_scans(self):
        passeport = PasseportProduit.objects.create(
            produit=self.produit1,
            variante=self.variante1,
            boutique=self.boutique1,
            numero_lot="LOT-PUBLIC-01",
            origine_geographique="Grand-Bassam",
            materiaux_utilises="Cuir véritable",
            artisan_createur="Atelier Bassam",
        )

        # Consultation publique anonyme (sans force_authenticate)
        url = reverse("passeport_qr:passeport-public-verification", kwargs={"code_passeport": passeport.code_passeport})
        response = self.client.get(url, HTTP_USER_AGENT="Mozilla/5.0 (iPhone)")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["code_passeport"], passeport.code_passeport)
        self.assertEqual(response.data["produit_nom"], "Masque Baoulé Traditionnel")
        self.assertEqual(response.data["boutique_nom"], "Artisanat Tiassalé")
        self.assertEqual(response.data["nb_scans"], 1)

        # Vérification en base de l'historique de scan
        passeport.refresh_from_db()
        self.assertEqual(passeport.nb_scans, 1)
        self.assertIsNotNone(passeport.dernier_scan)
        self.assertEqual(HistoriqueScanPasseport.objects.filter(passeport=passeport).count(), 1)

    def test_consultation_publique_code_invalide(self):
        url = reverse("passeport_qr:passeport-public-verification", kwargs={"code_passeport": "PAS-INVALIDE-999"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_liste_passeports_espace_vendeur(self):
        PasseportProduit.objects.create(
            produit=self.produit1,
            boutique=self.boutique1,
            numero_lot="LOT-01",
        )

        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("passeport_qr:passeport-vendeur-liste-creer")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["produit_nom"], "Masque Baoulé Traditionnel")
