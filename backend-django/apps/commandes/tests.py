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

# Tarif d'une commune du district d'Abidjan (Cocody) créé par migration
# (livraison 0004 / 0005) : ajouté au montant de chaque commande (une
# commande = un colis = des frais).
FRAIS_ABIDJAN = Decimal("1500")

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
        self.assertEqual(commande.montant_total, Decimal("3000.00") + FRAIS_ABIDJAN)

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
        # La remise ne porte que sur les articles, jamais sur les frais de livraison.
        self.assertEqual(commande.montant_total, Decimal("1800.00") + FRAIS_ABIDJAN)

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

from django.db import OperationalError, connection, transaction
from django.db.models import F, ProtectedError
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalogue.models import Stock
from apps.livraison.models import Livraison, TarifLivraison
from apps.livraison.tests import recreer_tarifs_initiaux
from apps.notifications.models import Notification
from apps.paiements.models import Paiement
from apps.paiements.services import valider_paiement
from apps.paiements.tests import attendre_transactions_bloquees, recreer_bareme_plateforme
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
        paiement = Paiement.objects.create(
            client=commande.client, commande=commande, montant=commande.montant_total, statut=statut,
            fournisseur="simule",
        )
        paiement.commandes.add(commande)
        return paiement

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

    def test_corps_qui_n_est_pas_un_objet_400_comme_la_simulation(self):
        # Avant : 500 à la validation (request.data.get sur une liste).
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=1)
        self.en_tant_que(self.client_user)
        for corps in ("[1, 2]", "[]", '"adresse"', "42", "true", "null"):
            with self.subTest(corps):
                validation = self.client.post(URL_VALIDER, corps, content_type="application/json")
                simulation = self.client.post(URL_SIMULER, corps, content_type="application/json")
                self.assertEqual((validation.status_code, simulation.status_code), (400, 400))
                self.assertEqual(list(validation.data["errors"]), ["non_field_errors"])
                self.assertEqual(validation.data["errors"], simulation.data["errors"])
                self.assertEqual(validation.data["detail"], simulation.data["detail"])
        self.assertFalse(Commande.objects.exists())
        self.assertEqual(self.stock(self.variante1), 10)

    def test_adresse_stockee_et_reprise_par_le_paiement(self):
        self.commander((self.variante1, 1))
        commande = Commande.objects.get()
        self.assertEqual(commande.groupe.livraison_telephone, "0700000001")
        self.en_tant_que(self.client_user)
        # Avant : adresse vide au paiement → livraison « Abidjan, Côte d'Ivoire ».
        r = self.client.post("/api/paiements/initier/", {"commande_id": str(commande.pk), "methode": "wave"},
                             format="json")
        self.assertEqual(r.status_code, 201)
        valider_paiement(Paiement.objects.get(pk=r.data["id"]))  # paiement en ligne confirmé
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
        self.assertEqual(commandes[self.boutique1.pk].montant_total, Decimal("2010") + FRAIS_ABIDJAN)
        self.assertEqual(commandes[self.boutique2.pk].montant_total, Decimal("1500") + FRAIS_ABIDJAN)
        VarianteProduit.objects.filter(pk=self.variante2.pk).update(prix=Decimal("9000"), prix_promo=None)
        self.assertEqual(CommandeItem.objects.get(variante=self.variante2).prix_unitaire, Decimal("1500"))

    def test_remise_en_francs_entiers(self):
        # Avant : 10 % de 1005 → remise 100.50, total 904.50.
        CouponReduction.objects.create(code="DIX", type_reduction="pourcentage", valeur=Decimal("10"))
        self.assertEqual(self.commander((self.variante1, 1), coupon_code="DIX").status_code, 201)
        commande = Commande.objects.get()
        self.assertEqual((commande.montant_remise, commande.montant_total), (Decimal("100"), Decimal("905") + FRAIS_ABIDJAN))

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

    def test_commande_payee_annulable_avant_preparation_et_remboursement_du(self):
        paiement = self.payer(self.commande)
        Commande.objects.filter(pk=self.commande.pk).update(status="confirmee")
        self.en_tant_que(self.client_user)
        self.assertEqual(self.client.post(self.url).status_code, 200)
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.VALIDE)  # encaissé ; le remboursement est à part
        remboursement = paiement.remboursements.get()
        # Annulation : le client récupère tout ce qu'il a payé, frais de livraison compris.
        self.assertEqual((remboursement.montant, remboursement.statut), (Decimal("3015") + FRAIS_ABIDJAN, "a_traiter"))
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre="Remboursement à traiter").exists())

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
        # Toujours « en attente » chez le fournisseur simulé : la commande ne
        # part pas dans le délai de grâce, puis expire quand même après.
        self.vieillir(31)
        self.assertEqual(expirer_commandes_impayees(), 0)
        self.vieillir(61)
        expirer_commandes_impayees()
        paiement.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.ANNULE)  # en attente, commande annulée

        valider_paiement(paiement)  # succès tardif
        paiement.refresh_from_db()
        self.commande.refresh_from_db()
        self.assertEqual(paiement.statut, Paiement.Statut.VALIDE)
        self.assertEqual(paiement.remboursements.get().motif, "commande_annulee")
        self.assertEqual(self.commande.status, "annulee")  # jamais réactivée
        self.assertFalse(Livraison.objects.filter(commande=self.commande).exists())
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre="Remboursement à traiter").exists())

    def test_A1_interblocage_sur_une_commande_n_arrete_pas_les_suivantes(self):
        self.commander((self.variante1, 1))
        Commande.objects.update(created_at=timezone.now() - timedelta(minutes=31))
        premiere, seconde = Commande.objects.order_by("pk")
        from . import services
        vraie = services.annuler_commande

        def annuler(commande, motif, **options):
            if commande.pk == premiere.pk:
                raise OperationalError("deadlock detected")
            return vraie(commande, motif, **options)

        with mock.patch.object(services, "annuler_commande", side_effect=annuler), \
                self.assertLogs("apps.commandes.services", "WARNING") as journaux:
            self.assertEqual(expirer_commandes_impayees(), 1)
        self.assertIn(f"Expiration de la commande {premiere.numero_commande} reportée (deadlock detected)",
                      journaux.output[0])
        premiere.refresh_from_db()
        seconde.refresh_from_db()
        self.assertEqual((premiere.status, seconde.status), ("creee", "annulee"))
        # Reprise à l'exécution suivante.
        self.assertEqual(expirer_commandes_impayees(), 1)
        premiere.refresh_from_db()
        self.assertEqual(premiere.status, "annulee")
        self.assertEqual(self.stock(self.variante1), 10)

    def test_M1_paiement_en_attente_reussi_chez_le_fournisseur_confirme_au_lieu_d_expirer(self):
        from apps.paiements.fournisseurs.simule import definir_etat_distant

        paiement = self.payer(self.commande, statut=Paiement.Statut.EN_ATTENTE)
        definir_etat_distant(paiement.reference, "succes", int(paiement.montant))
        self.vieillir(31)
        self.assertEqual(expirer_commandes_impayees(), 0)
        paiement.refresh_from_db()
        self.commande.refresh_from_db()
        self.assertEqual((paiement.statut, self.commande.status), ("valide", "confirmee"))
        self.assertEqual(self.stock(self.variante1), 8)  # stock jamais restitué


class MachineAEtatsTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        self.commander((self.variante1, 1))
        self.commande = Commande.objects.get()
        # L'administration ne fait plus les étapes du livreur (module livraison).
        self.livreur = Utilisateur.objects.create_user(email="livreur@cmd.ci", password="x", nom="L", prenom="Ivreur",
                                                       role=Role.LIVREUR)
        self.livraison = Livraison.objects.create(commande=self.commande, adresse_livraison="x", livreur=self.livreur)

    def preparer(self):
        self.en_tant_que(self.vendeur1)
        return self.client.post(f"/api/commandes/vendeur/{self.commande.pk}/preparation/")

    def changer_livraison(self, statut):
        self.en_tant_que(self.livreur)
        self.livraison.refresh_from_db()
        # « livrée » exige le code de livraison donné par le client.
        return self.client.patch(f"/api/livraison/{self.livraison.pk}/statut/",
                                 {"status": statut, "code": self.livraison.code_chiffre})

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
        self.assertEqual(self.client.post(url).status_code, 403)  # confirmée mais non encaissée
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
        self.assertEqual(Decimal(r.data["montant"]), Decimal("1500") + FRAIS_ABIDJAN)


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
        # Un TransactionTestCase précédent vide la base, y compris le barème
        # de frais par défaut créé par migration.
        recreer_bareme_plateforme()
        # Idem pour les tarifs de livraison (défauts et communes du district).
        recreer_tarifs_initiaux()
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

    def test_stocks_verrouilles_dans_l_ordre_des_cles_quel_que_soit_le_plan(self):
        """Sans ORDER BY, les stocks seraient verrouillés dans l'ordre que
        produit le plan : ordre des clés par un index, ordre physique par une
        lecture séquentielle. Les plans sont forcés par connexion (SET
        enable_*), comme deux plans choisis en même temps par le
        planificateur. Un autre checkout tient Y ; l'annulation (lecture
        séquentielle) puis le checkout (index) attendent. Verrouillés dans
        l'ordre des clés, ils passent l'un après l'autre ; dans l'ordre du
        plan (Y puis X contre X puis Y), ils s'interbloqueraient."""
        x = self.variante1
        y = self.variante(self.boutique1, "Produit C", Decimal("1000"))
        self.assertLess(x.pk, y.pk)
        self.assertEqual(self.commander((x, 2), (y, 2)).status_code, 201)
        commande = Commande.objects.get()
        panier, _ = Panier.objects.get_or_create(utilisateur=self.autre_client)
        PanierItem.objects.create(panier=panier, variante=x, quantite=1)
        PanierItem.objects.create(panier=panier, variante=y, quantite=1)
        # X vient d'être vendu : sa ligne vivante est physiquement après Y.
        Stock.objects.filter(variante=x).update(quantite_disponible=F("quantite_disponible"))
        index = ("enable_seqscan", "enable_bitmapscan")
        sequentiel = ("enable_indexscan", "enable_indexonlyscan", "enable_bitmapscan")

        def ordre(plan):
            with transaction.atomic(), connection.cursor() as curseur:
                for reglage in plan:
                    curseur.execute(f"SET LOCAL {reglage} = off")
                return [stock.variante_id for stock in Stock.objects.filter(variante_id__in=[x.pk, y.pk])]

        self.assertEqual((ordre(index), ordre(sequentiel)), ([x.pk, y.pk], [y.pk, x.pk]))

        resultats = {}

        def fil(nom, plan, utilisateur, url, corps=None):
            def executer():
                try:
                    with connection.cursor() as curseur:
                        for reglage in plan:
                            curseur.execute(f"SET {reglage} = off")  # connexion du fil, fermée ensuite
                    api = APIClient()
                    api.force_authenticate(utilisateur)
                    resultats[nom] = api.post(url, corps or {}, format="json").status_code
                except Exception as erreur:  # noqa: BLE001 — remontée au test
                    resultats[nom] = erreur
                finally:
                    connection.close()
            return threading.Thread(target=executer)

        annulation = fil("annulation", sequentiel, self.client_user, f"/api/commandes/{commande.pk}/annuler/")
        checkout = fil("checkout", index, self.autre_client, URL_VALIDER, {"adresse_livraison": ADRESSE_LIVRAISON})
        try:
            with transaction.atomic():
                # Un autre checkout (ou le vendeur, catalogue) tient le stock de Y.
                Stock.objects.select_for_update().get(variante=y)
                annulation.start()
                self.assertTrue(attendre_transactions_bloquees(1))  # l'annulation attend Y
                checkout.start()
                self.assertTrue(attendre_transactions_bloquees(2))  # le checkout attend aussi
        finally:
            for thread in (annulation, checkout):
                if thread.ident is not None:
                    thread.join(timeout=30)
        self.assertFalse(annulation.is_alive() or checkout.is_alive(), "Fil bloqué")
        self.assertEqual(resultats, {"annulation": 200, "checkout": 201})
        # Annulation : 2 + 2 rendus ; checkout : 1 + 1 pris.
        self.assertEqual((self.stock(x), self.stock(y)), (9, 9))


# =====================================================================
# FRAIS DE LIVRAISON (docs/MODULE_LIVRAISON.md, « Frais de livraison »)
# =====================================================================

URL_SIMULER = "/api/commandes/simuler-frais/"


class FraisDeLivraisonCheckoutTests(DonneesCycleDeVie, APITestCase):
    """Tarifs de la migration : communes du district d'Abidjan et défaut
    d'Abidjan 1 500, hors Abidjan 3 000. Port-Bouët passe à 1 000 ici."""

    def setUp(self):
        self.creer_donnees()
        TarifLivraison.objects.filter(commune="Port-Bouët").update(montant=1000)

    def adresse(self, **champs):
        return {**ADRESSE_LIVRAISON, **champs}

    def par_boutique(self):
        return {c.boutique_id: c for c in Commande.objects.all()}

    def commande_pour(self, **adresse):
        PanierItem.objects.all().delete()
        Commande.objects.all().delete()
        r = self.commander((self.variante1, 1), adresse_livraison=self.adresse(**adresse))
        self.assertEqual(r.status_code, 201, r.data)
        return Commande.objects.select_related("groupe").get()

    def test_une_commande_par_boutique_et_des_frais_par_commande(self):
        r = self.commander((self.variante1, 2), (self.variante2, 1))
        self.assertEqual(r.status_code, 201, r.data)
        commandes = self.par_boutique()
        self.assertEqual(commandes[self.boutique1.pk].montant_total, Decimal("2010") + FRAIS_ABIDJAN)
        self.assertEqual(commandes[self.boutique2.pk].montant_total, Decimal("1500") + FRAIS_ABIDJAN)
        for commande in commandes.values():
            self.assertEqual((commande.frais_livraison, commande.livraison_offerte, commande.frais_livraison_vendeur),
                             (FRAIS_ABIDJAN, False, Decimal("0")))
            self.assertEqual(commande.montant_hors_livraison, commande.montant_total - FRAIS_ABIDJAN)
        self.assertEqual(GroupeCommande.objects.get().livraison_zone, "abidjan")
        # Réponse client : frais visibles, part du vendeur jamais exposée.
        self.assertEqual(r.data[0]["frais_livraison"], "1500.00")
        self.assertNotIn("frais_livraison_vendeur", r.data[0])

    def test_commune_d_abidjan_ecrite_de_plusieurs_facons(self):
        for commune in ("Port-Bouët", "  port bouet ", "PORT-BOUET", "Port Bouët", "port-bouët"):
            with self.subTest(commune):
                commande = self.commande_pour(commune=commune)
                self.assertEqual((commande.groupe.livraison_zone, commande.frais_livraison),
                                 ("abidjan", Decimal("1000")))
        for commune in ("Attécoubé", "attecoube", "YOPOUGON", "Songon"):
            with self.subTest(commune):
                self.assertEqual(self.commande_pour(commune=commune).groupe.livraison_zone, "abidjan")

    def test_ville_hors_abidjan_declaree_en_zone_abidjan(self):
        # La zone envoyée par le client est ignorée : Bouaké paie le tarif
        # hors Abidjan, jamais celui d'Abidjan.
        commande = self.commande_pour(zone="abidjan", commune="Bouaké")
        self.assertEqual((commande.groupe.livraison_zone, commande.frais_livraison), ("hors_abidjan", Decimal("3000")))
        # Et une commune du district déclarée « hors Abidjan » reste à Abidjan.
        commande = self.commande_pour(zone="hors_abidjan", commune="Cocody")
        self.assertEqual((commande.groupe.livraison_zone, commande.frais_livraison), ("abidjan", FRAIS_ABIDJAN))

    def test_ville_hors_abidjan_avec_tarif_propre(self):
        TarifLivraison.objects.create(zone="hors_abidjan", commune="Bouaké", montant=2500)
        commande = self.commande_pour(commune="bouake")
        self.assertEqual((commande.groupe.livraison_zone, commande.frais_livraison), ("hors_abidjan", Decimal("2500")))

    def test_tarif_de_commune_inactif_retombe_sur_le_defaut_de_sa_zone(self):
        TarifLivraison.objects.filter(commune="Port-Bouët").update(est_actif=False)
        TarifLivraison.objects.filter(zone="abidjan", commune="").update(montant=1200)
        # Toujours une commune du district : défaut d'Abidjan, pas hors Abidjan.
        commande = self.commande_pour(commune="Port-Bouët")
        self.assertEqual((commande.groupe.livraison_zone, commande.frais_livraison), ("abidjan", Decimal("1200")))

    def test_montants_envoyes_par_le_client_ignores(self):
        r = self.commander((self.variante1, 1), frais_livraison="0", livraison_offerte=True, montant_total="1")
        self.assertEqual(r.status_code, 201)
        commande = Commande.objects.get()
        self.assertEqual((commande.frais_livraison, commande.livraison_offerte, commande.montant_total),
                         (FRAIS_ABIDJAN, False, Decimal("1005") + FRAIS_ABIDJAN))

    def test_sans_tarif_503_et_rien_n_est_cree(self):
        TarifLivraison.objects.all().delete()
        r = self.commander((self.variante1, 2))
        self.assertEqual(r.status_code, 503)
        self.assertFalse(Commande.objects.exists())
        self.assertEqual(self.stock(self.variante1), 10)
        self.assertEqual(PanierItem.objects.count(), 1)

    def test_livraison_offerte_par_la_boutique(self):
        Boutique.objects.filter(pk=self.boutique1.pk).update(livraison_offerte=True)
        self.commander((self.variante1, 1), (self.variante2, 1))
        commandes = self.par_boutique()
        offerte = commandes[self.boutique1.pk]
        self.assertEqual((offerte.frais_livraison, offerte.livraison_offerte, offerte.frais_livraison_vendeur,
                          offerte.montant_total), (Decimal("0"), True, FRAIS_ABIDJAN, Decimal("1005")))
        payante = commandes[self.boutique2.pk]
        self.assertEqual((payante.frais_livraison, payante.livraison_offerte), (FRAIS_ABIDJAN, False))
        # Le client voit « livraison offerte », jamais ce qu'elle coûte au vendeur.
        self.en_tant_que(self.client_user)
        detail = self.client.get(f"/api/commandes/{offerte.pk}/").data
        self.assertEqual((detail["frais_livraison"], detail["livraison_offerte"]), ("0.00", True))
        self.assertNotIn("frais_livraison_vendeur", detail)
        # Le vendeur, lui, voit ce qui sera déduit de son reversement.
        self.en_tant_que(self.vendeur1)
        detail = self.client.get(f"/api/commandes/vendeur/{offerte.pk}/").data
        self.assertEqual(detail["frais_livraison_vendeur"], "1500.00")

    def test_frais_figes_dans_la_commande(self):
        self.commander((self.variante1, 1))
        TarifLivraison.objects.filter(zone="abidjan").update(montant=9000)
        Boutique.objects.filter(pk=self.boutique1.pk).update(livraison_offerte=True)
        commande = Commande.objects.get()
        self.assertEqual((commande.frais_livraison, commande.livraison_offerte, commande.montant_total),
                         (FRAIS_ABIDJAN, False, Decimal("1005") + FRAIS_ABIDJAN))

    def test_le_coupon_ne_reduit_jamais_les_frais(self):
        CouponReduction.objects.create(code="TOUT", type_reduction="montant_fixe", valeur=Decimal("50000"))
        self.commander((self.variante1, 1), coupon_code="TOUT")
        commande = Commande.objects.get()
        # Remise plafonnée aux articles : les frais restent dus.
        self.assertEqual((commande.montant_remise, commande.montant_total), (Decimal("1005"), FRAIS_ABIDJAN))


class SimulationDuCheckoutTests(DonneesCycleDeVie, APITestCase):
    def setUp(self):
        self.creer_donnees()
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=2)
        PanierItem.objects.create(panier=panier, variante=self.variante2, quantite=1)
        self.en_tant_que(self.client_user)

    def simuler(self, **corps):
        return self.client.post(URL_SIMULER, {"adresse_livraison": ADRESSE_LIVRAISON, **corps}, format="json")

    def test_memes_montants_que_la_validation(self):
        CouponReduction.objects.create(code="SEPT", type_reduction="pourcentage", valeur=Decimal("7"))
        Boutique.objects.filter(pk=self.boutique2.pk).update(livraison_offerte=True)
        simulation = self.simuler(coupon_code="SEPT")
        self.assertEqual(simulation.status_code, 200, simulation.data)
        self.assertEqual(simulation.data["zone"], "abidjan")
        # 7 % de 3510 = 245.70 → 245 ; frais : boutique 1 seulement.
        self.assertEqual((simulation.data["total_articles"], simulation.data["total_remise"],
                          simulation.data["total_frais_livraison"], simulation.data["total_a_payer"]),
                         (3510, 245, 1500, 3510 - 245 + 1500))

        r = self.client.post(URL_VALIDER, {"adresse_livraison": ADRESSE_LIVRAISON, "coupon_code": "SEPT"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.data)
        commandes = {c.boutique_id: c for c in Commande.objects.all()}
        for ligne in simulation.data["commandes"]:
            commande = commandes[ligne["boutique"]]
            self.assertEqual(
                (ligne["remise"], ligne["frais_livraison"], ligne["livraison_offerte"], ligne["montant_total"]),
                (int(commande.montant_remise), int(commande.frais_livraison), commande.livraison_offerte,
                 int(commande.montant_total)),
            )
        self.assertEqual(simulation.data["total_a_payer"], int(sum(c.montant_total for c in commandes.values())))

    def test_aucune_ecriture(self):
        coupon = CouponReduction.objects.create(code="DIX", type_reduction="pourcentage", valeur=Decimal("10"))
        self.assertEqual(self.simuler(coupon_code="DIX").status_code, 200)
        coupon.refresh_from_db()
        self.assertFalse(coupon.est_utilise)
        self.assertFalse(Commande.objects.exists())
        self.assertEqual((self.stock(self.variante1), PanierItem.objects.count()), (10, 2))

    def test_part_du_vendeur_jamais_exposee(self):
        Boutique.objects.filter(pk=self.boutique1.pk).update(livraison_offerte=True)
        self.assertNotIn("frais_livraison_vendeur", str(self.simuler().data))

    def test_refus(self):
        self.assertEqual(self.simuler(coupon_code="INCONNU").status_code, 400)
        self.assertEqual(self.client.post(URL_SIMULER, {}, format="json").status_code, 400)
        TarifLivraison.objects.all().delete()
        self.assertEqual(self.simuler().status_code, 503)
        PanierItem.objects.all().delete()
        recreer_tarifs_initiaux()
        self.assertEqual(self.simuler().status_code, 400)  # panier vide
        self.client.force_authenticate(None)
        self.assertEqual(self.simuler().status_code, 401)

    def test_limite_dediee(self):
        from .views import SimulerFraisView

        self.assertEqual(SimulerFraisView.throttle_scope, "commande_simulation")


# =====================================================================
# POINT GPS DU LIEU DE LIVRAISON (docs/MODULE_COMMANDES.md, § 3 bis)
# =====================================================================

import json

from django.db import IntegrityError, transaction

from .serializers import AdresseLivraisonSerializer

# Un point à Cocody, déjà à 6 décimales.
POSITION = {"latitude": 5.359952, "longitude": -3.986912}


class PositionLivraisonCheckoutTests(DonneesCycleDeVie, APITestCase):
    """Point facultatif au checkout (validation et simulation) : les deux
    coordonnées ou aucune, des nombres, en Côte d'Ivoire, arrondis à 6
    décimales."""

    def setUp(self):
        self.creer_donnees()
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=1)
        self.en_tant_que(self.client_user)

    def corps(self, **position):
        return {"adresse_livraison": {**ADRESSE_LIVRAISON, **position}}

    def valider(self, **position):
        return self.client.post(URL_VALIDER, self.corps(**position), format="json")

    def simuler(self, **position):
        return self.client.post(URL_SIMULER, self.corps(**position), format="json")

    def test_sans_point_le_checkout_ne_change_pas(self):
        r = self.valider()
        self.assertEqual(r.status_code, 201, r.data)
        groupe = GroupeCommande.objects.get()
        self.assertEqual((groupe.livraison_latitude, groupe.livraison_longitude), (None, None))
        adresse = self.client.get(f"/api/commandes/{r.data[0]['id']}/").data["adresse_livraison"]
        self.assertEqual((adresse["latitude"], adresse["longitude"]), (None, None))

    def test_point_null_equivaut_a_absent(self):
        self.assertEqual(self.simuler(latitude=None, longitude=None).status_code, 200)
        self.assertEqual(self.valider(latitude=None, longitude=None).status_code, 201)
        groupe = GroupeCommande.objects.get()
        self.assertEqual((groupe.livraison_latitude, groupe.livraison_longitude), (None, None))

    def test_point_enregistre_et_sans_effet_sur_les_montants(self):
        sans_point = self.simuler().data
        self.assertEqual(self.simuler(**POSITION).data, sans_point)
        self.assertEqual(self.valider(**POSITION).status_code, 201)
        groupe = GroupeCommande.objects.get()
        self.assertEqual((groupe.livraison_latitude, groupe.livraison_longitude),
                         (Decimal("5.359952"), Decimal("-3.986912")))
        self.assertEqual(Commande.objects.get().montant_total, Decimal("1005") + FRAIS_ABIDJAN)

    def test_arrondi_a_6_decimales(self):
        cas = [
            ((5.35995171, -3.98691249), (Decimal("5.359952"), Decimal("-3.986912"))),
            ((5.1234565, -3.9869125), (Decimal("5.123457"), Decimal("-3.986913"))),  # moitié : vers le haut
            ((5, -4), (Decimal("5.000000"), Decimal("-4.000000"))),  # entiers JSON acceptés
            ((4.0, -9.0), (Decimal("4.000000"), Decimal("-9.000000"))),  # bornes incluses
            ((11.0, -2.0), (Decimal("11.000000"), Decimal("-2.000000"))),
        ]
        for (latitude, longitude), attendu in cas:
            with self.subTest(latitude=latitude, longitude=longitude):
                adresse = AdresseLivraisonSerializer(
                    data={**ADRESSE_LIVRAISON, "latitude": latitude, "longitude": longitude},
                )
                self.assertTrue(adresse.is_valid(), adresse.errors)
                self.assertEqual((adresse.validated_data["latitude"], adresse.validated_data["longitude"]), attendu)
        self.assertEqual(self.valider(latitude=5.35995171, longitude=-3.98691249).status_code, 201)
        groupe = GroupeCommande.objects.get()
        self.assertEqual((groupe.livraison_latitude, groupe.livraison_longitude),
                         (Decimal("5.359952"), Decimal("-3.986912")))

    def test_refus_400_sur_la_cle_concernee(self):
        cas = {
            "latitude seule": ({"latitude": 5.36}, "longitude"),
            "longitude seule": ({"longitude": -3.98}, "latitude"),
            "latitude nulle avec une longitude": ({"latitude": None, "longitude": -3.98}, "latitude"),
            "trop au sud (golfe de Guinée)": ({"latitude": 3.99, "longitude": -3.98}, "latitude"),
            "trop au nord (Mali)": ({"latitude": 11.01, "longitude": -5.5}, "latitude"),
            "trop à l'est (Ghana)": ({"latitude": 5.6, "longitude": -1.99}, "longitude"),
            "trop à l'ouest (Libéria)": ({"latitude": 6.3, "longitude": -9.01}, "longitude"),
            "Paris": ({"latitude": 48.8566, "longitude": 2.3522}, "latitude"),
            "chaîne numérique": ({"latitude": "5.36", "longitude": -3.98}, "latitude"),
            "chaîne": ({"latitude": 5.36, "longitude": "ouest"}, "longitude"),
            "booléen": ({"latitude": True, "longitude": -3.98}, "latitude"),
            "liste": ({"latitude": [5.36], "longitude": -3.98}, "latitude"),
            "entier démesuré": ({"latitude": 10 ** 40, "longitude": -3.98}, "latitude"),
        }
        for libelle, (position, champ) in cas.items():
            for endpoint, envoyer in (("valider", self.valider), ("simuler", self.simuler)):
                with self.subTest(libelle, endpoint=endpoint):
                    r = envoyer(**position)
                    self.assertEqual(r.status_code, 400, r.data)
                    self.assertIn(f"adresse_livraison.{champ}", r.data["errors"])
        self.assertFalse(GroupeCommande.objects.exists())
        self.assertFalse(Commande.objects.exists())
        self.assertEqual((self.stock(self.variante1), PanierItem.objects.count()), (10, 1))

    def test_nan_et_infini_refuses(self):
        # En JSON, NaN et Infinity sont refusés dès la lecture du corps (JSON strict)...
        for valeur in ("NaN", "Infinity", "-Infinity"):
            corps = json.dumps(self.corps(longitude=-3.98))[:-2] + f', "latitude": {valeur}}}}}'
            for url in (URL_VALIDER, URL_SIMULER):
                with self.subTest(valeur, url=url):
                    r = self.client.post(url, corps, content_type="application/json")
                    self.assertEqual(r.status_code, 400)
        self.assertFalse(GroupeCommande.objects.exists())
        # ... et par le champ lui-même, quelle que soit la façon dont il est lu.
        for valeur in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(valeur=valeur):
                adresse = AdresseLivraisonSerializer(data={**ADRESSE_LIVRAISON, "latitude": valeur, "longitude": -3.98})
                self.assertFalse(adresse.is_valid())
                self.assertIn("latitude", adresse.errors)

    def test_erreurs_d_adresse_sur_des_cles_a_points_a_la_validation(self):
        # Avant : « telephone » à la validation, « adresse_livraison.telephone »
        # à la simulation. Désormais la même clé des deux côtés.
        adresse = {**ADRESSE_LIVRAISON, "telephone": "abc"}
        for url in (URL_VALIDER, URL_SIMULER):
            with self.subTest(url):
                r = self.client.post(url, {"adresse_livraison": adresse}, format="json")
                self.assertEqual(r.status_code, 400)
                self.assertEqual(list(r.data["errors"]), ["adresse_livraison.telephone"])
        r = self.client.post(URL_VALIDER, {}, format="json")
        self.assertEqual(list(r.data["errors"]), ["adresse_livraison"])


class PositionLivraisonVisibiliteTests(DonneesCycleDeVie, APITestCase):
    """Même règle que l'adresse, sauf pour le vendeur : il ne voit jamais le
    point (côté livraison : apps.livraison.tests.PositionLivraisonTests)."""

    def setUp(self):
        self.creer_donnees()
        r = self.commander((self.variante1, 1), adresse_livraison={**ADRESSE_LIVRAISON, **POSITION})
        self.assertEqual(r.status_code, 201, r.data)
        self.commande = Commande.objects.get()

    def lire(self, utilisateur, url, methode="get"):
        self.en_tant_que(utilisateur)
        r = getattr(self.client, methode)(url)
        self.assertEqual(r.status_code, 200, r.content)
        return r

    def assertPoint(self, adresse):
        # Des nombres JSON, pas des chaînes décimales comme les montants.
        self.assertEqual((adresse["latitude"], adresse["longitude"]), (5.359952, -3.986912))
        self.assertIsInstance(adresse["latitude"], float)
        self.assertIsInstance(adresse["longitude"], float)

    def assertSansPoint(self, reponse):
        contenu = reponse.content.decode()
        for fragment in ("latitude", "longitude", "5.359952", "3.986912"):
            self.assertNotIn(fragment, contenu)

    def test_le_client_voit_le_point_de_sa_commande_en_nombres(self):
        detail = json.loads(self.lire(self.client_user, f"/api/commandes/{self.commande.pk}/").content)
        self.assertPoint(detail["adresse_livraison"])
        groupes = json.loads(self.lire(self.client_user, "/api/commandes/groupes/").content)
        self.assertPoint(groupes["results"][0]["adresse_livraison"])
        annulation = self.lire(self.client_user, f"/api/commandes/{self.commande.pk}/annuler/", "post")
        self.assertPoint(json.loads(annulation.content)["adresse_livraison"])

    def test_un_autre_client_ne_voit_rien(self):
        self.en_tant_que(self.autre_client)
        self.assertEqual(self.client.get(f"/api/commandes/{self.commande.pk}/").status_code, 404)
        self.assertSansPoint(self.client.get("/api/commandes/groupes/"))

    def test_le_vendeur_ne_voit_jamais_le_point(self):
        detail = self.lire(self.vendeur1, f"/api/commandes/vendeur/{self.commande.pk}/")
        self.assertEqual(detail.data["adresse_livraison"]["quartier"], ADRESSE_LIVRAISON["quartier"])
        self.assertSansPoint(detail)
        self.assertSansPoint(self.lire(self.vendeur1, "/api/commandes/vendeur/"))
        # Réponse du passage en préparation (même représentation vendeur).
        self.payer(self.commande)
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.CONFIRMEE)
        preparation = self.lire(self.vendeur1, f"/api/commandes/vendeur/{self.commande.pk}/preparation/", "post")
        self.assertSansPoint(preparation)

    def test_l_administration_voit_le_point(self):
        r = self.lire(self.admin, f"/api/commandes/administration/{self.commande.pk}/annuler/", "post")
        self.assertPoint(json.loads(r.content)["adresse_livraison"])

    def test_contrainte_de_base_les_deux_ou_aucune(self):
        for position in ({"livraison_latitude": Decimal("5.36")}, {"livraison_longitude": Decimal("-3.98")}):
            with self.subTest(position), self.assertRaises(IntegrityError), transaction.atomic():
                GroupeCommande.objects.create(client=self.client_user, **position)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GroupeCommande.objects.filter(pk=self.commande.groupe_id).update(livraison_longitude=None)
        # Les deux vides (groupes existants, checkout sans point) : accepté.
        GroupeCommande.objects.filter(pk=self.commande.groupe_id).update(
            livraison_latitude=None, livraison_longitude=None,
        )
