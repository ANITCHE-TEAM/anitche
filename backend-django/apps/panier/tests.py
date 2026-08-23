from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from .models import Panier, PanierItem


class PanierTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)

        # Création d'un vendeur avec boutique et produit pour les tests de panier
        self.vendeur = self._create_user("vendeur@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique = Boutique.objects.create(
            proprietaire=self.vendeur,
            nom="Boutique Panier Test",
            est_active=True,
        )
        self.produit = Produit.objects.create(
            boutique=self.boutique,
            nom="Montre Connectée",
            prix_base=Decimal("25000.00"),
        )
        self.variante = VarianteProduit.objects.create(
            produit=self.produit,
            nom="Version Noire",
            prix=Decimal("25000.00"),
            prix_promo=Decimal("20000.00"),
        )
        self.variante.stock.quantite_disponible = 10
        self.variante.stock.save()

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS):
        return Utilisateur.objects.create_user(
            email=email,
            password="testpass123",
            nom="Test",
            prenom="User",
            role=role,
            statut_kyc=statut_kyc,
        )

    # ---------- Création implicite du panier ----------

    def test_authenticated_user_gets_a_panier(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-detail")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Panier.objects.filter(utilisateur=self.client_user).exists())

    def test_anonymous_visitor_gets_a_panier(self):
        url = reverse("panier:panier-detail")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Panier.objects.filter(utilisateur__isnull=True).exists())

    def test_authenticated_user_always_gets_same_panier(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-detail")
        response1 = self.client.get(url)
        response2 = self.client.get(url)
        self.assertEqual(response1.data["id"], response2.data["id"])
        self.assertEqual(Panier.objects.filter(utilisateur=self.client_user).count(), 1)

    # ---------- Ajout d'articles et intégration Catalogue ----------

    def test_authenticated_user_can_add_item_with_variant(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-items")
        response = self.client.post(url, {
            "variante": self.variante.id,
            "quantite": 2,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["quantite"], 2)
        self.assertEqual(Decimal(response.data["prix_unitaire"]), Decimal("20000.00"))
        self.assertEqual(Decimal(response.data["sous_total"]), Decimal("40000.00"))

    def test_adding_same_variant_increments_quantity(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-items")
        self.client.post(url, {"variante": self.variante.id, "quantite": 1})
        self.client.post(url, {"variante": self.variante.id, "quantite": 2})

        panier = Panier.objects.get(utilisateur=self.client_user)
        self.assertEqual(panier.items.count(), 1)
        self.assertEqual(panier.items.first().quantite, 3)
        self.assertEqual(panier.total, Decimal("60000.00"))

    def test_item_is_attached_to_own_panier_not_payload(self):
        self.client.force_authenticate(user=self.client_user)
        other_panier = Panier.objects.create(utilisateur=self.other_client)

        url = reverse("panier:panier-items")
        response = self.client.post(url, {
            "quantite": 1,
            "panier": str(other_panier.id),  # tentative de forcer un autre panier
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = PanierItem.objects.get(pk=response.data["id"])
        self.assertNotEqual(item.panier_id, other_panier.id)
        self.assertEqual(item.panier.utilisateur, self.client_user)

    # ---------- Isolation entre paniers ----------

    def test_user_cannot_see_others_items(self):
        other_panier = Panier.objects.create(utilisateur=self.other_client)
        PanierItem.objects.create(panier=other_panier, quantite=3)

        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-items")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_user_cannot_modify_others_item(self):
        other_panier = Panier.objects.create(utilisateur=self.other_client)
        item = PanierItem.objects.create(panier=other_panier, quantite=1)

        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-item-detail", args=[item.id])
        response = self.client.patch(url, {"quantite": 5})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_modify_own_item(self):
        self.client.force_authenticate(user=self.client_user)
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        item = PanierItem.objects.create(panier=panier, quantite=1, variante=self.variante)

        url = reverse("panier:panier-item-detail", args=[item.id])
        response = self.client.patch(url, {"quantite": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 5)

    def test_user_can_delete_own_item(self):
        self.client.force_authenticate(user=self.client_user)
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        item = PanierItem.objects.create(panier=panier, quantite=1)

        url = reverse("panier:panier-item-detail", args=[item.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PanierItem.objects.filter(pk=item.id).exists())