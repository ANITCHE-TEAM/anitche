from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from .models import DemandeRetour, RetourItem


class BaseRetourTestCase(APITestCase):
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
            nom="Boutique Wax",
            est_active=True,
        )

        # Produit et Variante 1
        self.produit1 = Produit.objects.create(
            boutique=self.boutique1,
            nom="Robe Baoulé",
            prix_base=Decimal("20000.00"),
        )
        self.variante1 = VarianteProduit.objects.create(
            produit=self.produit1,
            nom="Taille M",
            prix=Decimal("20000.00"),
        )
        self.variante1.stock.quantite_disponible = 5
        self.variante1.stock.save()

        # Commande livrée pour Client 1
        self.commande1 = Commande.objects.create(
            boutique=self.boutique1,
            client=self.client1,
            montant_total=Decimal("40000.00"),
            status=Commande.Status.LIVREE,
        )
        self.item1 = CommandeItem.objects.create(
            commande=self.commande1,
            variante=self.variante1,
            nom_produit="Robe Baoulé",
            prix_unitaire=Decimal("20000.00"),
            quantite=2,
        )


class RetoursAPITestCase(BaseRetourTestCase):

    def test_creer_demande_retour_valide(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("retours:retour-liste-creer")
        data = {
            "commande_id": str(self.commande1.id),
            "motif": "produit_defectueux",
            "type_resolution": "remboursement",
            "description": "La couture latérale est déchirée au déballage du colis.",
            "articles": [
                {
                    "commande_item_id": str(self.item1.id),
                    "quantite": 1,
                }
            ],
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["numero_retour"].startswith("RET-"))
        self.assertEqual(response.data["statut"], "demande")
        self.assertEqual(Decimal(str(response.data["montant_remboursement"])), Decimal("20000.00"))
        self.assertEqual(len(response.data["articles"]), 1)

    def test_rejet_demande_retour_quantite_superieure(self):
        self.client.force_authenticate(user=self.client1)
        url = reverse("retours:retour-liste-creer")
        data = {
            "commande_id": str(self.commande1.id),
            "description": "Erreur de taille",
            "articles": [
                {
                    "commande_item_id": str(self.item1.id),
                    "quantite": 5,  # Supérieur aux 2 commandés
                }
            ],
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_liste_retours_client(self):
        demande = DemandeRetour.objects.create(
            commande=self.commande1,
            client=self.client1,
            boutique=self.boutique1,
            motif=DemandeRetour.Motif.NON_CONFORME,
            description="Article reçu non conforme à la photo.",
            montant_remboursement=Decimal("20000.00"),
        )

        self.client.force_authenticate(user=self.client1)
        url = reverse("retours:retour-liste-creer")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(demande.id))

    def test_vendeur_approuve_demande_retour(self):
        demande = DemandeRetour.objects.create(
            commande=self.commande1,
            client=self.client1,
            boutique=self.boutique1,
            motif=DemandeRetour.Motif.PRODUIT_DEFECTUEUX,
            description="Défaut constaté",
            montant_remboursement=Decimal("20000.00"),
            statut=DemandeRetour.Statut.DEMANDE,
        )

        # Le vendeur de la boutique approuve
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("retours:retour-traiter", kwargs={"pk": demande.id})
        data = {
            "action": "approuver",
            "reponse": "Retour accepté. Veuillez expédier le colis à notre adresse.",
        }

        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["statut"], "approuve")

        demande.refresh_from_db()
        self.assertEqual(demande.statut, DemandeRetour.Statut.APPROUVE)

    def test_vendeur_rejette_demande_retour(self):
        demande = DemandeRetour.objects.create(
            commande=self.commande1,
            client=self.client1,
            boutique=self.boutique1,
            motif=DemandeRetour.Motif.CHANGEMENT_AVIS,
            description="Je ne veux plus l'article",
            montant_remboursement=Decimal("20000.00"),
            statut=DemandeRetour.Statut.DEMANDE,
        )

        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("retours:retour-traiter", kwargs={"pk": demande.id})
        data = {
            "action": "rejeter",
            "reponse": "Délai de rétractation de 14 jours dépassé.",
        }

        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["statut"], "rejete")

    def test_reception_colis_retour_reapprovisionne_stock(self):
        demande = DemandeRetour.objects.create(
            commande=self.commande1,
            client=self.client1,
            boutique=self.boutique1,
            motif=DemandeRetour.Motif.MAUVAISE_TAILLE,
            description="Taille M trop petite",
            montant_remboursement=Decimal("20000.00"),
            statut=DemandeRetour.Statut.EN_TRANSIT,
        )
        RetourItem.objects.create(
            demande_retour=demande,
            commande_item=self.item1,
            quantite=1,
        )

        stock_initial = self.variante1.stock.quantite_disponible  # 5

        self.client.force_authenticate(user=self.vendeur1)
        url = reverse("retours:retour-traiter", kwargs={"pk": demande.id})
        data = {
            "action": "receptionner",
            "restock": True,
        }

        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["statut"], "receptionne")

        # Vérification du stock incrémenté : 5 + 1 = 6
        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, stock_initial + 1)
