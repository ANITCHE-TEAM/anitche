from decimal import Decimal
from unittest import skipUnless
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APITestCase, APIClient
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
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(demande.id))

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


class RetoursConcurrenceTestCase(TransactionTestCase):
    """A04:2025 : deux demandes de retour concurrentes sur le même article
    ne doivent jamais pouvoir cumuler une quantité retournée supérieure à
    ce qui a été réellement acheté (même famille de protection que F-09
    sur le stock)."""

    def setUp(self):
        self.client1 = Utilisateur.objects.create_user(
            email="client1@anitche.ci", password="TestPassword123!",
            nom="Konan", prenom="Aya", role=Role.CLIENT,
        )
        vendeur1 = Utilisateur.objects.create_user(
            email="vendeur1@anitche.ci", password="TestPassword123!",
            nom="Kouassi", prenom="Jean", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE,
        )
        boutique1 = Boutique.objects.create(proprietaire=vendeur1, nom="Boutique Ivoire", est_active=True)
        produit1 = Produit.objects.create(boutique=boutique1, nom="Robe Baoulé", prix_base=Decimal("20000.00"))
        variante1 = VarianteProduit.objects.create(produit=produit1, nom="Taille M", prix=Decimal("20000.00"))

        # Un seul exemplaire acheté : la moindre double comptabilisation
        # est donc directement observable.
        self.commande1 = Commande.objects.create(
            boutique=boutique1, client=self.client1,
            montant_total=Decimal("20000.00"), status=Commande.Status.LIVREE,
        )
        self.item1 = CommandeItem.objects.create(
            commande=self.commande1, variante=variante1, nom_produit="Robe Baoulé",
            prix_unitaire=Decimal("20000.00"), quantite=1,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "DIAGNOSTIC : la vue verrouille correctement la ligne Commande "
        "via select_for_update() dans transaction.atomic() (voir "
        "apps/retours/views.py) — le mécanisme visé par ce test est réel "
        "et correct. Mais select_for_update() est un no-op sur SQLite "
        "(pas de verrouillage de ligne), qui ne connaît qu'un verrou "
        "global de fichier ; deux transactions d'écriture concurrentes "
        "s'y soldent par une OperationalError('database is locked') "
        "plutôt que par l'attente-puis-rejet attendu, un comportement "
        "propre à SQLite et absent de PostgreSQL (la base cible réelle "
        "du projet, voir CLAUDE.md). Ce test ne peut donc valider ce "
        "qu'il prétend valider que contre PostgreSQL — il tournera en CI "
        "si celle-ci utilise Postgres pour les tests, comme prévu en "
        "production.",
    )
    def test_deux_demandes_retour_concurrentes_meme_article_une_seule_acceptee(self):
        from concurrent.futures import ThreadPoolExecutor
        import django.db

        url = reverse("retours:retour-liste-creer")
        payload = {
            "commande_id": str(self.commande1.id),
            "description": "Test de concurrence sur un article acheté en un seul exemplaire.",
            "articles": [{"commande_item_id": str(self.item1.id), "quantite": 1}],
        }

        def appel():
            django.db.close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.client1)
                return client.post(url, payload, format="json").status_code
            finally:
                django.db.connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuts = list(executor.map(lambda _: appel(), range(2)))

        self.assertEqual(statuts.count(status.HTTP_201_CREATED), 1)
        self.assertEqual(statuts.count(status.HTTP_400_BAD_REQUEST), 1)
        # La quantité totale réellement retournée ne doit jamais dépasser
        # ce qui a été acheté (1 exemplaire).
        total_retourne = sum(
            RetourItem.objects.filter(commande_item=self.item1).values_list("quantite", flat=True)
        )
        self.assertEqual(total_retourne, 1)


class RetoursAdminTestCase(APITestCase):
    """La quantité d'un article retourné et la photo justificative ne
    doivent jamais être modifiables depuis l'admin Django après coup :
    la première est la base de calcul de montant_remboursement ET du
    restock, la seconde est une preuve dans un litige."""

    def test_statut_et_montant_remboursement_readonly_dans_admin(self):
        """F-06 : 'statut' et 'montant_remboursement' doivent rester en
        lecture seule dans DemandeRetourAdmin, sinon un compte staff peut
        forcer une transition (ex. passer directement à 'rembourse') sans
        passer par TraiterDemandeRetourView et sa machine à états."""
        from apps.retours.admin import DemandeRetourAdmin
        from django.contrib.admin.sites import AdminSite

        admin_instance = DemandeRetourAdmin(DemandeRetour, AdminSite())
        self.assertIn("statut", admin_instance.readonly_fields)
        self.assertIn("montant_remboursement", admin_instance.readonly_fields)

    def test_quantite_retour_item_readonly_dans_admin(self):
        from apps.retours.admin import RetourItemAdmin
        from django.contrib.admin.sites import AdminSite

        admin_instance = RetourItemAdmin(RetourItem, AdminSite())
        self.assertIn("quantite", admin_instance.readonly_fields)

    def test_image_photo_retour_readonly_dans_admin(self):
        from apps.retours.admin import PhotoRetourAdmin
        from apps.retours.models import PhotoRetour
        from django.contrib.admin.sites import AdminSite

        admin_instance = PhotoRetourAdmin(PhotoRetour, AdminSite())
        self.assertIn("image", admin_instance.readonly_fields)
