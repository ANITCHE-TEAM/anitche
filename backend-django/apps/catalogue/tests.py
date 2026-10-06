import io
import os
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle
from rest_framework_simplejwt.tokens import AccessToken

from apps.commandes.models import Commande, CommandeItem
from apps.panier.models import Panier, PanierItem
from apps.passeport_qr.models import HistoriqueScanPasseport, PasseportProduit
from apps.support.models import SupportTicket
from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from .models import MAX_IMAGES_PAR_PRODUIT, Categorie, Produit, VarianteProduit, Stock, ImageProduit
from .serializers import (
    MESSAGE_DESACTIVATION_ADMINISTRATION, MESSAGE_MONTANT_ENTIER, ProduitPublicListSerializer, ProduitVendeurSerializer,
)
from .views import ProduitPublicListView, StockUpdateView


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
        p.desactiver(par="vendeur")
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

    def test_produits_boutique_suspendue_masques(self):
        self.boutique1.est_suspendue = True
        self.boutique1.save()

        liste = self.client.get(reverse('catalogue:produits-liste'), {'recherche': 'Prestige'})
        resultats = liste.data['results'] if 'results' in liste.data else liste.data
        self.assertEqual(len(resultats), 0)

        detail = self.client.get(reverse('catalogue:produit-detail', args=[self.produit.slug]))
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)

        self.produit.refresh_from_db()
        self.assertFalse(self.produit.est_achetable)

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

    def test_reponse_creation_variante_reflete_le_stock_initial(self):
        """Non-régression : la réponse JSON de création doit refléter le
        stock réellement enregistré, pas une version en cache de
        `variante.stock` antérieure à sa mise à jour (le stock persisté
        était correct, mais la réponse mentait au vendeur en affichant 0)."""
        produit = Produit.objects.create(
            boutique=self.vendeur1.boutique,
            nom="Sac à main",
            prix_base=Decimal("15000.00"),
        )
        self.client.force_authenticate(user=self.vendeur1)
        url = reverse('catalogue:vendeur-variantes-liste', args=[produit.id])
        response = self.client.post(url, {
            'nom': 'Standard',
            'prix': '15000.00',
            'quantite_initiale': 20,
            'seuil_alerte': 3,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['stock']['quantite_disponible'], 20)
        self.assertEqual(response.data['stock']['seuil_alerte'], 3)

    def test_vendeur_peut_mettre_a_jour_le_stock(self):
        produit = Produit.objects.create(boutique=self.boutique1, nom="Montre")
        variante = VarianteProduit.objects.create(produit=produit, nom="Argent", prix=Decimal("45000.00"))

        self.client.force_authenticate(user=self.vendeur1)
        url = reverse('catalogue:vendeur-stock-update', args=[variante.id])
        response = self.client.patch(url, {'quantite_disponible': 15})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        variante.stock.refresh_from_db()
        self.assertEqual(variante.stock.quantite_disponible, 15)


class StockConcurrenceTestCase(TransactionTestCase):
    """Stock.incrementer() doit être atomique, comme decrementer().
    Nécessite TransactionTestCase (pas APITestCase/TestCase) : les threads
    doivent voir une vraie transaction commitée en base, pas la transaction
    unique enveloppant un TestCase classique."""

    def setUp(self):
        vendeur = Utilisateur.objects.create_user(
            email="vendeur-stock@anitche.ci",
            password="MotDePasse123!",
            nom="Kouame",
            prenom="Yao",
            role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE,
        )
        boutique = Boutique.objects.create(
            proprietaire=vendeur,
            nom="Boutique Stock Test",
            est_active=True,
        )
        produit = Produit.objects.create(boutique=boutique, nom="Sac")
        self.variante = VarianteProduit.objects.create(
            produit=produit, nom="Standard", prix=Decimal("10000.00"),
        )
        self.variante.stock.quantite_disponible = 10
        self.variante.stock.save(update_fields=["quantite_disponible"])

    def test_incrementer_stock_est_atomique_sous_concurrence(self):
        from concurrent.futures import ThreadPoolExecutor
        import django.db

        def appel():
            django.db.close_old_connections()
            try:
                Stock.objects.get(variante=self.variante).incrementer(1)
            finally:
                # Chaque thread ouvre sa propre connexion SQLite ; sans
                # fermeture explicite, elle reste ouverte après la fin du
                # thread et empêche Django de supprimer le fichier de base
                # de test (PermissionError [WinError 32] sous Windows, qui
                # bloque ensuite tous les runs suivants par une invite
                # interactive).
                django.db.connection.close()

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda _: appel(), range(5)))

        self.variante.stock.refresh_from_db()
        self.assertEqual(self.variante.stock.quantite_disponible, 15)  # 10 + 5, aucun incrément perdu


# =====================================================================
# 4. ESPACES VENDEUR, PUBLIC ET ADMINISTRATION (docs/MODULE_CATALOGUE.md)
# =====================================================================

URL_V = '/api/catalogue/vendeur/'
URL_P = '/api/catalogue/'
URL_ADMIN = '/api/catalogue/administration/produits/'
DOSSIER_MEDIA_TESTS = tempfile.mkdtemp(prefix='anitche-tests-catalogue-')


def image_png(nom='photo.png', taille=(4, 4), octets_aleatoires=False):
    tampon = io.BytesIO()
    if octets_aleatoires:
        image = Image.frombytes('RGB', taille, os.urandom(taille[0] * taille[1] * 3))
    else:
        image = Image.new('RGB', taille, 'red')
    image.save(tampon, 'PNG')
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT=DOSSIER_MEDIA_TESTS)
class CatalogueEspacesBase(BaseCatalogueTestCase):
    """produit1 (boutique1) avec une variante en stock ; produit2 (boutique2) idem."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(DOSSIER_MEDIA_TESTS, ignore_errors=True)

    def setUp(self):
        super().setUp()
        cache.clear()
        self.produit1 = Produit.objects.create(
            boutique=self.boutique1, nom="Pagne Baoulé", prix_base=Decimal("15000"), categorie=self.cat_chaussures,
        )
        self.variante1 = VarianteProduit.objects.create(produit=self.produit1, nom="Bleu", prix=Decimal("15000"))
        Stock.objects.filter(variante=self.variante1).update(quantite_disponible=10, seuil_alerte=3)
        self.produit2 = Produit.objects.create(boutique=self.boutique2, nom="Radio", prix_base=Decimal("20000"))
        self.variante2 = VarianteProduit.objects.create(produit=self.produit2, nom="Noire", prix=Decimal("20000"))
        self.administrateur = Utilisateur.objects.create_user(
            email="admin@anitche.ci", password="MotDePasse123!", nom="Admin", prenom="Anitche", role=Role.ADMIN,
        )

    def en_tant_que(self, utilisateur):
        self.client.force_authenticate(utilisateur)

    def ids_publics(self, **params):
        return [p['id'] for p in self.client.get(URL_P + 'produits/', params).data['results']]


class CatalogueSuspensionTests(CatalogueEspacesBase):
    """Boutique suspendue = lecture seule, comme ma-boutique/ et les passeports."""

    def test_lecture_autorisee_ecriture_refusee(self):
        image = ImageProduit.objects.create(produit=self.produit1, image=image_png())
        Boutique.objects.filter(pk=self.boutique1.pk).update(est_suspendue=True)
        self.en_tant_que(self.vendeur1)

        self.assertEqual(self.client.get(URL_V + 'produits/').status_code, 200)
        self.assertEqual(self.client.get(f'{URL_V}produits/{self.produit1.pk}/').status_code, 200)
        self.assertEqual(self.client.get(f'{URL_V}variantes/{self.variante1.pk}/').status_code, 200)

        ecritures = [
            ('post', URL_V + 'produits/', {'nom': 'Nouveau', 'prix_base': '100'}, None),
            ('patch', f'{URL_V}produits/{self.produit1.pk}/', {'nom': 'Renommé'}, None),
            ('delete', f'{URL_V}produits/{self.produit1.pk}/', None, None),
            ('post', f'{URL_V}produits/{self.produit1.pk}/variantes/', {'nom': 'Rouge', 'prix': '100'}, None),
            ('patch', f'{URL_V}variantes/{self.variante1.pk}/', {'prix': '900'}, None),
            ('delete', f'{URL_V}variantes/{self.variante1.pk}/', None, None),
            ('patch', f'{URL_V}variantes/{self.variante1.pk}/stock/', {'quantite_disponible': 99}, None),
            ('post', f'{URL_V}produits/{self.produit1.pk}/images/', {'image': image_png()}, 'multipart'),
            ('delete', f'{URL_V}images/{image.pk}/', None, None),
        ]
        for methode, url, corps, format_ in ecritures:
            with self.subTest(methode=methode, url=url):
                response = getattr(self.client, methode)(url, corps, format=format_)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertIn('suspendue', str(response.data['detail']))

        self.produit1.refresh_from_db()
        self.variante1.refresh_from_db()
        self.assertEqual((self.produit1.nom, self.produit1.est_actif), ("Pagne Baoulé", True))
        self.assertEqual((self.variante1.prix, self.variante1.est_active), (Decimal("15000"), True))
        self.assertEqual(self.variante1.stock.quantite_disponible, 10)
        self.assertEqual(Produit.objects.filter(boutique=self.boutique1).count(), 1)
        self.assertTrue(ImageProduit.objects.filter(pk=image.pk).exists())


class CatalogueIsolationTests(CatalogueEspacesBase):
    def test_variante_ne_change_pas_de_produit(self):
        # Une variante ne peut pas être déplacée sur le produit (et la fiche) de vendeur2.
        self.en_tant_que(self.vendeur1)
        response = self.client.patch(
            f'{URL_V}variantes/{self.variante1.pk}/', {'produit': self.produit2.pk}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('produit', response.data['errors'])
        self.variante1.refresh_from_db()
        self.assertEqual(self.variante1.produit_id, self.produit1.pk)

    def test_put_avec_le_meme_produit_accepte(self):
        self.en_tant_que(self.vendeur1)
        response = self.client.put(
            f'{URL_V}variantes/{self.variante1.pk}/',
            {'produit': self.produit1.pk, 'nom': 'Bleu nuit', 'prix': '15000'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_objets_dune_autre_boutique_404(self):
        image = ImageProduit.objects.create(produit=self.produit1, image=image_png())
        self.en_tant_que(self.vendeur2)
        requetes = [
            ('get', f'{URL_V}produits/{self.produit1.pk}/', None, None),
            ('patch', f'{URL_V}produits/{self.produit1.pk}/', {}, None),
            ('delete', f'{URL_V}produits/{self.produit1.pk}/', None, None),
            ('get', f'{URL_V}produits/{self.produit1.pk}/variantes/', None, None),
            ('post', f'{URL_V}produits/{self.produit1.pk}/variantes/', {'nom': 'x', 'prix': '1'}, None),
            ('get', f'{URL_V}variantes/{self.variante1.pk}/', None, None),
            ('patch', f'{URL_V}variantes/{self.variante1.pk}/', {}, None),
            ('delete', f'{URL_V}variantes/{self.variante1.pk}/', None, None),
            ('patch', f'{URL_V}variantes/{self.variante1.pk}/stock/', {'quantite_disponible': 1}, None),
            ('get', f'{URL_V}produits/{self.produit1.pk}/images/', None, None),
            ('post', f'{URL_V}produits/{self.produit1.pk}/images/', {'image': image_png()}, 'multipart'),
            ('delete', f'{URL_V}images/{image.pk}/', None, None),
        ]
        for methode, url, corps, format_ in requetes:
            with self.subTest(methode=methode, url=url):
                self.assertEqual(getattr(self.client, methode)(url, corps, format=format_).status_code, 404)

    def test_is_staff_et_administration_sans_acces_a_lespace_vendeur(self):
        staff = Utilisateur.objects.create_user(
            email="staff@anitche.ci", password="MotDePasse123!", nom="S", prenom="T", role=Role.CLIENT, is_staff=True,
        )
        for utilisateur in (staff, self.administrateur):
            with self.subTest(role=utilisateur.role):
                self.en_tant_que(utilisateur)
                self.assertEqual(self.client.get(f'{URL_V}produits/{self.produit1.pk}/').status_code, 403)


class CatalogueVisibilitePubliqueTests(CatalogueEspacesBase):
    def test_stock_public_reduit_a_la_disponibilite(self):
        # Ni quantité ni seuil d'alerte côté public.
        response = self.client.get(f'{URL_P}produits/{self.produit1.slug}/')
        self.assertEqual(response.data['variantes'][0]['stock'], {'est_en_stock': True})
        self.assertNotIn('quantite_disponible', response.content.decode())
        self.assertNotIn('seuil_alerte', response.content.decode())

        self.en_tant_que(self.vendeur1)
        stock_vendeur = self.client.get(f'{URL_V}variantes/{self.variante1.pk}/').data['stock']
        self.assertEqual((stock_vendeur['quantite_disponible'], stock_vendeur['seuil_alerte']), (10, 3))

    def test_produit_sans_variante_active_masque(self):
        # Sans variante active (ou sans variante du tout), rien n'est achetable.
        VarianteProduit.objects.filter(pk=self.variante1.pk).update(est_active=False)
        sans_variante = Produit.objects.create(boutique=self.boutique1, nom="Vide")
        for produit in (self.produit1, sans_variante):
            with self.subTest(produit=produit.nom):
                self.assertNotIn(produit.pk, self.ids_publics())
                self.assertEqual(self.client.get(f'{URL_P}produits/{produit.slug}/').status_code, 404)
        self.assertIn(self.produit2.pk, self.ids_publics())

    def test_variante_inactive_absente_de_la_fiche(self):
        VarianteProduit.objects.create(produit=self.produit1, nom="Retirée", prix=Decimal("1000"), est_active=False)
        noms = [v['nom'] for v in self.client.get(f'{URL_P}produits/{self.produit1.slug}/').data['variantes']]
        self.assertEqual(noms, ['Bleu'])

    def test_sous_categories_inactives_masquees(self):
        Categorie.objects.create(nom="Sandales cachées", parent=self.cat_mode, est_active=False)
        liste = self.client.get(URL_P + 'categories/').data
        detail = self.client.get(f'{URL_P}categories/{self.cat_mode.slug}/').data
        for sous_categories in (liste[0]['sous_categories'], detail['sous_categories']):
            self.assertEqual([c['nom'] for c in sous_categories], ['Chaussures'])

    def test_filtre_et_tri_sur_le_prix_effectif_des_variantes_actives(self):
        # Une variante inactive très chère ne fait pas sortir produit1 ;
        # une promo compte comme prix affiché.
        VarianteProduit.objects.create(produit=self.produit1, nom="Luxe", prix=Decimal("900000"), est_active=False)
        self.assertNotIn(self.produit1.pk, self.ids_publics(prix_min='500000'))

        VarianteProduit.objects.filter(pk=self.variante2.pk).update(prix_promo=Decimal("5000"))
        self.assertEqual(self.ids_publics(prix_max='6000'), [self.produit2.pk])
        self.assertEqual(self.ids_publics(tri='prix_asc'), [self.produit2.pk, self.produit1.pk])
        premier = self.client.get(URL_P + 'produits/', {'tri': 'prix_asc'}).data['results'][0]
        self.assertEqual((premier['prix_min'], premier['en_stock']), (Decimal("5000.00"), False))

    def test_categorie_au_slug_numerique(self):
        categorie = Categorie.objects.create(nom="2024")
        Produit.objects.filter(pk=self.produit2.pk).update(categorie=categorie)
        self.assertEqual(self.ids_publics(categorie='2024'), [self.produit2.pk])
        self.assertEqual(self.ids_publics(categorie=str(self.cat_chaussures.pk)), [self.produit1.pk])


class CatalogueDesactivationTests(CatalogueEspacesBase):
    """DELETE = désactivation ; l'historique n'est jamais cassé."""

    def test_delete_produit_desactive(self):
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.client.delete(f'{URL_V}produits/{self.produit1.pk}/').status_code, 204)
        self.produit1.refresh_from_db()
        self.assertEqual((self.produit1.est_actif, self.produit1.desactive_par), (False, 'vendeur'))
        self.assertNotIn(self.produit1.pk, self.ids_publics())

    def test_delete_variante_desactive_et_panier_garde_la_ligne(self):
        panier = Panier.objects.create(utilisateur=self.client_user)
        ligne = PanierItem.objects.create(panier=panier, variante=self.variante1, quantite=1)
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.client.delete(f'{URL_V}variantes/{self.variante1.pk}/').status_code, 204)
        self.variante1.refresh_from_db()
        self.assertFalse(self.variante1.est_active)
        ligne.refresh_from_db()
        self.assertFalse(ligne.est_disponible)

    def test_produit_et_variante_commandes_desactivables(self):
        # Les lignes de commande protègent produit et variante (PROTECT) :
        # la désactivation ne doit pas lever ProtectedError.
        commande = Commande.objects.create(boutique=self.boutique1, client=self.client_user, montant_total=Decimal("15000"))
        CommandeItem.objects.create(
            commande=commande, variante=self.variante1, nom_produit="Pagne", prix_unitaire=Decimal("15000"), quantite=1,
        )
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.client.delete(f'{URL_V}variantes/{self.variante1.pk}/').status_code, 204)
        self.assertEqual(self.client.delete(f'{URL_V}produits/{self.produit1.pk}/').status_code, 204)
        self.assertEqual(CommandeItem.objects.filter(variante=self.variante1).count(), 1)

    def test_passeport_et_historique_conserves(self):
        passeport = PasseportProduit.objects.create(
            produit=self.produit1, variante=self.variante1, boutique=self.boutique1, numero_lot="L1",
        )
        HistoriqueScanPasseport.objects.create(passeport=passeport)
        self.en_tant_que(self.vendeur1)
        self.client.delete(f'{URL_V}variantes/{self.variante1.pk}/')
        self.client.delete(f'{URL_V}produits/{self.produit1.pk}/')
        passeport.refresh_from_db()
        self.assertEqual(passeport.variante_id, self.variante1.pk)
        self.assertEqual(HistoriqueScanPasseport.objects.filter(passeport=passeport).count(), 1)

    def test_suppression_reelle_protegee_en_base(self):
        # Filet de sécurité du Django admin : passeports et tickets protègent le produit.
        PasseportProduit.objects.create(produit=self.produit1, variante=self.variante1, boutique=self.boutique1)
        for objet in (self.variante1, self.produit1):
            with self.subTest(objet=objet):
                with self.assertRaises(ProtectedError):
                    objet.delete()
        SupportTicket.objects.create(
            created_by=self.client_user, product=self.produit2, subject="Défaut", description="Écran",
            category=SupportTicket.Category.PRODUCT,
        )
        with self.assertRaises(ProtectedError):
            self.produit2.delete()


class CatalogueModerationTests(CatalogueEspacesBase):
    """L'administration désactive/réactive un produit, et rien d'autre ;
    le vendeur ne lève pas une désactivation de l'administration."""

    def moderer(self, produit, **corps):
        self.en_tant_que(self.administrateur)
        return self.client.patch(f'{URL_ADMIN}{produit.pk}/', corps, format='json')

    def reactiver_en_vendeur(self, **autres_champs):
        self.en_tant_que(self.vendeur1)
        return self.client.patch(
            f'{URL_V}produits/{self.produit1.pk}/', {'est_actif': True, **autres_champs}, format='json',
        )

    def etat(self):
        self.produit1.refresh_from_db()
        return self.produit1.est_actif, self.produit1.desactive_par

    def test_administration_desactive_et_le_produit_disparait(self):
        response = self.moderer(self.produit1, est_actif=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['desactive_par'], 'administration')
        self.assertEqual(self.etat(), (False, 'administration'))
        self.assertNotIn(self.produit1.pk, self.ids_publics())

    def test_super_admin_aussi(self):
        super_admin = Utilisateur.objects.create_user(
            email="sa@anitche.ci", password="MotDePasse123!", nom="S", prenom="A", role=Role.SUPER_ADMIN,
        )
        self.en_tant_que(super_admin)
        response = self.client.patch(f'{URL_ADMIN}{self.produit1.pk}/', {'est_actif': False}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_seul_est_actif_est_modifiable(self):
        response = self.moderer(self.produit1, est_actif=False, nom="Renommé par l'admin")
        self.assertEqual(response.status_code, 400)
        self.assertIn('nom', str(response.data['detail']))
        self.assertEqual(self.etat(), (True, ''))

    def test_vendeur_ne_leve_pas_une_desactivation_de_ladministration(self):
        self.moderer(self.produit1, est_actif=False)
        response = self.reactiver_en_vendeur()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(str(response.data['detail']), MESSAGE_DESACTIVATION_ADMINISTRATION)
        self.assertEqual(self.etat(), (False, 'administration'))

    def test_reactivation_refusee_napplique_aucun_autre_champ(self):
        self.moderer(self.produit1, est_actif=False)
        self.assertEqual(self.reactiver_en_vendeur(nom="Autre nom").status_code, 403)
        self.produit1.refresh_from_db()
        self.assertEqual(self.produit1.nom, "Pagne Baoulé")

    def test_vendeur_ne_reprend_pas_la_main_en_redesactivant(self):
        self.moderer(self.produit1, est_actif=False)
        self.en_tant_que(self.vendeur1)
        self.assertEqual(self.client.delete(f'{URL_V}produits/{self.produit1.pk}/').status_code, 204)
        self.assertEqual(self.etat(), (False, 'administration'))
        self.assertEqual(self.reactiver_en_vendeur().status_code, 403)

    def test_administration_simpose_a_une_desactivation_vendeur(self):
        self.produit1.desactiver(par='vendeur')
        self.moderer(self.produit1, est_actif=False)
        self.assertEqual(self.etat(), (False, 'administration'))
        self.assertEqual(self.reactiver_en_vendeur().status_code, 403)

    def test_administration_leve_sa_propre_desactivation(self):
        self.moderer(self.produit1, est_actif=False)
        self.assertEqual(self.moderer(self.produit1, est_actif=True).status_code, 200)
        self.assertEqual(self.etat(), (True, ''))
        self.assertIn(self.produit1.pk, self.ids_publics())

    def test_vendeur_reactive_sa_propre_desactivation(self):
        self.en_tant_que(self.vendeur1)
        self.client.patch(f'{URL_V}produits/{self.produit1.pk}/', {'est_actif': False}, format='json')
        self.assertEqual(self.etat(), (False, 'vendeur'))
        response = self.reactiver_en_vendeur()
        self.assertEqual((response.status_code, response.data['desactive_par']), (200, ''))

    def test_modification_concurrente_nannule_pas_une_desactivation(self):
        instance_perimee = Produit.objects.get(pk=self.produit1.pk)
        Produit.objects.get(pk=self.produit1.pk).desactiver(par='administration')
        serializer = ProduitVendeurSerializer(
            instance_perimee, data={'description': 'Nouvelle'}, partial=True,
            context={'request': SimpleNamespace(user=self.vendeur1)},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        self.assertEqual(self.etat(), (False, 'administration'))

    def test_creation_inactive_attribuee_au_vendeur(self):
        self.en_tant_que(self.vendeur1)
        response = self.client.post(URL_V + 'produits/', {'nom': 'Brouillon', 'prix_base': '100', 'est_actif': False})
        self.assertEqual((response.status_code, response.data['desactive_par']), (201, 'vendeur'))

    def test_acces_reserve_a_ladministration(self):
        staff = Utilisateur.objects.create_user(
            email="staff@anitche.ci", password="MotDePasse123!", nom="S", prenom="T", role=Role.CLIENT, is_staff=True,
        )
        for utilisateur in (self.vendeur1, staff):
            with self.subTest(role=utilisateur.role):
                self.en_tant_que(utilisateur)
                response = self.client.patch(f'{URL_ADMIN}{self.produit1.pk}/', {'est_actif': False}, format='json')
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.etat(), (True, ''))

    def test_contrainte_de_coherence_en_base(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Produit.objects.filter(pk=self.produit1.pk).update(est_actif=False)


class CataloguePrixTests(CatalogueEspacesBase):
    """FCFA entiers, prix > 0, prix_base ≥ 0, 0 < prix_promo < prix, poids ≥ 0."""

    def creer_variante(self, **corps):
        self.en_tant_que(self.vendeur1)
        return self.client.post(f'{URL_V}produits/{self.produit1.pk}/variantes/', {'nom': 'Test', **corps})

    def test_prix_de_variante_refuses(self):
        cas = {
            'prix nul': {'prix': '0'},
            'prix négatif': {'prix': '-100'},
            'prix non entier': {'prix': '150.50'},
            'promo nulle': {'prix': '100', 'prix_promo': '0'},
            'promo négative': {'prix': '100', 'prix_promo': '-5'},
            'promo égale au prix': {'prix': '100', 'prix_promo': '100'},
            'promo supérieure': {'prix': '100', 'prix_promo': '500'},
            'promo non entière': {'prix': '100', 'prix_promo': '49.99'},
            'poids négatif': {'prix': '100', 'poids_kg': '-3'},
        }
        for libelle, corps in cas.items():
            with self.subTest(libelle):
                self.assertEqual(self.creer_variante(**corps).status_code, 400)
        self.assertEqual(VarianteProduit.objects.filter(produit=self.produit1).count(), 1)

    def test_prix_valides(self):
        response = self.creer_variante(prix='100', prix_promo='80', poids_kg='0.25')
        self.assertEqual((response.status_code, response.data['prix_effectif']), (201, '80.00'))

    def test_montant_non_entier_message_explicite(self):
        response = self.creer_variante(prix='150.50')
        self.assertEqual(str(response.data['errors']['prix'][0]), MESSAGE_MONTANT_ENTIER)

    def test_patch_prix_sous_la_promo_refuse(self):
        VarianteProduit.objects.filter(pk=self.variante1.pk).update(prix_promo=Decimal("12000"))
        self.en_tant_que(self.vendeur1)
        response = self.client.patch(f'{URL_V}variantes/{self.variante1.pk}/', {'prix': '10000'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('prix_promo', response.data['errors'])

    def test_prix_base(self):
        self.en_tant_que(self.vendeur1)
        for prix_base, attendu in (('-5', 400), ('10.5', 400), ('0', 201), ('2500', 201)):
            with self.subTest(prix_base=prix_base):
                response = self.client.post(URL_V + 'produits/', {'nom': 'P', 'prix_base': prix_base})
                self.assertEqual(response.status_code, attendu)

    def test_contraintes_en_base(self):
        for champs in ({'prix': Decimal("0")}, {'prix': Decimal("100"), 'prix_promo': Decimal("100")},
                       {'prix': Decimal("100"), 'poids_kg': Decimal("-1")}):
            with self.subTest(champs=champs):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        VarianteProduit.objects.create(produit=self.produit1, nom="Base", **champs)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Produit.objects.filter(pk=self.produit1.pk).update(prix_base=Decimal("-1"))


class CatalogueStockTests(CatalogueEspacesBase):
    def test_seuil_seul_nannule_pas_une_vente_concurrente(self):
        # Le vendeur a lu le stock (10) ; 3 unités sont vendues avant son
        # enregistrement. Ne changer que le seuil ne doit pas remettre la
        # quantité à 10.
        get_object_original = StockUpdateView.get_object

        def lecture_puis_vente(vue):
            stock = get_object_original(vue)
            Stock.objects.filter(pk=stock.pk).update(quantite_disponible=7)
            return stock

        self.en_tant_que(self.vendeur1)
        with mock.patch.object(StockUpdateView, 'get_object', lecture_puis_vente):
            response = self.client.patch(f'{URL_V}variantes/{self.variante1.pk}/stock/', {'seuil_alerte': 2})
        self.assertEqual(response.status_code, 200)
        self.variante1.stock.refresh_from_db()
        self.assertEqual((self.variante1.stock.quantite_disponible, self.variante1.stock.seuil_alerte), (7, 2))
        self.assertEqual(response.data, {'quantite_disponible': 7, 'seuil_alerte': 2})

    def test_stock_negatif_refuse(self):
        self.en_tant_que(self.vendeur1)
        response = self.client.patch(f'{URL_V}variantes/{self.variante1.pk}/stock/', {'quantite_disponible': -1})
        self.assertEqual(response.status_code, 400)


class CatalogueImagesTests(CatalogueEspacesBase):
    def envoyer(self, fichier):
        self.en_tant_que(self.vendeur1)
        return self.client.post(f'{URL_V}produits/{self.produit1.pk}/images/', {'image': fichier}, format='multipart')

    def test_dix_images_au_maximum(self):
        for _ in range(MAX_IMAGES_PAR_PRODUIT):
            self.assertEqual(self.envoyer(image_png()).status_code, 201)
        response = self.envoyer(image_png())
        self.assertEqual(response.status_code, 400)
        self.assertIn('10 images', str(response.data['errors']['image']))
        self.assertEqual(ImageProduit.objects.filter(produit=self.produit1).count(), MAX_IMAGES_PAR_PRODUIT)

    def test_contenu_falsifie_refuse(self):
        faux = SimpleUploadedFile('photo.png', b'<html>pas une image</html>', content_type='image/png')
        self.assertEqual(self.envoyer(faux).status_code, 400)

    def test_limite_de_5_mo(self):
        # Une vraie image PNG de plus de 5 Mo (octets aléatoires, incompressibles).
        lourde = image_png(taille=(1400, 1400), octets_aleatoires=True)
        self.assertGreater(lourde.size, 5 * 1024 * 1024)
        response = self.envoyer(lourde)
        self.assertEqual(response.status_code, 400)
        self.assertIn('5 Mo', str(response.data['errors']['image']))
        # Entre 3 et 5 Mo : accepté (seule la limite de 5 Mo s'applique).
        moyenne = image_png(taille=(1150, 1150), octets_aleatoires=True)
        self.assertTrue(3 * 1024 * 1024 < moyenne.size < 5 * 1024 * 1024)
        self.assertEqual(self.envoyer(moyenne).status_code, 201)


class CataloguePerformanceTests(CatalogueEspacesBase):
    def compter_requetes(self, url, utilisateur=None):
        self.client.force_authenticate(utilisateur)
        with CaptureQueriesContext(connection) as requetes:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(requetes.captured_queries)

    def ajouter_produits(self, nombre):
        for i in range(nombre):
            produit = Produit.objects.create(boutique=self.boutique1, nom=f"Lot {i}")
            variante = VarianteProduit.objects.create(produit=produit, nom="Unique", prix=Decimal("1000"))
            ImageProduit.objects.create(produit=produit, image=image_png())
            Stock.objects.filter(variante=variante).update(quantite_disponible=1)

    def test_listes_sans_n_plus_1(self):
        # Le nombre de requêtes ne dépend pas du nombre de produits listés.
        self.ajouter_produits(3)
        public_avant = self.compter_requetes(URL_P + 'produits/')
        vendeur_avant = self.compter_requetes(URL_V + 'produits/', self.vendeur1)
        self.ajouter_produits(6)
        self.assertEqual(self.compter_requetes(URL_P + 'produits/'), public_avant)
        self.assertEqual(self.compter_requetes(URL_V + 'produits/', self.vendeur1), vendeur_avant)


class CatalogueDebitTests(CatalogueEspacesBase):
    def test_limite_dediee_non_contournable_par_x_forwarded_for(self):
        with mock.patch.object(SimpleRateThrottle, 'THROTTLE_RATES', {'catalogue_public': '3/hour'}):
            codes = [self.client.get(URL_P + 'produits/', REMOTE_ADDR='9.9.9.9').status_code for _ in range(2)]
            codes.append(self.client.get(URL_P + 'categories/', REMOTE_ADDR='9.9.9.9').status_code)
            codes.append(self.client.get(f'{URL_P}produits/{self.produit1.slug}/', REMOTE_ADDR='9.9.9.9').status_code)
            codes.append(self.client.get(
                URL_P + 'produits/', REMOTE_ADDR='9.9.9.9', HTTP_X_FORWARDED_FOR='1.2.3.4',
            ).status_code)
            codes.append(self.client.get(URL_P + 'produits/', REMOTE_ADDR='8.8.8.8').status_code)
        self.assertEqual(codes, [200, 200, 200, 429, 429, 200])

    def test_limite_par_compte_avec_un_jeton_valide(self):
        """Un jeton valide identifie le compte : deux clients connectés derrière
        la même IP (CGNAT) ont chacun leur seau ; un jeton expiré compte par IP."""
        def get(utilisateur=None, expire=False):
            entetes = {}
            if utilisateur:
                jeton = AccessToken.for_user(utilisateur)
                if expire:
                    jeton.set_exp(lifetime=timedelta(seconds=-1))
                entetes['HTTP_AUTHORIZATION'] = f'Bearer {jeton}'
            return self.client.get(URL_P + 'produits/', REMOTE_ADDR='9.9.9.9', **entetes).status_code

        with mock.patch.object(SimpleRateThrottle, 'THROTTLE_RATES', {'catalogue_public': '2/hour'}):
            codes_client = [get(self.client_user) for _ in range(3)]
            codes_vendeur = [get(self.vendeur1) for _ in range(2)]
            codes_ip = [get(self.vendeur2, expire=True) for _ in range(3)]
        self.assertEqual(codes_client, [200, 200, 429])
        self.assertEqual(codes_vendeur, [200, 200])
        self.assertEqual(codes_ip, [200, 200, 429])

    def test_jeton_expire_ignore_sur_les_routes_publiques(self):
        """Un vieux jeton gardé par le client ne casse pas le catalogue (pas de 401)."""
        jeton = AccessToken.for_user(self.client_user)
        jeton.set_exp(lifetime=timedelta(seconds=-1))
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {jeton}')
        for url in (
            URL_P + 'categories/', f'{URL_P}categories/{self.cat_mode.slug}/',
            URL_P + 'produits/', f'{URL_P}produits/{self.produit1.slug}/',
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)


# =====================================================================
# VUES SQL LUES PAR FASTAPI (migration 0004) : PARITÉ AVEC DJANGO
# =====================================================================

#: Colonnes de chaque vue, et rien d'autre : c'est tout ce que FastAPI peut
#: lire du catalogue. Ajouter une colonne impose de revoir ce qu'elle expose.
COLONNES_VUES_PUBLIQUES = {
    'catalogue_produit_public': {
        'id', 'nom', 'slug', 'prix_base', 'date_creation',
        'categorie_id', 'categorie_nom', 'categorie_slug', 'categorie_parent_id', 'categorie_parent_slug',
        'boutique_id', 'boutique_nom', 'boutique_slug',
        'prix_min', 'en_stock', 'image_principale', 'nom_normalise', 'texte_normalise',
    },
    'catalogue_categorie_publique': {'id', 'nom', 'slug', 'parent_id', 'nom_normalise'},
    'catalogue_boutique_publique': {'id', 'nom', 'slug', 'nom_normalise'},
}

#: Jamais dans une vue : stock exact et seuil d'alerte, états internes
#: (désactivation, suspension, SKU), données du propriétaire de la boutique.
FRAGMENTS_SENSIBLES = (
    'quantite', 'seuil', 'sku', 'desactive', 'suspendu', 'est_',
    'proprietaire', 'email', 'telephone', 'adresse', 'ville', 'kyc', 'role', 'password', 'is_',
)


@skipUnless(connection.vendor == 'postgresql', "Vues SQL de la migration 0004 : PostgreSQL uniquement")
class CatalogueVuesPubliquesTests(CatalogueEspacesBase):
    """Chaque test place un cas de visibilité, puis vérifie que la vue et
    Django donnent les mêmes produits, avec les mêmes prix affichés. Échoue
    dès qu'une règle change d'un seul côté (modèles ou migration)."""

    def lire_vue(self, vue):
        self.assertIn(vue, COLONNES_VUES_PUBLIQUES)
        with connection.cursor() as curseur:
            curseur.execute(f'SELECT * FROM {vue}')
            colonnes = [colonne.name for colonne in curseur.description]
            return {ligne[0]: dict(zip(colonnes, ligne)) for ligne in curseur.fetchall()}

    def liste_publique_django(self):
        vue = ProduitPublicListView()
        vue.request = SimpleNamespace(query_params={})
        return {produit.pk: produit for produit in vue.get_queryset()}

    def verifier_parite(self):
        """Renvoie les lignes de catalogue_produit_public après avoir vérifié
        qu'elles correspondent exactement à la liste publique Django."""
        vue = self.lire_vue('catalogue_produit_public')
        django = self.liste_publique_django()
        self.assertEqual(set(vue), set(Produit.objects.visibles_publiquement().values_list('pk', flat=True)))
        self.assertEqual(set(vue), set(django))
        for pk, produit in django.items():
            with self.subTest(produit=produit.nom):
                ligne, element, categorie = vue[pk], ProduitPublicListSerializer(produit).data, produit.categorie
                image = ligne['image_principale']
                self.assertEqual(ligne['prix_min'], min(v.prix_effectif for v in produit.variantes.all() if v.est_active))
                self.assertEqual({
                    'nom': ligne['nom'], 'slug': ligne['slug'], 'prix_base': ligne['prix_base'],
                    'prix_min': ligne['prix_min'], 'en_stock': ligne['en_stock'],
                    'image_principale': default_storage.url(image) if image else None,
                    'categorie': ligne['categorie_id'], 'categorie_nom': ligne['categorie_nom'],
                    'boutique': ligne['boutique_id'], 'boutique_nom': ligne['boutique_nom'],
                    'boutique_slug': ligne['boutique_slug'], 'date_creation': ligne['date_creation'],
                    'categorie_slug': ligne['categorie_slug'], 'categorie_parent_id': ligne['categorie_parent_id'],
                    'categorie_parent_slug': ligne['categorie_parent_slug'],
                }, {
                    'nom': element['nom'], 'slug': element['slug'], 'prix_base': Decimal(element['prix_base']),
                    'prix_min': element['prix_min'], 'en_stock': element['en_stock'],
                    'image_principale': element['image_principale'],
                    'categorie': element['categorie'], 'categorie_nom': element.get('categorie_nom'),
                    'boutique': element['boutique'], 'boutique_nom': element['boutique_nom'],
                    'boutique_slug': element['boutique_slug'], 'date_creation': produit.date_creation,
                    # Colonnes du filtre `categorie` (slug du produit ou de son parent).
                    'categorie_slug': categorie.slug if categorie else None,
                    'categorie_parent_id': categorie.parent_id if categorie else None,
                    'categorie_parent_slug': categorie.parent.slug if categorie and categorie.parent else None,
                })
        self.assertEqual(
            set(self.lire_vue('catalogue_boutique_publique')), set(Boutique.objects.publiques().values_list('pk', flat=True)),
        )
        self.assertEqual(
            set(self.lire_vue('catalogue_categorie_publique')), set(Categorie.objects.actives().values_list('pk', flat=True)),
        )
        return vue

    def verifier_masque_par(self, produit, changement):
        self.assertIn(produit.pk, self.verifier_parite())
        changement()
        self.assertNotIn(produit.pk, self.verifier_parite())

    def nouveau_produit(self, boutique=None, nom="Produit de test", prix='10000', promo=None, quantite=5, **champs):
        produit = Produit.objects.create(boutique=boutique or self.boutique1, nom=nom, **champs)
        variante = VarianteProduit.objects.create(
            produit=produit, nom="Unique", prix=Decimal(prix), prix_promo=Decimal(promo) if promo else None,
        )
        Stock.objects.filter(variante=variante).update(quantite_disponible=quantite)
        return produit

    def nouvelle_boutique(self, code):
        vendeur = Utilisateur.objects.create_user(
            email=f"{code}@vues.anitche.ci", password="MotDePasse123!", nom="Vue", prenom=code,
            role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE,
        )
        return Boutique.objects.create(proprietaire=vendeur, nom=f"Boutique {code}")

    def test_produits_de_reference(self):
        vue = self.verifier_parite()
        self.assertEqual(set(vue), {self.produit1.pk, self.produit2.pk})

    def test_produit_desactive_par_le_vendeur_ou_par_ladministration(self):
        for origine in Produit.OrigineDesactivation:
            with self.subTest(origine=origine):
                produit = self.nouveau_produit()
                self.verifier_masque_par(produit, lambda: produit.desactiver(par=origine))

    def test_boutique_fermee_suspendue_ou_vendeur_non_valide(self):
        def boutique(**champs):
            return lambda b: Boutique.objects.filter(pk=b.pk).update(**champs)

        def vendeur(**champs):
            return lambda b: Utilisateur.objects.filter(pk=b.proprietaire_id).update(**champs)

        cas = [
            ('suspendue', boutique(est_suspendue=True)),
            ('fermee', boutique(est_active=False)),
            ('kyc-en-attente', vendeur(statut_kyc=StatutKYC.EN_ATTENTE)),
            ('kyc-refuse', vendeur(statut_kyc=StatutKYC.REFUSE)),
            ('redevenu-client', vendeur(role=Role.CLIENT)),
            ('vendeur-inactif', vendeur(is_active=False)),
        ]
        for code, changement in cas:
            with self.subTest(cas=code):
                nouvelle = self.nouvelle_boutique(code)
                produit = self.nouveau_produit(nouvelle)
                self.verifier_masque_par(produit, lambda: changement(nouvelle))
                self.assertNotIn(nouvelle.pk, self.lire_vue('catalogue_boutique_publique'))

    def test_variantes_inactives(self):
        toutes_inactives = self.nouveau_produit()
        self.verifier_masque_par(
            toutes_inactives, lambda: VarianteProduit.objects.filter(produit=toutes_inactives).update(est_active=False),
        )
        sans_variante = Produit.objects.create(boutique=self.boutique1, nom="Sans variante")
        self.assertNotIn(sans_variante.pk, self.verifier_parite())

        # Une variante inactive, moins chère et seule en stock : ni son prix ni son stock ne comptent.
        mixte = self.nouveau_produit(prix='20000', quantite=0)
        retiree = VarianteProduit.objects.create(
            produit=mixte, nom="Retirée", prix=Decimal('5000'), prix_promo=Decimal('1000'), est_active=False,
        )
        Stock.objects.filter(variante=retiree).update(quantite_disponible=50)
        ligne = self.verifier_parite()[mixte.pk]
        self.assertEqual((ligne['prix_min'], ligne['en_stock']), (Decimal('20000.00'), False))

    def test_rupture_de_stock(self):
        # Le stock n'est pas une condition de visibilité : seul `en_stock` change.
        produit = self.nouveau_produit(quantite=3)
        self.assertTrue(self.verifier_parite()[produit.pk]['en_stock'])
        Stock.objects.filter(variante__produit=produit).update(quantite_disponible=0)
        self.assertFalse(self.verifier_parite()[produit.pk]['en_stock'])
        Stock.objects.filter(variante__produit=produit).delete()
        self.assertFalse(self.verifier_parite()[produit.pk]['en_stock'])
        autre = VarianteProduit.objects.create(produit=produit, nom="Autre", prix=Decimal('12000'))
        Stock.objects.filter(variante=autre).update(quantite_disponible=1)
        self.assertTrue(self.verifier_parite()[produit.pk]['en_stock'])

    def test_promotion(self):
        # Prix affiché = plus petit prix effectif des variantes actives, promo comprise.
        produit = self.nouveau_produit(prix='10000', promo='7000')
        VarianteProduit.objects.create(produit=produit, nom="Grande", prix=Decimal('8000'))
        self.assertEqual(self.verifier_parite()[produit.pk]['prix_min'], Decimal('7000.00'))
        VarianteProduit.objects.filter(produit=produit).update(prix_promo=None)
        self.assertEqual(self.verifier_parite()[produit.pk]['prix_min'], Decimal('8000.00'))

    def test_categorie_inactive_ou_absente(self):
        # Règle Django actuelle (dette, MODULE_CATALOGUE.md § 9) : le produit reste
        # visible ; seule la catégorie disparaît de catalogue_categorie_publique.
        Categorie.objects.filter(pk__in=[self.cat_mode.pk, self.cat_chaussures.pk]).update(est_active=False)
        sans_categorie = self.nouveau_produit()
        vue = self.verifier_parite()
        self.assertTrue({self.produit1.pk, sans_categorie.pk} <= set(vue))
        self.assertEqual(
            (vue[self.produit1.pk]['categorie_slug'], vue[self.produit1.pk]['categorie_parent_slug']),
            (self.cat_chaussures.slug, self.cat_mode.slug),
        )
        self.assertNotIn(self.cat_chaussures.pk, self.lire_vue('catalogue_categorie_publique'))

    def test_image_principale(self):
        sans_principale = self.nouveau_produit()
        ImageProduit.objects.create(produit=sans_principale, image=image_png('b.png'), ordre=2)
        premiere = ImageProduit.objects.create(produit=sans_principale, image=image_png('a.png'), ordre=1)
        ImageProduit.objects.create(produit=self.produit1, image=image_png('c.png'), ordre=1)
        principale = ImageProduit.objects.create(
            produit=self.produit1, image=image_png('d.png'), ordre=5, est_principale=True,
        )
        vue = self.verifier_parite()
        self.assertEqual(vue[sans_principale.pk]['image_principale'], premiere.image.name)
        self.assertEqual(vue[self.produit1.pk]['image_principale'], principale.image.name)
        self.assertIsNone(vue[self.produit2.pk]['image_principale'])

    def test_textes_normalises_sans_accents_ni_majuscules(self):
        produit = self.nouveau_produit(nom="Écouteurs ÉLÉGANTS", description="Son clair à Abidjan")
        ligne = self.verifier_parite()[produit.pk]
        self.assertEqual(ligne['nom_normalise'], "ecouteurs elegants")
        self.assertEqual(ligne['texte_normalise'], "ecouteurs elegants son clair a abidjan")
        categories = self.lire_vue('catalogue_categorie_publique')
        self.assertEqual(categories[self.cat_mode.pk]['nom_normalise'], "mode & vetements")

    def test_aucune_colonne_sensible(self):
        with connection.cursor() as curseur:
            curseur.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
                [list(COLONNES_VUES_PUBLIQUES)],
            )
            colonnes = curseur.fetchall()
        for vue, autorisees in COLONNES_VUES_PUBLIQUES.items():
            with self.subTest(vue=vue):
                exposees = {colonne for nom_vue, colonne, _ in colonnes if nom_vue == vue}
                self.assertEqual({c for c in exposees if any(f in c for f in FRAGMENTS_SENSIBLES)}, set())
                self.assertEqual(exposees, autorisees)
        # Disponibilité seulement : un booléen, jamais une quantité.
        types = {(nom_vue, colonne): type_ for nom_vue, colonne, type_ in colonnes}
        self.assertEqual(types[('catalogue_produit_public', 'en_stock')], 'boolean')
