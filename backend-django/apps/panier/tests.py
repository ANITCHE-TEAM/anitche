import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import skipUnless

import django.db
from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from .models import Panier, PanierItem


class PanierBaseTestCase(APITestCase):
    """Jeu de données commun (sans test) : un client, une boutique publiable,
    une variante à 10 unités en stock."""

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.other_client = self._create_user("other@test.com", Role.CLIENT)

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


class PanierTestCase(PanierBaseTestCase):

    # ---------- Création du panier : uniquement sur écriture réelle ----------

    def test_consulter_panier_authentifie_ne_cree_rien_en_base(self):
        """A04:2025 : une simple consultation (GET) ne doit jamais créer de
        Panier en base — seul un vrai ajout d'article le justifie."""
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-detail")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total"], "0.00")
        self.assertFalse(Panier.objects.filter(utilisateur=self.client_user).exists())

    def test_consulter_panier_anonyme_ne_cree_rien_en_base(self):
        url = reverse("panier:panier-detail")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Panier.objects.filter(utilisateur__isnull=True).exists())

    def test_ajouter_article_cree_bien_le_panier(self):
        """Le premier ajout réel doit, lui, créer le panier."""
        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-items")
        response = self.client.post(url, {"variante": self.variante.id, "quantite": 1})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Panier.objects.filter(utilisateur=self.client_user).exists())

    def test_authenticated_user_always_gets_same_panier(self):
        """Une fois le panier réellement créé (premier ajout), toute
        consultation ultérieure doit renvoyer ce même panier persisté —
        pas une nouvelle instance éphémère à chaque fois."""
        self.client.force_authenticate(user=self.client_user)
        self.client.post(reverse("panier:panier-items"), {"variante": self.variante.id, "quantite": 1})

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
            "variante": self.variante.id,
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
        PanierItem.objects.create(panier=other_panier, variante=self.variante, quantite=3)

        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-items")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 0)

    def test_user_cannot_modify_others_item(self):
        other_panier = Panier.objects.create(utilisateur=self.other_client)
        item = PanierItem.objects.create(panier=other_panier, variante=self.variante, quantite=1)

        self.client.force_authenticate(user=self.client_user)
        url = reverse("panier:panier-item-detail", args=[item.id])
        response = self.client.patch(url, {"quantite": 5})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_modify_own_item(self):
        self.client.force_authenticate(user=self.client_user)
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        item = PanierItem.objects.create(panier=panier, variante=self.variante, quantite=1)

        url = reverse("panier:panier-item-detail", args=[item.id])
        response = self.client.patch(url, {"quantite": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 5)

    def test_user_can_delete_own_item(self):
        self.client.force_authenticate(user=self.client_user)
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        item = PanierItem.objects.create(panier=panier, variante=self.variante, quantite=1)

        url = reverse("panier:panier-item-detail", args=[item.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PanierItem.objects.filter(pk=item.id).exists())


class PanierFaillesCorrigeesTestCase(PanierBaseTestCase):
    """Une faille confirmée au diagnostic = un test qui la bloque."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.client_user)
        self.url_items = reverse("panier:panier-items")

    def _url_item(self, item):
        return reverse("panier:panier-item-detail", args=[item.id])

    def _ligne(self, quantite=2):
        panier, _ = Panier.objects.get_or_create(utilisateur=self.client_user)
        return PanierItem.objects.create(panier=panier, variante=self.variante, quantite=quantite)

    def _rendre_indisponible(self, cas):
        if cas == "boutique_suspendue":
            self.boutique.est_suspendue = True
            self.boutique.save(update_fields=["est_suspendue"])
        elif cas == "boutique_fermee":
            self.boutique.est_active = False
            self.boutique.save(update_fields=["est_active"])
        elif cas == "produit_inactif":
            self.produit.desactiver(par="vendeur")
        elif cas == "variante_inactive":
            self.variante.est_active = False
            self.variante.save(update_fields=["est_active"])

    # ---------- Ajout d'un article indisponible (faille prioritaire) ----------

    def test_ajout_article_indisponible_refuse(self):
        for cas in ("boutique_suspendue", "boutique_fermee", "produit_inactif", "variante_inactive"):
            with self.subTest(cas=cas):
                sid = transaction.savepoint()
                self._rendre_indisponible(cas)
                response = self.client.post(self.url_items, {"variante": self.variante.id, "quantite": 1})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("n'est plus disponible à la vente", response.data["errors"]["variante"][0])
                self.assertFalse(PanierItem.objects.exists())
                transaction.savepoint_rollback(sid)
                for objet in (self.boutique, self.produit, self.variante):
                    objet.refresh_from_db()

    # ---------- Article devenu indisponible après coup (option 2) ----------

    def test_ligne_devenue_indisponible_marquee_et_exclue_du_total(self):
        self._ligne(quantite=2)
        autre_variante = VarianteProduit.objects.create(
            produit=self.produit, nom="Version Blanche", prix=Decimal("1000.00"),
        )
        autre_variante.stock.quantite_disponible = 5
        autre_variante.stock.save()
        PanierItem.objects.create(
            panier=Panier.objects.get(utilisateur=self.client_user), variante=autre_variante, quantite=1,
        )
        self._rendre_indisponible("variante_inactive")

        response = self.client.get(reverse("panier:panier-detail"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        lignes = {ligne["variante"]: ligne for ligne in response.data["items"]}
        self.assertFalse(lignes[self.variante.id]["est_disponible"])
        self.assertTrue(lignes[self.variante.id]["motif_indisponibilite"])
        self.assertTrue(lignes[autre_variante.id]["est_disponible"])
        self.assertIsNone(lignes[autre_variante.id]["motif_indisponibilite"])
        self.assertEqual(Decimal(response.data["total"]), Decimal("1000.00"))
        self.assertEqual(response.data["nombre_articles"], 3)

    def test_motif_generique_pour_une_boutique_suspendue(self):
        self._ligne()
        self._rendre_indisponible("boutique_suspendue")

        response = self.client.get(self.url_items)

        motif = response.data["results"][0]["motif_indisponibilite"]
        self.assertIn("boutique", motif)
        self.assertNotIn("suspendue", motif)

    def test_ligne_indisponible_baisse_et_suppression_autorisees_hausse_refusee(self):
        item = self._ligne(quantite=3)
        self._rendre_indisponible("boutique_suspendue")

        response = self.client.patch(self._url_item(item), {"quantite": 4})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 3)

        response = self.client.patch(self._url_item(item), {"quantite": 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 1)

        response = self.client.delete(self._url_item(item))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PanierItem.objects.filter(pk=item.pk).exists())

    def test_ligne_redevient_disponible_a_la_levee_de_suspension(self):
        self._ligne(quantite=1)
        self._rendre_indisponible("boutique_suspendue")
        self.boutique.est_suspendue = False
        self.boutique.save(update_fields=["est_suspendue"])

        response = self.client.get(reverse("panier:panier-detail"))

        self.assertTrue(response.data["items"][0]["est_disponible"])
        self.assertEqual(Decimal(response.data["total"]), Decimal("20000.00"))

    # ---------- Quantités (A) ----------

    def test_quantite_zero_refusee_a_l_ajout_et_a_la_modification(self):
        response = self.client.post(self.url_items, {"variante": self.variante.id, "quantite": 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("quantite", response.data["errors"])
        self.assertFalse(PanierItem.objects.exists())

        item = self._ligne(quantite=2)
        response = self.client.patch(self._url_item(item), {"quantite": 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 2)

    def test_quantite_negative_refusee(self):
        response = self.client.post(self.url_items, {"variante": self.variante.id, "quantite": -1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PanierItem.objects.exists())

    def test_contrainte_base_quantite_minimum_1(self):
        panier = Panier.objects.create(utilisateur=self.client_user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PanierItem.objects.create(panier=panier, variante=self.variante, quantite=0)

    def test_quantite_superieure_au_stock_refusee(self):
        response = self.client.post(self.url_items, {"variante": self.variante.id, "quantite": 11})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        item = self._ligne(quantite=2)
        response = self.client.patch(self._url_item(item), {"quantite": 11})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 2)

    def test_baisse_toujours_autorisee_hausse_controlee_contre_le_stock(self):
        item = self._ligne(quantite=5)
        self.variante.stock.quantite_disponible = 2
        self.variante.stock.save()

        response = self.client.patch(self._url_item(item), {"quantite": 4})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 4)

        response = self.client.patch(self._url_item(item), {"quantite": 6})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 4)

    # ---------- Variante non modifiable après création (B) ----------

    def test_changer_la_variante_d_une_ligne_refuse(self):
        sans_stock = VarianteProduit.objects.create(
            produit=self.produit, nom="Sans stock", prix=Decimal("1.00"),
        )
        item = self._ligne(quantite=5)

        for methode in ("patch", "put"):
            with self.subTest(methode=methode):
                response = getattr(self.client, methode)(
                    self._url_item(item), {"variante": sans_stock.id, "quantite": 5}, format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("ne peut pas être modifiée", response.data["errors"]["variante"][0])
                item.refresh_from_db()
                self.assertEqual(item.variante_id, self.variante.id)

    def test_put_avec_la_meme_variante_ou_sans_variante_accepte(self):
        item = self._ligne(quantite=1)

        response = self.client.put(
            self._url_item(item), {"variante": self.variante.id, "quantite": 2}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.put(self._url_item(item), {"quantite": 3}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.quantite, 3)

    # ---------- session_key non exposée (C) ----------

    def test_session_key_absente_de_la_reponse_anonyme(self):
        self.client.force_authenticate(user=None)
        self.client.post(self.url_items, {"variante": self.variante.id, "quantite": 1})

        response = self.client.get(reverse("panier:panier-detail"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["items"]), 1)
        self.assertNotIn("session_key", response.data)
        self.assertNotIn(self.client.cookies["sessionid"].value, response.content.decode())

    # ---------- Unicité en base (D) ----------

    def test_contraintes_un_panier_par_compte_une_ligne_par_variante(self):
        panier = Panier.objects.create(utilisateur=self.client_user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Panier.objects.create(utilisateur=self.client_user)

        PanierItem.objects.create(panier=panier, variante=self.variante, quantite=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PanierItem.objects.create(panier=panier, variante=self.variante, quantite=1)

    # ---------- Nombre de requêtes constant (E) ----------

    def _remplir(self, nombre, depart=0):
        for index in range(depart, depart + nombre):
            variante = VarianteProduit.objects.create(
                produit=self.produit, nom=f"Variante {index}", prix=Decimal("10.00"),
            )
            variante.stock.quantite_disponible = 5
            variante.stock.save()
            self.client.post(self.url_items, {"variante": variante.id, "quantite": 1})

    def test_consultation_panier_nombre_de_requetes_constant(self):
        url = reverse("panier:panier-detail")
        self._remplir(2)
        with CaptureQueriesContext(connection) as avec_2:
            self.client.get(url)
        self._remplir(8, depart=2)
        with CaptureQueriesContext(connection) as avec_10:
            response = self.client.get(url)

        self.assertEqual(len(response.data["items"]), 10)
        self.assertEqual(len(avec_2), len(avec_10))

    def test_liste_des_lignes_nombre_de_requetes_constant(self):
        self._remplir(2)
        with CaptureQueriesContext(connection) as avec_2:
            self.client.get(self.url_items)
        self._remplir(8, depart=2)
        with CaptureQueriesContext(connection) as avec_10:
            self.client.get(self.url_items)

        self.assertEqual(len(avec_2), len(avec_10))

    # ---------- Panier non enregistré : id null (F) ----------

    def test_panier_vide_id_null_et_stable(self):
        url = reverse("panier:panier-detail")
        premiere = self.client.get(url)
        seconde = self.client.get(url)

        self.assertIsNone(premiere.data["id"])
        self.assertIsNone(seconde.data["id"])
        self.assertEqual(premiere.data["items"], [])
        self.assertEqual(premiere.data["nombre_articles"], 0)

    # ---------- Détails de stock non exposés ----------

    def test_stock_detaille_non_expose_dans_le_panier(self):
        self.client.post(self.url_items, {"variante": self.variante.id, "quantite": 1})

        for response in (self.client.get(self.url_items), self.client.get(reverse("panier:panier-detail"))):
            with self.subTest(url=response.request["PATH_INFO"]):
                lignes = response.data["results"] if "results" in response.data else response.data["items"]
                self.assertEqual(lignes[0]["variante_detail"]["stock"], {"est_en_stock": True})
                self.assertNotIn("quantite_disponible", response.content.decode())
                self.assertNotIn("seuil_alerte", response.content.decode())


@skipUnless(
    connection.vendor == "postgresql",
    "select_for_update() est un no-op sous SQLite : ce test ne mesurerait pas "
    "le verrouillage réel visé (même limite que les autres tests de concurrence).",
)
class PanierConcurrenceTestCase(TransactionTestCase):
    """D : des ajouts simultanés ne doivent créer ni second panier, ni
    ligne en double, ni erreur 500."""

    NOMBRE_REQUETES = 8

    def setUp(self):
        self.client_user = Utilisateur.objects.create_user(
            email="client@test.com", password="testpass123", nom="Test", prenom="User",
        )
        vendeur = Utilisateur.objects.create_user(
            email="vendeur@test.com", password="testpass123", nom="Test", prenom="User",
            role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE,
        )
        boutique = Boutique.objects.create(proprietaire=vendeur, nom="Boutique Concurrence")
        produit = Produit.objects.create(boutique=boutique, nom="Produit", prix_base=Decimal("10.00"))
        self.variante = VarianteProduit.objects.create(produit=produit, nom="Unique", prix=Decimal("10.00"))
        self.variante.stock.quantite_disponible = 100
        self.variante.stock.save()

    def _rafale(self):
        barriere = threading.Barrier(self.NOMBRE_REQUETES)

        def appel(_):
            django.db.close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.client_user)
                barriere.wait()
                return client.post(
                    reverse("panier:panier-items"), {"variante": self.variante.id, "quantite": 1}
                ).status_code
            finally:
                django.db.connection.close()

        with ThreadPoolExecutor(max_workers=self.NOMBRE_REQUETES) as executeur:
            return list(executeur.map(appel, range(self.NOMBRE_REQUETES)))

    def test_premiers_ajouts_simultanes_un_seul_panier_une_seule_ligne(self):
        statuts = self._rafale()

        self.assertEqual(statuts, [status.HTTP_201_CREATED] * self.NOMBRE_REQUETES)
        self.assertEqual(Panier.objects.filter(utilisateur=self.client_user).count(), 1)
        self.assertEqual(PanierItem.objects.count(), 1)
        self.assertEqual(PanierItem.objects.get().quantite, self.NOMBRE_REQUETES)