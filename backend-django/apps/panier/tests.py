from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role
from .models import Panier, PanierItem


class PanierTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)

    def _create_user(self, email, role):
        return Utilisateur.objects.create_user(
            email=email,
            password="testpass123",
            nom="Test",
            prenom="User",
            role=role,
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
        # Un panier a été créé avec une session_key, sans utilisateur
        self.assertTrue(Panier.objects.filter(utilisateur__isnull=True).exists())

    def test_authenticated_user_always_gets_same_panier(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-detail")
        response1 = self.client.get(url)
        response2 = self.client.get(url)
        self.assertEqual(response1.data["id"], response2.data["id"])
        self.assertEqual(Panier.objects.filter(utilisateur=self.client_user).count(), 1)

    # ---------- Ajout d'articles ----------

    def test_authenticated_user_can_add_item(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-items")
        response = self.client.post(url, {"quantite": 2})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["quantite"], 2)

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
        # Doit être attaché au panier du client_user, pas à other_panier
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
        item = PanierItem.objects.create(panier=panier, quantite=1)

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