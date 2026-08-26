from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from apps.panier.models import Panier, PanierItem
from .models import Commande, GroupeCommande, CommandeItem


class ValiderPanierTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)

        # Boutique 1 avec une variante en stock
        self.vendeur1 = self._create_user("vendeur1@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique1 = Boutique.objects.create(proprietaire=self.vendeur1, nom="Boutique 1", est_active=True)
        self.produit1 = Produit.objects.create(boutique=self.boutique1, nom="Produit A", prix_base=Decimal("1000"))
        self.variante1 = VarianteProduit.objects.create(produit=self.produit1, nom="Standard", prix=Decimal("1000"))
        self.variante1.stock.quantite_disponible = 10
        self.variante1.stock.save()

        # Boutique 2 avec une autre variante en stock
        self.vendeur2 = self._create_user("vendeur2@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique2 = Boutique.objects.create(proprietaire=self.vendeur2, nom="Boutique 2", est_active=True)
        self.produit2 = Produit.objects.create(boutique=self.boutique2, nom="Produit B", prix_base=Decimal("2000"))
        self.variante2 = VarianteProduit.objects.create(produit=self.produit2, nom="Standard", prix=Decimal("2000"))
        self.variante2.stock.quantite_disponible = 5
        self.variante2.stock.save()

        self.url = reverse("commandes:valider-panier")

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS):
        return Utilisateur.objects.create_user(
            email=email, password="testpass123", nom="Test", prenom="User",
            role=role, statut_kyc=statut_kyc,
        )

    def _ajouter_au_panier(self, variante, quantite):
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=variante, quantite=quantite)
        return panier

    # ---------- Validation nominale ----------

    def test_valider_panier_cree_une_commande_par_boutique(self):
        self._ajouter_au_panier(self.variante1, 2)
        self._ajouter_au_panier(self.variante2, 1)

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data), 2)  # une commande par boutique
        self.assertEqual(Commande.objects.count(), 2)
        self.assertEqual(GroupeCommande.objects.count(), 1)

    def test_montant_total_correctement_calcule(self):
        self._ajouter_au_panier(self.variante1, 3)  # 3 x 1000 = 3000

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url)

        commande = Commande.objects.get(boutique=self.boutique1)
        self.assertEqual(commande.montant_total, Decimal("3000.00"))

    def test_commande_item_snapshot_correct(self):
        self._ajouter_au_panier(self.variante1, 2)

        self.client.force_authenticate(user=self.client_user)
        self.client.post(self.url)

        item = CommandeItem.objects.get(commande__boutique=self.boutique1)
        self.assertEqual(item.nom_produit, "Produit A")
        self.assertEqual(item.prix_unitaire, Decimal("1000.00"))
        self.assertEqual(item.quantite, 2)

    def test_stock_decremente_apres_validation(self):
        self._ajouter_au_panier(self.variante1, 4)

        self.client.force_authenticate(user=self.client_user)
        self.client.post(self.url)

        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, 6)  # 10 - 4

    def test_panier_vide_apres_validation(self):
        self._ajouter_au_panier(self.variante1, 1)

        self.client.force_authenticate(user=self.client_user)
        self.client.post(self.url)

        panier = Panier.objects.get(utilisateur=self.client_user)
        self.assertEqual(panier.items.count(), 0)

    # ---------- Cas d'erreur ----------

    def test_panier_vide_refuse(self):
        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_stock_insuffisant_bloque_toute_la_validation(self):
        self._ajouter_au_panier(self.variante1, 2)      # stock suffisant
        self._ajouter_au_panier(self.variante2, 999)    # stock insuffisant

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Rien ne doit avoir été créé (transaction atomique)
        self.assertEqual(Commande.objects.count(), 0)
        self.assertEqual(GroupeCommande.objects.count(), 0)
        # Le stock de variante1 ne doit pas avoir bougé non plus
        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, 10)

    def test_unauthenticated_cannot_validate(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class CommandeAccessTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)

        self.vendeur = self._create_user("vendeur@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique = Boutique.objects.create(proprietaire=self.vendeur, nom="Boutique", est_active=True)
        self.produit = Produit.objects.create(boutique=self.boutique, nom="Produit", prix_base=Decimal("500"))
        self.variante = VarianteProduit.objects.create(produit=self.produit, nom="Standard", prix=Decimal("500"))

        self.groupe = GroupeCommande.objects.create(client=self.client_user)
        self.commande = Commande.objects.create(
            groupe=self.groupe, boutique=self.boutique, client=self.client_user,
            montant_total=Decimal("500.00"),
        )
        CommandeItem.objects.create(
            commande=self.commande, variante=self.variante,
            nom_produit="Produit", prix_unitaire=Decimal("500.00"), quantite=1,
        )

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS):
        return Utilisateur.objects.create_user(
            email=email, password="testpass123", nom="Test", prenom="User",
            role=role, statut_kyc=statut_kyc,
        )

    def test_owner_can_see_own_commande(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("commandes:commande-detail", args=[self.commande.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_stranger_cannot_see_others_commande(self):
        self.client.force_authenticate(user=self.other_client)
        url = reverse("commandes:commande-detail", args=[self.commande.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_list_commande_items(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("commandes:commande-items", args=[self.commande.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_stranger_cannot_list_others_commande_items(self):
        self.client.force_authenticate(user=self.other_client)
        url = reverse("commandes:commande-items", args=[self.commande.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)