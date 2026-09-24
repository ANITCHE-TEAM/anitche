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


class PasseportReassignationProduitTestCase(BasePasseportTestCase):
    """F-21 (audit sécurité) : un vendeur ne doit pas pouvoir, via PATCH,
    réassigner un passeport existant à un produit qui n'est pas le sien —
    ce qui casserait la garantie d'authenticité que ce module est censé
    apporter (le passeport resterait affiché sous SA boutique, tout en
    certifiant le produit d'un autre vendeur)."""

    def setUp(self):
        super().setUp()
        # Produit de Vendeur 2, cible de la tentative de réassignation
        self.produit2 = Produit.objects.create(
            boutique=self.boutique2,
            nom="Sac en Raphia Bassam",
            prix_base=Decimal("18000.00"),
        )
        self.variante2 = VarianteProduit.objects.create(
            produit=self.produit2,
            nom="Naturel",
            prix=Decimal("18000.00"),
        )

        self.passeport1 = PasseportProduit.objects.create(
            produit=self.produit1,
            variante=self.variante1,
            boutique=self.boutique1,
            numero_lot="LOT-V1-01",
        )

    def test_vendeur_ne_peut_pas_reassigner_passeport_vers_produit_dun_autre_vendeur(self):
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("passeport_qr:passeport-vendeur-detail", kwargs={"pk": self.passeport1.pk})

        response = self.client.patch(url, {"produit": self.produit2.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.produit_id, self.produit1.id)

    def test_vendeur_ne_peut_pas_assigner_variante_dun_autre_produit(self):
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("passeport_qr:passeport-vendeur-detail", kwargs={"pk": self.passeport1.pk})

        # variante2 appartient à produit2, pas à produit1 (le produit courant du passeport)
        response = self.client.patch(url, {"variante": self.variante2.id}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.variante_id, self.variante1.id)

    def test_vendeur_peut_toujours_corriger_vers_un_autre_produit_de_sa_propre_boutique(self):
        # Cas légitime : un produit de la MÊME boutique reste autorisé (en
        # remettant aussi la variante à zéro, puisque variante1 est liée à
        # l'ancien produit1 — changer de produit sans y toucher serait
        # justement l'incohérence que le correctif détecte à raison).
        autre_produit_meme_boutique = Produit.objects.create(
            boutique=self.boutique1,
            nom="Masque Baoulé (variante fabrication)",
            prix_base=Decimal("35000.00"),
        )
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("passeport_qr:passeport-vendeur-detail", kwargs={"pk": self.passeport1.pk})

        response = self.client.patch(
            url,
            {"produit": autre_produit_meme_boutique.id, "variante": None},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.passeport1.refresh_from_db()
        self.assertEqual(self.passeport1.produit_id, autre_produit_meme_boutique.id)