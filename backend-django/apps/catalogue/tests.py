from decimal import Decimal
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from .models import Categorie, Produit, VarianteProduit, Stock, ImageProduit


class BaseCatalogueTestCase(APITestCase):
    def setUp(self):
        # Vendeur 1 (validé avec boutique)
        self.vendeur1 = Utilisateur.objects.create_user(
            email="vendeur1@anitche.ci",
            password="MotDePasse123!",
            nom="Kouame",
            prenom="Yao",
            role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE,
        )
        self.boutique1 = Boutique.objects.create(
            proprietaire=self.vendeur1,
            nom="Boutique Yao",
            description="Produits artisanaux",
            est_active=True,
        )

        # Vendeur 2 (validé avec boutique distincte)
        self.vendeur2 = Utilisateur.objects.create_user(
            email="vendeur2@anitche.ci",
            password="MotDePasse123!",
            nom="Diallo",
            prenom="Moussa",
            role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE,
        )
        self.boutique2 = Boutique.objects.create(
            proprietaire=self.vendeur2,
            nom="Boutique Diallo",
            description="Électronique",
            est_active=True,
        )

        # Client standard
        self.client_user = Utilisateur.objects.create_user(
            email="client@anitche.ci",
            password="MotDePasse123!",
            nom="Awa",
            prenom="Traore",
            role=Role.CLIENT,
            statut_kyc=StatutKYC.NON_SOUMIS,
        )

        # Vendeur en attente (non validé)
        self.vendeur_en_attente = Utilisateur.objects.create_user(
            email="attente@anitche.ci",
            password="MotDePasse123!",
            nom="Kone",
            prenom="Ibrahim",
            role=Role.CLIENT,
            statut_kyc=StatutKYC.EN_ATTENTE,
        )

        # Catégorie principale et sous-catégorie
        self.cat_mode = Categorie.objects.create(nom="Mode & Vêtements", ordre=1)
        self.cat_chaussures = Categorie.objects.create(
            nom="Chaussures",
            parent=self.cat_mode,
            ordre=2
        )


# =====================================================================
# 1. TESTS MODÈLES & RÈGLES MÉTIER
# =====================================================================

class CategorieModeleTests(BaseCatalogueTestCase):
    def test_creation_et_slug_automatique(self):
        cat = Categorie.objects.create(nom="Artisanat Africain")
        self.assertEqual(cat.slug, "artisanat-africain")
        self.assertIn("Artisanat Africain", str(cat))

    def test_chaine_hierarchique_str(self):
        self.assertEqual(str(self.cat_chaussures), "Mode & Vêtements > Chaussures")

    def test_auto_parentage_impossible(self):
        cat = Categorie.objects.create(nom="Test Loop")
        cat.parent = cat
        with self.assertRaises(ValidationError):
            cat.save()


class ProduitModeleTests(BaseCatalogueTestCase):
    def test_creation_produit_avec_slug_unique(self):
        p1 = Produit.objects.create(
            boutique=self.boutique1,
            categorie=self.cat_chaussures,
            nom="Chaussure Cuir Wax",
            prix_base=Decimal("15000.00"),
        )
        p2 = Produit.objects.create(
            boutique=self.boutique1,
            categorie=self.cat_chaussures,
            nom="Chaussure Cuir Wax",
            prix_base=Decimal("15000.00"),
        )
        self.assertTrue(p1.slug.startswith("chaussure-cuir-wax-"))
        self.assertNotEqual(p1.slug, p2.slug)

    def test_est_achetable_respecte_boutique_est_publiable(self):
        p = Produit.objects.create(
            boutique=self.boutique1,
            nom="Robe Bazin",
            prix_base=Decimal("25000.00"),
        )
        self.assertTrue(p.est_achetable)

        # Si le vendeur désactive sa boutique
        self.boutique1.est_active = False
        self.boutique1.save()
        self.assertFalse(p.est_achetable)

        # Si le produit lui-même est désactivé
        self.boutique1.est_active = True
        self.boutique1.save()
        p.est_actif = False
        p.save()
        self.assertFalse(p.est_achetable)

    def test_queryset_publies_filtre_strictement(self):
        p = Produit.objects.create(
            boutique=self.boutique1,
            nom="Sandales Cuir",
            prix_base=Decimal("5000.00"),
        )
        self.assertIn(p, Produit.objects.publies())

        # Vendeur suspendu / non validé
        self.vendeur1.statut_kyc = StatutKYC.REFUSE
        self.vendeur1.save()
        self.assertNotIn(p, Produit.objects.publies())


class VarianteEtStockModeleTests(BaseCatalogueTestCase):
    def test_creation_variante_initialise_le_stock(self):
        produit = Produit.objects.create(
            boutique=self.boutique1,
            nom="T-shirt Anitche",
            prix_base=Decimal("5000.00"),
        )
        variante = VarianteProduit.objects.create(
            produit=produit,
            nom="Taille L / Blanc",
            prix=Decimal("5500.00"),
            prix_promo=Decimal("4500.00"),
        )
        self.assertTrue(hasattr(variante, 'stock'))
        self.assertEqual(variante.stock.quantite_disponible, 0)
        self.assertEqual(variante.prix_effectif, Decimal("4500.00"))

    def test_mouvements_de_stock(self):
        produit = Produit.objects.create(boutique=self.boutique1, nom="Sac")
        variante = VarianteProduit.objects.create(
            produit=produit, nom="Standard", prix=Decimal("10000.00")
        )
        stock = variante.stock
        stock.incrementer(10)
        self.assertEqual(stock.quantite_disponible, 10)
        self.assertTrue(stock.est_en_stock(5))

        stock.decrementer(4)
        self.assertEqual(stock.quantite_disponible, 6)

        with self.assertRaises(ValidationError):
            stock.decrementer(10)


# =====================================================================
# 2. TESTS API PUBLIQUE
# =====================================================================

class CataloguePublicAPITests(BaseCatalogueTestCase):
    def setUp(self):
        super().setUp()
        self.produit = Produit.objects.create(
            boutique=self.boutique1,
            categorie=self.cat_chaussures,
            nom="Mocassin Prestige",
            description="Mocassin en cuir véritable de fabrication artisanale",
            prix_base=Decimal("30000.00"),
        )
        self.variante = VarianteProduit.objects.create(
            produit=self.produit,
            nom="Pointure 42",
            prix=Decimal("30000.00"),
        )
        self.variante.stock.quantite_disponible = 5
        self.variante.stock.save()

    def test_liste_categories_public(self):
        url = reverse('catalogue:categories-liste')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Vérifie que la catégorie racine contient bien sa sous-catégorie
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['nom'], "Mode & Vêtements")
        self.assertEqual(len(response.data[0]['sous_categories']), 1)

    def test_liste_produits_public_avec_filtre(self):
        url = reverse('catalogue:produits-liste')
        response = self.client.get(url, {'recherche': 'Prestige'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results'] if 'results' in response.data else response.data), 1)

    def test_fiche_detail_produit_public(self):
        url = reverse('catalogue:produit-detail', args=[self.produit.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['nom'], "Mocassin Prestige")
        self.assertEqual(len(response.data['variantes']), 1)

    def test_produit_boutique_fermee_renvoie_404(self):
        self.boutique1.est_active = False
        self.boutique1.save()

        url = reverse('catalogue:produit-detail', args=[self.produit.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# =====================================================================
# 3. TESTS API ESPACE VENDEUR & ISOLATION
# =====================================================================

class CatalogueVendeurAPITests(BaseCatalogueTestCase):
    def test_vendeur_valide_peut_creer_produit(self):
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse('catalogue:vendeur-produits-liste')
        data = {
            'nom': 'Chemise Bogolan',
            'description': 'Chemise traditionnelle 100% coton',
            'prix_base': '12000.00',
            'categorie': self.cat_mode.id,
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Produit.objects.filter(nom='Chemise Bogolan', boutique=self.boutique1).exists())

    def test_client_ne_peut_pas_creer_produit(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse('catalogue:vendeur-produits-liste')
        response = self.client.post(url, {'nom': 'Hack'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_vendeur_en_attente_ne_peut_pas_creer_produit(self):
        self.client.force_authenticate(user=self.vendeur_en_attente)
        url = reverse('catalogue:vendeur-produits-liste')
        response = self.client.post(url, {'nom': 'Non autorisé'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_isolation_vendeur_ne_peut_pas_modifier_produit_autre_vendeur(self):
        # Produit appartenant au Vendeur 1
        produit1 = Produit.objects.create(
            boutique=self.boutique1,
            nom="Collier Or",
            prix_base=Decimal("50000.00"),
        )

        # Tentative de modification par le Vendeur 2
        self.client.force_authenticate(user=self.vendeur2)
        url = reverse('catalogue:vendeur-produit-detail', args=[produit1.id])
        response = self.client.patch(url, {'nom': 'Collier Piraté'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_vendeur_peut_ajouter_variante_avec_stock(self):
        produit = Produit.objects.create(
            boutique=self.vendeur1.boutique,
            nom="Casquette",
            prix_base=Decimal("3000.00")
        )
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse('catalogue:vendeur-variantes-liste', args=[produit.id])
        data = {
            'nom': 'Noire',
            'prix': '3500.00',
            'quantite_initiale': 20,
            'seuil_alerte': 3,
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        variante = VarianteProduit.objects.get(pk=response.data['id'])
        self.assertEqual(variante.stock.quantite_disponible, 20)
        self.assertEqual(variante.stock.seuil_alerte, 3)

    def test_vendeur_peut_mettre_a_jour_le_stock(self):
        produit = Produit.objects.create(boutique=self.boutique1, nom="Montre")
        variante = VarianteProduit.objects.create(produit=produit, nom="Argent", prix=Decimal("45000.00"))

        self.client.force_authenticate(user=self.vendeur1)
        url = reverse('catalogue:vendeur-stock-update', args=[variante.id])
        response = self.client.patch(url, {'quantite_disponible': 15})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        variante.stock.refresh_from_db()
        self.assertEqual(variante.stock.quantite_disponible, 15)
