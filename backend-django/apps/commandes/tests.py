from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from apps.panier.models import Panier, PanierItem
from apps.fidelite.models import CouponReduction
from .models import Commande, GroupeCommande, CommandeItem



# Adresse de livraison, obligatoire à la validation du panier.
ADRESSE_LIVRAISON = {
    "commune": "Cocody",
    "quartier": "Angré 8e Tranche",
    "point_de_repere": "Derrière la pharmacie",
    "telephone": "0700000001",
}

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
            role=role, statut_kyc=statut_kyc, email_verifie=True,
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
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data), 2)  # une commande par boutique
        self.assertEqual(Commande.objects.count(), 2)
        self.assertEqual(GroupeCommande.objects.count(), 1)

    def test_montant_total_correctement_calcule(self):
        self._ajouter_au_panier(self.variante1, 3)  # 3 x 1000 = 3000

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        commande = Commande.objects.get(boutique=self.boutique1)
        self.assertEqual(commande.montant_total, Decimal("3000.00"))

    def test_commande_item_snapshot_correct(self):
        self._ajouter_au_panier(self.variante1, 2)

        self.client.force_authenticate(user=self.client_user)
        self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        item = CommandeItem.objects.get(commande__boutique=self.boutique1)
        self.assertEqual(item.nom_produit, "Produit A")
        self.assertEqual(item.prix_unitaire, Decimal("1000.00"))
        self.assertEqual(item.quantite, 2)

    def test_stock_decremente_apres_validation(self):
        self._ajouter_au_panier(self.variante1, 4)

        self.client.force_authenticate(user=self.client_user)
        self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, 6)  # 10 - 4

    def test_panier_vide_apres_validation(self):
        self._ajouter_au_panier(self.variante1, 1)

        self.client.force_authenticate(user=self.client_user)
        self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        panier = Panier.objects.get(utilisateur=self.client_user)
        self.assertEqual(panier.items.count(), 0)

    # ---------- Cas d'erreur ----------

    def test_panier_vide_refuse(self):
        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_stock_insuffisant_bloque_toute_la_validation(self):
        self._ajouter_au_panier(self.variante1, 2)      # stock suffisant
        self._ajouter_au_panier(self.variante2, 999)    # stock insuffisant

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Rien ne doit avoir été créé (transaction atomique)
        self.assertEqual(Commande.objects.count(), 0)
        self.assertEqual(GroupeCommande.objects.count(), 0)
        # Le stock de variante1 ne doit pas avoir bougé non plus
        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, 10)

    def test_unauthenticated_cannot_validate(self):
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_articles_indisponibles_tous_listes_et_validation_refusee(self):
        """Toutes les lignes indisponibles sont nommées dans le refus, pas
        seulement la première ; rien n'est créé ni décrémenté."""
        troisieme = VarianteProduit.objects.create(produit=self.produit1, nom="Premium", prix=Decimal("1500"))
        troisieme.stock.quantite_disponible = 10
        troisieme.stock.save()
        self._ajouter_au_panier(self.variante1, 1)   # reste disponible
        self._ajouter_au_panier(troisieme, 1)        # variante désactivée
        self._ajouter_au_panier(self.variante2, 1)   # boutique suspendue
        troisieme.est_active = False
        troisieme.save(update_fields=["est_active"])
        self.boutique2.est_suspendue = True
        self.boutique2.save(update_fields=["est_suspendue"])

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(response.data["errors"])
        self.assertIn("Produit A (Premium)", message)
        self.assertIn("Produit B (Standard)", message)
        self.assertNotIn("Produit A (Standard)", message)
        self.assertEqual(Commande.objects.count(), 0)
        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, 10)
        self.assertEqual(Panier.objects.get(utilisateur=self.client_user).items.count(), 3)


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
            role=role, statut_kyc=statut_kyc, email_verifie=True,
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
        self.assertEqual(len(response.data["results"]), 1)

    def test_stranger_cannot_list_others_commande_items(self):
        self.client.force_authenticate(user=self.other_client)
        url = reverse("commandes:commande-items", args=[self.commande.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class CommandeAdminTestCase(APITestCase):
    """Le statut d'une commande ne doit jamais être modifiable depuis
    l'admin Django : seul le flux réel (paiement validé → signal →
    confirmation) doit pouvoir le faire — même principe que F-14
    (Paiement.statut) et F-15 (Livraison.status)."""

    def test_status_et_montant_total_readonly_dans_admin(self):
        from apps.commandes.admin import CommandeAdmin
        from django.contrib.admin.sites import AdminSite

        admin_instance = CommandeAdmin(Commande, AdminSite())
        self.assertIn("status", admin_instance.readonly_fields)
        self.assertIn("montant_total", admin_instance.readonly_fields)

    def test_champs_financiers_commande_item_readonly_dans_admin(self):
        from apps.commandes.admin import CommandeItemAdmin
        from django.contrib.admin.sites import AdminSite

        admin_instance = CommandeItemAdmin(CommandeItem, AdminSite())
        self.assertIn("prix_unitaire", admin_instance.readonly_fields)
        self.assertIn("quantite", admin_instance.readonly_fields)
        self.assertIn("nom_produit", admin_instance.readonly_fields)


class ValiderPanierCouponTestCase(APITestCase):
    """F-10 (audit sécurité) : CouponReduction.est_utilise n'était jamais
    posé à True nulle part, et rien ne reliait un coupon au flux de
    validation de commande — un même coupon pouvait donc être appliqué un
    nombre illimité de fois. Ces tests couvrent l'intégration réelle dans
    ValiderPanierView : application du coupon, marquage est_utilise, et
    répartition de la remise au prorata entre boutiques."""

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)

        self.vendeur1 = self._create_user("vendeur1@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique1 = Boutique.objects.create(proprietaire=self.vendeur1, nom="Boutique 1", est_active=True)
        self.produit1 = Produit.objects.create(boutique=self.boutique1, nom="Produit A", prix_base=Decimal("1000"))
        self.variante1 = VarianteProduit.objects.create(produit=self.produit1, nom="Standard", prix=Decimal("1000"))
        self.variante1.stock.quantite_disponible = 10
        self.variante1.stock.save()

        self.vendeur2 = self._create_user("vendeur2@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique2 = Boutique.objects.create(proprietaire=self.vendeur2, nom="Boutique 2", est_active=True)
        self.produit2 = Produit.objects.create(boutique=self.boutique2, nom="Produit B", prix_base=Decimal("3000"))
        self.variante2 = VarianteProduit.objects.create(produit=self.produit2, nom="Standard", prix=Decimal("3000"))
        self.variante2.stock.quantite_disponible = 5
        self.variante2.stock.save()

        self.url = reverse("commandes:valider-panier")

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS):
        return Utilisateur.objects.create_user(
            email=email, password="testpass123", nom="Test", prenom="User",
            role=role, statut_kyc=statut_kyc, email_verifie=True,
        )

    def _ajouter_au_panier(self, variante, quantite):
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=variante, quantite=quantite)
        return panier

    def test_coupon_valide_reduit_le_montant_et_est_marque_utilise(self):
        coupon = CouponReduction.objects.create(
            code="FID-TESTCODE", type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("10.00"), montant_minimum_commande=Decimal("0.00"),
        )
        self._ajouter_au_panier(self.variante1, 2)  # 2000

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"coupon_code": "fid-testcode", "adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        commande = Commande.objects.get(boutique=self.boutique1)
        self.assertEqual(commande.coupon_code, "FID-TESTCODE")
        self.assertEqual(commande.montant_remise, Decimal("200.00"))  # 10% de 2000
        self.assertEqual(commande.montant_total, Decimal("1800.00"))

        coupon.refresh_from_db()
        self.assertTrue(coupon.est_utilise)

    def test_remise_repartie_au_prorata_entre_boutiques(self):
        coupon = CouponReduction.objects.create(
            code="FID-MULTI", type_reduction=CouponReduction.TypeReduction.MONTANT_FIXE,
            valeur=Decimal("400.00"), montant_minimum_commande=Decimal("0.00"),
        )
        self._ajouter_au_panier(self.variante1, 1)  # boutique1 : 1000 (25% du panier)
        self._ajouter_au_panier(self.variante2, 1)  # boutique2 : 3000 (75% du panier)

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"coupon_code": "FID-MULTI", "adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        commande1 = Commande.objects.get(boutique=self.boutique1)
        commande2 = Commande.objects.get(boutique=self.boutique2)

        # 25%/75% de 400 = 100 / 300, la somme doit correspondre exactement
        self.assertEqual(commande1.montant_remise + commande2.montant_remise, Decimal("400.00"))
        self.assertEqual(commande1.montant_remise, Decimal("100.00"))
        self.assertEqual(commande2.montant_remise, Decimal("300.00"))

    def test_coupon_deja_utilise_est_refuse(self):
        coupon = CouponReduction.objects.create(
            code="FID-USED", type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("10.00"), est_utilise=True,
        )
        self._ajouter_au_panier(self.variante1, 1)

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"coupon_code": "FID-USED", "adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Commande.objects.count(), 0)

    def test_coupon_inexistant_est_refuse(self):
        self._ajouter_au_panier(self.variante1, 1)

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"coupon_code": "FID-INCONNU", "adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Commande.objects.count(), 0)

    def test_coupon_nominatif_dun_autre_client_est_refuse(self):
        autre_client = self._create_user("autre@test.com", Role.CLIENT)
        CouponReduction.objects.create(
            code="FID-PRIVE", type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("10.00"), client=autre_client,
        )
        self._ajouter_au_panier(self.variante1, 1)

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"coupon_code": "FID-PRIVE", "adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Commande.objects.count(), 0)

    def test_montant_minimum_non_atteint_est_refuse(self):
        CouponReduction.objects.create(
            code="FID-MINIMUM", type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("10.00"), montant_minimum_commande=Decimal("5000.00"),
        )
        self._ajouter_au_panier(self.variante1, 1)  # 1000, sous le minimum

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"coupon_code": "FID-MINIMUM", "adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Commande.objects.count(), 0)

    def test_validation_sans_coupon_laisse_champs_coupon_vides(self):
        self._ajouter_au_panier(self.variante1, 1)

        self.client.force_authenticate(user=self.client_user)
        response = self.client.post(self.url, {"adresse_livraison": ADRESSE_LIVRAISON}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        commande = Commande.objects.get(boutique=self.boutique1)
        self.assertEqual(commande.coupon_code, "")
        self.assertEqual(commande.montant_remise, Decimal("0.00"))

# =====================================================================
# REFONTE DU MODULE (diagnostic de septembre 2026)
# =====================================================================

import threading
from datetime import timedelta
from unittest import mock, skipUnless

from django.db import connection
from django.db.models import ProtectedError
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalogue.models import Stock
from apps.livraison.models import Livraison
from apps.notifications.models import Notification
from apps.paiements.models import Paiement
from .services import expirer_commandes_impayees

URL_VALIDER = "/api/commandes/valider-panier/"


class DonneesCycleDeVie:
    """Deux boutiques (b1 : Produit A à 1005 FCFA, b2 : Produit B à 2000
    en promo 1500), un client, un autre client, un administrateur."""

    def creer_donnees(self):
        creer = Utilisateur.objects.create_user
        self.client_user = creer(email="client@cmd.ci", password="x", nom="Kouassi", prenom="Awa", email_verifie=True)
        self.autre_client = creer(email="autre@cmd.ci", password="x", nom="Autre", prenom="Client", email_verifie=True)
        self.vendeur1 = creer(email="v1@cmd.ci", password="x", nom="V", prenom="Un", role=Role.VENDEUR,
                              statut_kyc=StatutKYC.VALIDE, email_verifie=True)
        self.vendeur2 = creer(email="v2@cmd.ci", password="x", nom="V", prenom="Deux", role=Role.VENDEUR,
                              statut_kyc=StatutKYC.VALIDE, email_verifie=True)
        self.admin = creer(email="admin@cmd.ci", password="x", nom="A", prenom="D", role=Role.ADMIN)
        self.boutique1 = Boutique.objects.create(proprietaire=self.vendeur1, nom="Cycle Un")
        self.boutique2 = Boutique.objects.create(proprietaire=self.vendeur2, nom="Cycle Deux")
        self.variante1 = self.variante(self.boutique1, "Produit A", Decimal("1005"))
        self.variante2 = self.variante(self.boutique2, "Produit B", Decimal("2000"), promo=Decimal("1500"))

    def variante(self, boutique, nom, prix, promo=None, stock=10):
        produit = Produit.objects.create(boutique=boutique, nom=nom, prix_base=prix)
        variante = VarianteProduit.objects.create(produit=produit, nom="Standard", prix=prix, prix_promo=promo)
        Stock.objects.filter(variante=variante).update(quantite_disponible=stock)
        return variante

    def stock(self, variante):
        return Stock.objects.get(variante=variante).quantite_disponible

    def commander(self, *lignes, client=None, **corps):
        client = client or self.client_user
        panier, _ = Panier.objects.get_or_create(utilisateur=client)
        for variante, quantite in lignes:
            PanierItem.objects.create(panier=panier, variante=variante, quantite=quantite)
        api = APIClient()
        api.force_authenticate(client)
        return api.post(URL_VALIDER, {"adresse_livraison": ADRESSE_LIVRAISON, **corps}, format="json")

    def payer(self, commande, statut=Paiement.Statut.VALIDE):
        return Paiement.objects.create(
            client=commande.client, commande=commande, montant=commande.montant_total, statut=statut,
            metadata={"commandes_couvertes": [str(commande.pk)]},
        )

    def en_tant_que(self, utilisateur):
        self.client.force_authenticate(utilisateur)


class AdresseLivraisonTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()

    def test_adresse_obligatoire_et_complete(self):
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=1)
        self.en_tant_que(self.client_user)
        cas = {
            "absente": {},
            "incomplète": {"adresse_livraison": {"commune": "Cocody", "telephone": "0700000001"}},
            "téléphone invalide": {"adresse_livraison": {**ADRESSE_LIVRAISON, "telephone": "abc"}},
        }
        for libelle, corps in cas.items():
            with self.subTest(libelle):
                self.assertEqual(self.client.post(URL_VALIDER, corps, format="json").status_code, 400)
        self.assertFalse(Commande.objects.exists())
        self.assertEqual(self.stock(self.variante1), 10)

    def test_adresse_stockee_et_reprise_par_le_paiement(self):
        self.commander((self.variante1, 1))
        commande = Commande.objects.get()
        self.assertEqual(commande.groupe.livraison_telephone, "0700000001")
        self.en_tant_que(self.client_user)
        # Avant : adresse vide au paiement → livraison « Abidjan, Côte d'Ivoire ».
        r = self.client.post("/api/paiements/initier/", {"commande_id": str(commande.pk), "methode": "espece_livraison"},
                             format="json")
        self.assertEqual(r.status_code, 201)
        livraison = Livraison.objects.get(commande=commande)
        self.assertEqual(livraison.adresse_livraison, "Cocody, Angré 8e Tranche — Derrière la pharmacie")
        detail = self.client.get(f"/api/livraison/{livraison.pk}/").data
        self.assertEqual(detail["telephone_contact"], "0700000001")

    def test_paiement_refuse_sans_adresse(self):
        groupe = GroupeCommande.objects.create(client=self.client_user)
        commande = Commande.objects.create(groupe=groupe, boutique=self.boutique1, client=self.client_user,
                                           montant_total=Decimal("1005"))
        self.en_tant_que(self.client_user)
        r = self.client.post("/api/paiements/initier/", {"commande_id": str(commande.pk), "methode": "wave"},
                             format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Paiement.objects.exists())


class MontantsTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()

    def test_montants_serveur_prix_fige_promo_comprise(self):
        r = self.commander((self.variante1, 2), (self.variante2, 1), montant_total="1", prix_unitaire="1")
        self.assertEqual(r.status_code, 201)
        commandes = {c.boutique_id: c for c in Commande.objects.all()}
        self.assertEqual(commandes[self.boutique1.pk].montant_total, Decimal("2010"))
        self.assertEqual(commandes[self.boutique2.pk].montant_total, Decimal("1500"))
        VarianteProduit.objects.filter(pk=self.variante2.pk).update(prix=Decimal("9000"), prix_promo=None)
        self.assertEqual(CommandeItem.objects.get(variante=self.variante2).prix_unitaire, Decimal("1500"))

    def test_remise_en_francs_entiers(self):
        # Avant : 10 % de 1005 → remise 100.50, total 904.50.
        CouponReduction.objects.create(code="DIX", type_reduction="pourcentage", valeur=Decimal("10"))
        self.assertEqual(self.commander((self.variante1, 1), coupon_code="DIX").status_code, 201)
        commande = Commande.objects.get()
        self.assertEqual((commande.montant_remise, commande.montant_total), (Decimal("100"), Decimal("905")))

    def test_remise_repartie_en_francs_entiers_entre_boutiques(self):
        CouponReduction.objects.create(code="SEPT", type_reduction="pourcentage", valeur=Decimal("7"))
        self.commander((self.variante1, 1), (self.variante2, 1), coupon_code="SEPT")
        commandes = list(Commande.objects.all())
        for commande in commandes:
            self.assertEqual(commande.montant_remise, commande.montant_remise.to_integral_value())
            self.assertEqual(commande.montant_total, commande.montant_total.to_integral_value())
        # 7 % de 2505 = 175.35 → 175 au total, réparti sans perte.
        self.assertEqual(sum(c.montant_remise for c in commandes), Decimal("175"))


class AnnulationClientTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 3))
        self.commande = Commande.objects.get()
        self.url = f"/api/commandes/{self.commande.pk}/annuler/"

    def test_annulation_restitue_le_stock_une_seule_fois(self):
        self.assertEqual(self.stock(self.variante1), 7)
        self.en_tant_que(self.client_user)
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["status"], r.data["motif_annulation"]), ("annulee", "client"))
        self.assertEqual(self.stock(self.variante1), 10)
        self.assertEqual(self.client.post(self.url).status_code, 409)
        self.assertEqual(self.stock(self.variante1), 10)

    def test_commande_payee_annulable_avant_preparation_et_paiement_a_rembourser(self):
        paiement = self.payer(self.commande)
        Commande.objects.filter(pk=self.commande.pk).update(status="confirmee")
        self.en_tant_que(self.client_user)
        self.assertEqual(self.client.post(self.url).status_code, 200)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.A_REMBOURSER)
        self.assertEqual(paiement.metadata["remboursements_dus"][0]["montant"], "3015.00")
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre="Paiement à rembourser").exists())

    def test_plus_annulable_par_le_client_une_fois_en_preparation(self):
        Commande.objects.filter(pk=self.commande.pk).update(status="preparation")
        self.en_tant_que(self.client_user)
        self.assertEqual(self.client.post(self.url).status_code, 409)
        self.assertEqual(self.stock(self.variante1), 7)

    def test_commande_dun_autre_client_404(self):
        self.en_tant_que(self.autre_client)
        self.assertEqual(self.client.post(self.url).status_code, 404)


class ExpirationTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 2))
        self.commande = Commande.objects.get()

    def vieillir(self, minutes):
        Commande.objects.filter(pk=self.commande.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))

    def test_commande_non_payee_annulee_apres_30_minutes(self):
        self.vieillir(29)
        self.assertEqual(expirer_commandes_impayees(), 0)
        self.vieillir(31)
        self.assertEqual(expirer_commandes_impayees(), 1)
        self.commande.refresh_from_db()
        self.assertEqual((self.commande.status, self.commande.motif_annulation), ("annulee", "expiration"))
        self.assertEqual(self.stock(self.variante1), 10)
        self.assertEqual(expirer_commandes_impayees(), 0)  # une seule restitution
        self.assertEqual(self.stock(self.variante1), 10)

    def test_commande_confirmee_jamais_expiree(self):
        Commande.objects.filter(pk=self.commande.pk).update(status="confirmee")
        self.vieillir(120)
        self.assertEqual(expirer_commandes_impayees(), 0)

    def test_paiement_recu_apres_expiration(self):
        paiement = self.payer(self.commande, statut=Paiement.Statut.EN_ATTENTE)
        self.vieillir(31)
        expirer_commandes_impayees()
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.ANNULE)  # en attente, commande annulée

        tardif = self.payer(self.commande, statut=Paiement.Statut.EN_ATTENTE)
        tardif.valider(donnees_supplementaires={"commandes_couvertes": ["falsifie"]})
        tardif.refresh_from_db()
        self.commande.refresh_from_db()
        self.assertEqual(tardif.statut, Paiement.Statut.A_REMBOURSER)
        self.assertEqual(tardif.metadata["commandes_couvertes"], [str(self.commande.pk)])  # clé interne protégée
        self.assertEqual(self.commande.status, "annulee")  # jamais réactivée
        self.assertFalse(Livraison.objects.filter(commande=self.commande).exists())
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre="Paiement à rembourser").exists())


class MachineAEtatsTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 1))
        self.commande = Commande.objects.get()
        self.livraison = Livraison.objects.create(commande=self.commande, adresse_livraison="x", livreur=self.admin)

    def preparer(self):
        self.en_tant_que(self.vendeur1)
        return self.client.post(f"/api/commandes/vendeur/{self.commande.pk}/preparation/")

    def changer_livraison(self, statut):
        self.en_tant_que(self.admin)
        return self.client.patch(f"/api/livraison/{self.livraison.pk}/statut/", {"status": statut})

    def statut(self):
        self.commande.refresh_from_db()
        return self.commande.status

    def test_parcours_complet(self):
        self.assertEqual(self.preparer().status_code, 409)  # pas encore payée (creee)
        Commande.objects.filter(pk=self.commande.pk).update(status="confirmee")
        self.assertEqual(self.preparer().status_code, 200)
        self.assertEqual(self.preparer().status_code, 409)  # pas deux fois
        self.assertEqual(self.changer_livraison("expediee").status_code, 200)
        self.assertEqual(self.statut(), "expediee")
        self.assertEqual(self.changer_livraison("en_cours").status_code, 200)
        self.assertEqual(self.statut(), "expediee")
        self.assertEqual(self.changer_livraison("livree").status_code, 200)
        self.assertEqual(self.statut(), "livree")

    def test_livraison_ne_peut_pas_sauter_la_preparation(self):
        # Avant : la commande restait « confirmée » même livrée.
        Commande.objects.filter(pk=self.commande.pk).update(status="confirmee")
        r = self.changer_livraison("expediee")
        self.assertEqual(r.status_code, 409)
        self.livraison.refresh_from_db()
        self.assertEqual((self.livraison.status, self.statut()), ("en_attente", "confirmee"))

    def test_commande_annulee_ne_peut_pas_etre_expediee(self):
        Commande.objects.filter(pk=self.commande.pk).update(status="annulee", motif_annulation="client")
        self.assertEqual(self.changer_livraison("expediee").status_code, 409)


class EspaceVendeurTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 1), (self.variante2, 1))
        self.commande_b1 = Commande.objects.get(boutique=self.boutique1)
        self.commande_b2 = Commande.objects.get(boutique=self.boutique2)

    def test_le_vendeur_ne_voit_que_sa_boutique(self):
        # Avant : liste vide (les commandes n'étaient filtrées que par client).
        self.en_tant_que(self.vendeur1)
        r = self.client.get("/api/commandes/vendeur/")
        self.assertEqual([c["id"] for c in r.data["results"]], [str(self.commande_b1.pk)])
        self.assertEqual([a["nom_produit"] for a in r.data["results"][0]["articles"]], ["Produit A"])
        for url in (f"/api/commandes/vendeur/{self.commande_b2.pk}/", f"/api/commandes/vendeur/{self.commande_b2.pk}/preparation/"):
            with self.subTest(url):
                methode = self.client.post if url.endswith("preparation/") else self.client.get
                self.assertEqual(methode(url).status_code, 404)

    def test_donnees_client_minimales(self):
        self.en_tant_que(self.vendeur1)
        detail = self.client.get(f"/api/commandes/vendeur/{self.commande_b1.pk}/").data
        self.assertEqual(detail["client"], "Awa K.")
        self.assertEqual(detail["adresse_livraison"]["telephone"], "0700000001")
        contenu = str(detail)
        self.assertNotIn("client@cmd.ci", contenu)
        self.assertNotIn("Kouassi", contenu)

    def test_reserve_aux_vendeurs_valides(self):
        staff = Utilisateur.objects.create_user(email="st@cmd.ci", password="x", nom="S", prenom="T", is_staff=True)
        for utilisateur in (self.client_user, staff, self.admin):
            with self.subTest(utilisateur.email):
                self.en_tant_que(utilisateur)
                self.assertEqual(self.client.get("/api/commandes/vendeur/").status_code, 403)

    def test_boutique_suspendue_honore_seulement_les_commandes_payees(self):
        Commande.objects.filter(pk=self.commande_b1.pk).update(status="confirmee")
        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.en_tant_que(self.vendeur1)
        url = f"/api/commandes/vendeur/{self.commande_b1.pk}/preparation/"
        self.assertEqual(self.client.post(url).status_code, 403)  # confirmée mais non encaissée (espèces)
        self.payer(self.commande_b1)
        self.assertEqual(self.client.post(url).status_code, 200)

    def test_listes_sans_n_plus_1(self):
        def compter(url):
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(ctx.captured_queries)

        self.en_tant_que(self.vendeur1)
        avant = compter("/api/commandes/vendeur/")
        for _ in range(3):
            self.commander((self.variante1, 1))
        self.en_tant_que(self.vendeur1)
        self.assertEqual(compter("/api/commandes/vendeur/"), avant)


class BoutiqueIndisponibleAuPaiementTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 2), (self.variante2, 1))
        self.groupe = GroupeCommande.objects.get()
        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.en_tant_que(self.client_user)

    def test_paiement_refuse_commande_annulee_stock_restitue(self):
        commande = Commande.objects.get(boutique=self.boutique1)
        r = self.client.post("/api/paiements/initier/", {"commande_id": str(commande.pk), "methode": "wave"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("suspend", str(r.data).lower())
        commande.refresh_from_db()
        self.assertEqual((commande.status, commande.motif_annulation), ("annulee", "boutique_indisponible"))
        self.assertEqual(self.stock(self.variante1), 10)

    def test_groupe_payable_ensuite_pour_le_reste(self):
        corps = {"groupe_commande_id": str(self.groupe.pk), "methode": "wave"}
        self.assertEqual(self.client.post("/api/paiements/initier/", corps, format="json").status_code, 400)
        r = self.client.post("/api/paiements/initier/", corps, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Decimal(r.data["montant"]), Decimal("1500"))


class AnnulationAdministrationTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 1))
        self.commande = Commande.objects.get()
        self.url = f"/api/commandes/administration/{self.commande.pk}/annuler/"

    def test_administration_annule_jusqua_la_preparation(self):
        Commande.objects.filter(pk=self.commande.pk).update(status="preparation")
        self.en_tant_que(self.admin)
        r = self.client.post(self.url)
        self.assertEqual((r.status_code, r.data["motif_annulation"]), (200, "administration"))
        self.assertEqual(self.stock(self.variante1), 10)

    def test_pas_apres_expedition(self):
        Commande.objects.filter(pk=self.commande.pk).update(status="expediee")
        self.en_tant_que(self.admin)
        self.assertEqual(self.client.post(self.url).status_code, 409)

    def test_reserve_a_ladministration(self):
        staff = Utilisateur.objects.create_user(email="st@cmd.ci", password="x", nom="S", prenom="T", is_staff=True)
        for utilisateur in (self.client_user, self.vendeur1, staff):
            with self.subTest(utilisateur.email):
                self.en_tant_que(utilisateur)
                self.assertEqual(self.client.post(self.url).status_code, 403)


class DetailEtHistoriqueTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 2))
        self.commande = Commande.objects.get()

    def test_detail_client_avec_articles_et_adresse(self):
        self.en_tant_que(self.client_user)
        detail = self.client.get(f"/api/commandes/{self.commande.pk}/").data
        self.assertEqual([a["quantite"] for a in detail["articles"]], [2])
        self.assertEqual(detail["adresse_livraison"]["commune"], "Cocody")
        self.en_tant_que(self.autre_client)
        self.assertEqual(self.client.get(f"/api/commandes/{self.commande.pk}/").status_code, 404)

    def test_collision_de_numero_retentee(self):
        existant = self.commande.numero_commande
        with mock.patch("apps.commandes.models.generer_numero_commande", side_effect=[existant, "CMD-2026-0000ABCD"]):
            self.assertEqual(self.commander((self.variante1, 1)).status_code, 201)
        self.assertTrue(Commande.objects.filter(numero_commande="CMD-2026-0000ABCD").exists())

    def test_historique_protege_contre_la_suppression(self):
        # Avant : supprimer le compte effaçait ses commandes (CASCADE).
        with self.assertRaises(ProtectedError):
            self.client_user.delete()
        with self.assertRaises(ProtectedError):
            self.boutique1.delete()
        self.assertEqual(Commande.objects.count(), 1)


@skipUnless(connection.vendor == "postgresql", "Concurrence réelle : PostgreSQL uniquement")
class ConcurrenceCommandesTests(DonneesCycleDeVie, TransactionTestCase):
    def setUp(self):
        self.creer_donnees()

    def en_parallele(self, action, fois=2):
        barriere = threading.Barrier(fois)
        resultats = []

        def executer():
            try:
                barriere.wait()
                resultats.append(action())
            finally:
                connection.close()

        threads = [threading.Thread(target=executer) for _ in range(fois)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return sorted(resultats)

    def appel(self, url, corps=None):
        def faire():
            api = APIClient()
            api.force_authenticate(self.client_user)
            return api.post(url, corps or {}, format="json").status_code
        return faire

    def test_double_validation_une_seule_commande(self):
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=2)
        codes = self.en_parallele(self.appel(URL_VALIDER, {"adresse_livraison": ADRESSE_LIVRAISON}))
        self.assertEqual(codes, [201, 400])
        self.assertEqual(Commande.objects.count(), 1)
        self.assertEqual(self.stock(self.variante1), 8)

    def test_double_annulation_stock_restitue_une_fois(self):
        self.commander((self.variante1, 3))
        commande = Commande.objects.get()
        codes = self.en_parallele(self.appel(f"/api/commandes/{commande.pk}/annuler/"))
        self.assertEqual(codes, [200, 409])
        self.assertEqual(self.stock(self.variante1), 10)
