from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from apps.livraison.models import Livraison
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
        # Colis remis hier : dans le délai de retour.
        self.livraison1 = Livraison.objects.create(
            commande=self.commande1, status=Livraison.Status.LIVREE,
            date_livraison=timezone.now() - timedelta(days=1),
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
    ce qui a été réellement acheté (même famille de protection que
    l'atomicité de Stock.incrementer/decrementer)."""

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
        Livraison.objects.create(commande=self.commande1, status=Livraison.Status.LIVREE, date_livraison=timezone.now())

    @skipUnless(
        connection.vendor == "postgresql",
        "DIAGNOSTIC : la vue verrouille correctement la ligne Commande "
        "via select_for_update() dans transaction.atomic() (voir "
        "apps/retours/services.py) — le mécanisme visé par ce test est réel "
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


    @skipUnless(connection.vendor == "postgresql", "Concurrence réelle : PostgreSQL uniquement")
    def test_deux_demandes_concurrentes_frais_de_livraison_rendus_une_seule_fois(self):
        """Deux demandes simultanées (motif imputable au vendeur) sur deux
        exemplaires d'une même commande : les frais de livraison ne sont
        inclus que dans l'une (verrou de la commande)."""
        from concurrent.futures import ThreadPoolExecutor
        import django.db

        commande = Commande.objects.create(
            boutique=self.item1.commande.boutique, client=self.client1, montant_total=Decimal("41500.00"),
            frais_livraison=Decimal("1500.00"), status=Commande.Status.LIVREE,
        )
        item = CommandeItem.objects.create(
            commande=commande, variante=self.item1.variante, nom_produit="Robe Baoulé",
            prix_unitaire=Decimal("20000.00"), quantite=2,
        )
        Livraison.objects.create(commande=commande, status=Livraison.Status.LIVREE, date_livraison=timezone.now())
        payload = {
            "commande_id": str(commande.id), "motif": "produit_defectueux",
            "description": "Couture déchirée à la réception du colis.",
            "articles": [{"commande_item_id": str(item.id), "quantite": 1}],
        }

        def appel():
            django.db.close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.client1)
                return client.post(reverse("retours:retour-liste-creer"), payload, format="json").status_code
            finally:
                django.db.connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuts = list(executor.map(lambda _: appel(), range(2)))

        self.assertEqual(statuts, [status.HTTP_201_CREATED] * 2)
        frais = sorted(DemandeRetour.objects.filter(commande=commande).values_list("frais_livraison_rembourses", flat=True))
        self.assertEqual(frais, [Decimal("0"), Decimal("1500")])

class RetoursAdminTestCase(APITestCase):
    """La quantité d'un article retourné et la photo justificative ne
    doivent jamais être modifiables depuis l'admin Django après coup :
    la première est la base de calcul de montant_remboursement ET du
    restock, la seconde est une preuve dans un litige."""

    def test_statut_et_montant_remboursement_readonly_dans_admin(self):
        """'statut' et 'montant_remboursement' doivent rester en
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


# =====================================================================
# Éligibilité, montants, transitions, photos et limites des retours
# (docs/MODULE_RETOURS.md, § Sécurité).
# =====================================================================

import io
import threading
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.throttling import SimpleRateThrottle

from apps.notifications.models import Notification
from apps.paiements import reversements
from apps.paiements.models import Paiement, Remboursement, Reversement
from .models import PhotoRetour

URL_RETOURS = "/api/retours/"


def image_png(nom="preuve.png"):
    tampon = io.BytesIO()
    Image.new("RGB", (4, 4)).save(tampon, "PNG")
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type="image/png")


class RetoursCycleBase(BaseRetourTestCase):
    def setUp(self):
        super().setUp()
        self.admin = Utilisateur.objects.create_user(
            email="admin.retours@anitche.ci", password="TestPassword123!", nom="A", prenom="D", role=Role.ADMIN,
        )

    def demander(self, commande=None, articles=None, utilisateur=None, **extra):
        commande = commande or self.commande1
        self.client.force_authenticate(user=utilisateur or self.client1)
        corps = {
            "commande_id": str(commande.id),
            "description": "Couture déchirée au déballage du colis.",
            "articles": articles or [{"commande_item_id": str(self.item1.id), "quantite": 1}],
            **extra,
        }
        return self.client.post(URL_RETOURS, corps, format="json")

    def agir(self, demande_id, utilisateur, action, **extra):
        self.client.force_authenticate(user=utilisateur)
        return self.client.patch(f"{URL_RETOURS}{demande_id}/traiter/", {"action": action, **extra}, format="json")

    def demande_au_statut(self, statut, quantite=1):
        demande = DemandeRetour.objects.create(
            commande=self.commande1, client=self.client1, boutique=self.boutique1,
            description="Défaut constaté", montant_remboursement=Decimal("20000") * quantite, statut=statut,
        )
        RetourItem.objects.create(demande_retour=demande, commande_item=self.item1, quantite=quantite)
        return demande


class EligibiliteTests(RetoursCycleBase):
    """Commande livrée seulement, et dans le délai de retour."""

    def test_commande_confirmee_ou_expediee_refusee(self):
        for statut in (Commande.Status.CONFIRMEE, Commande.Status.EXPEDIEE, Commande.Status.PREPARATION):
            with self.subTest(statut=statut):
                Commande.objects.filter(pk=self.commande1.pk).update(status=statut)
                r = self.demander()
                self.assertEqual(r.status_code, 400)
                self.assertIn("livrée", str(r.data))
        self.assertFalse(DemandeRetour.objects.exists())

    def test_delai_ecoule_refuse(self):
        Livraison.objects.filter(pk=self.livraison1.pk).update(date_livraison=timezone.now() - timedelta(days=8))
        r = self.demander()
        self.assertEqual(r.status_code, 400)
        self.assertIn("délai", str(r.data))

    def test_dans_le_delai_accepte(self):
        Livraison.objects.filter(pk=self.livraison1.pk).update(date_livraison=timezone.now() - timedelta(days=6))
        self.assertEqual(self.demander().status_code, 201)

    @override_settings(RETOUR_DELAI_JOURS=30)
    def test_delai_reglable(self):
        Livraison.objects.filter(pk=self.livraison1.pk).update(date_livraison=timezone.now() - timedelta(days=20))
        self.assertEqual(self.demander().status_code, 201)

    def test_livree_sans_date_de_livraison_refusee(self):
        Livraison.objects.filter(pk=self.livraison1.pk).update(date_livraison=None)
        self.assertEqual(self.demander().status_code, 400)

    def test_commande_d_un_autre_client_introuvable(self):
        self.assertEqual(self.demander(utilisateur=self.client2).status_code, 400)
        self.assertFalse(DemandeRetour.objects.exists())

    def test_echange_et_avoir_refuses(self):
        for type_resolution in ("echange", "avoir"):
            with self.subTest(type_resolution=type_resolution):
                self.assertEqual(self.demander(type_resolution=type_resolution).status_code, 400)

    def test_meme_ligne_citee_deux_fois_dans_une_demande(self):
        ligne = {"commande_item_id": str(self.item1.id), "quantite": 2}
        self.assertEqual(self.demander(articles=[ligne, ligne]).status_code, 400)

    def test_demande_annulee_ne_bloque_pas_une_nouvelle_demande(self):
        r = self.demander(articles=[{"commande_item_id": str(self.item1.id), "quantite": 2}])
        self.assertEqual(self.agir(r.data["id"], self.client1, "annuler").status_code, 200)
        self.assertEqual(self.demander(articles=[{"commande_item_id": str(self.item1.id), "quantite": 2}]).status_code, 201)


class MontantRembourseTests(RetoursCycleBase):
    """Le client récupère ce qu'il a payé, remise du coupon déduite."""

    def setUp(self):
        super().setUp()
        Commande.objects.filter(pk=self.commande1.pk).update(montant_total=Decimal("30000"), montant_remise=Decimal("10000"))

    def test_remise_deduite_au_prorata(self):
        r = self.demander()
        self.assertEqual(Decimal(r.data["montant_remboursement"]), Decimal("15000"))

    def test_retours_partiels_jamais_au_dela_du_paye(self):
        self.demander()
        self.demander()
        total = sum(DemandeRetour.objects.values_list("montant_remboursement", flat=True))
        self.assertEqual(total, Decimal("30000"))

    def test_arrondi_au_franc_inferieur(self):
        Commande.objects.filter(pk=self.commande1.pk).update(montant_total=Decimal("33333"))
        r = self.demander()
        self.assertEqual(Decimal(r.data["montant_remboursement"]), Decimal("16666"))


class TransitionsTests(RetoursCycleBase):
    """Qui fait quoi, et depuis quel statut."""

    def test_rejet_apres_reception_refuse(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.RECEPTIONNE)
        r = self.agir(demande.id, self.vendeur1, "rejeter", reponse="Article abîmé par le client.")
        self.assertEqual(r.status_code, 400)
        demande.refresh_from_db()
        self.assertEqual(demande.statut, DemandeRetour.Statut.RECEPTIONNE)

    def test_rejet_sans_motif_refuse(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "rejeter").status_code, 400)

    def test_rejet_alerte_l_administration_et_le_client(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "rejeter", reponse="Hors délai.").status_code, 200)
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre__icontains="rejeté").exists())
        self.assertTrue(Notification.objects.filter(destinataire=self.client1, metadata__retour_id=str(demande.id)).exists())
        # Lien du portail d'administration, jamais /admin/ (admin Django).
        self.assertEqual(
            Notification.objects.get(destinataire=self.admin, titre__icontains="rejeté").lien_redirection,
            f"/administration/retours/{demande.id}",
        )
        self.assertEqual(
            Notification.objects.get(destinataire=self.client1, metadata__retour_id=str(demande.id)).lien_redirection,
            f"/retours/{demande.id}",
        )

    def test_client_marque_expedie_et_le_vendeur_est_prevenu(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.APPROUVE)
        r = self.agir(demande.id, self.client1, "en_transit")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["statut"], "en_transit")
        self.assertTrue(Notification.objects.filter(destinataire=self.vendeur1, metadata__statut="en_transit").exists())

    def test_client_annule_sa_demande_et_le_reversement_reprend(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        with patch("apps.retours.services.reprendre_reversement") as reprendre:
            r = self.agir(demande.id, self.client1, "annuler")
        self.assertEqual((r.status_code, r.data["statut"]), (200, "annule"))
        reprendre.assert_called_once()

    def test_client_ne_decide_pas_a_la_place_du_vendeur(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        for action in ("approuver", "rejeter", "receptionner", "rembourser"):
            with self.subTest(action=action):
                self.assertEqual(self.agir(demande.id, self.client1, action, reponse="x").status_code, 403)

    def test_vendeur_n_annule_pas_la_demande_du_client(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "annuler").status_code, 403)

    def test_tiers_recoit_404(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        for tiers in (self.client2, self.vendeur2):
            with self.subTest(tiers=tiers.email):
                self.assertEqual(self.agir(demande.id, tiers, "approuver").status_code, 404)

    def test_administration_peut_agir(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        self.assertEqual(self.agir(demande.id, self.admin, "approuver").status_code, 200)

    def test_reception_sans_passage_en_transit(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.APPROUVE)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "receptionner").status_code, 200)
        self.variante1.stock.refresh_from_db()
        self.assertEqual(self.variante1.stock.quantite_disponible, 6)

    def test_statuts_definitifs(self):
        for statut in (DemandeRetour.Statut.REJETE, DemandeRetour.Statut.ANNULE, DemandeRetour.Statut.CLOTURE):
            demande = self.demande_au_statut(statut)
            for action in ("approuver", "cloturer", "annuler", "rembourser"):
                with self.subTest(statut=statut, action=action):
                    utilisateur = self.client1 if action == "annuler" else self.vendeur1
                    self.assertEqual(self.agir(demande.id, utilisateur, action).status_code, 400)


class NotificationEtVisibiliteTests(RetoursCycleBase):
    """Le vendeur est prévenu ; un vendeur voit aussi ses retours d'acheteur."""

    def test_vendeur_notifie_d_une_nouvelle_demande(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.demander()
        self.assertEqual(r.status_code, 201)
        self.assertTrue(Notification.objects.filter(destinataire=self.vendeur1, metadata__retour_id=r.data["id"]).exists())

    def test_vendeur_acheteur_voit_son_propre_retour(self):
        commande = Commande.objects.create(boutique=self.boutique2, client=self.vendeur1,
                                           montant_total=Decimal("20000"), status=Commande.Status.LIVREE)
        article = CommandeItem.objects.create(commande=commande, variante=self.variante1, nom_produit="Robe",
                                              prix_unitaire=Decimal("20000"), quantite=1)
        Livraison.objects.create(commande=commande, status=Livraison.Status.LIVREE, date_livraison=timezone.now())
        r = self.demander(commande=commande, utilisateur=self.vendeur1,
                          articles=[{"commande_item_id": str(article.id), "quantite": 1}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.get(f"{URL_RETOURS}{r.data['id']}/").status_code, 200)
        # …mais sa liste « espace vendeur » ne montre que ceux de sa boutique.
        ids = [d["id"] for d in self.client.get(f"{URL_RETOURS}vendeur/liste/").data["results"]]
        self.assertNotIn(r.data["id"], ids)


class PhotosTests(RetoursCycleBase):
    """Photos limitées, renommées, servies après contrôle d'accès."""

    def setUp(self):
        super().setUp()
        self.demande = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)

    def ajouter(self, utilisateur=None, nom="preuve.png"):
        self.client.force_authenticate(user=utilisateur or self.client1)
        return self.client.post(f"{URL_RETOURS}{self.demande.id}/photos/", {"image": image_png(nom)}, format="multipart")

    def test_nom_d_origine_jamais_conserve(self):
        self.assertEqual(self.ajouter(nom="Aya_Konan_CNI.png").status_code, 201)
        nom = PhotoRetour.objects.get().image.name
        self.assertNotIn("Aya_Konan", nom)
        self.assertTrue(nom.startswith("retours/preuves/") and nom.endswith(".png"))

    def test_plus_de_photo_une_fois_le_retour_traite(self):
        for statut in (DemandeRetour.Statut.RECEPTIONNE, DemandeRetour.Statut.REJETE,
                       DemandeRetour.Statut.CLOTURE, DemandeRetour.Statut.ANNULE):
            with self.subTest(statut=statut):
                DemandeRetour.objects.filter(pk=self.demande.pk).update(statut=statut)
                self.assertEqual(self.ajouter().status_code, 400)

    def test_cinq_photos_au_plus(self):
        for _ in range(5):
            self.assertEqual(self.ajouter().status_code, 201)
        self.assertEqual(self.ajouter().status_code, 400)
        self.assertEqual(PhotoRetour.objects.count(), 5)

    def test_faux_fichier_refuse(self):
        self.client.force_authenticate(user=self.client1)
        faux = SimpleUploadedFile("preuve.png", b"<?php echo 1; ?>", content_type="image/png")
        r = self.client.post(f"{URL_RETOURS}{self.demande.id}/photos/", {"image": faux}, format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_seul_le_client_ajoute_des_photos(self):
        for autre in (self.vendeur1, self.client2):
            with self.subTest(autre=autre.email):
                self.assertEqual(self.ajouter(utilisateur=autre).status_code, 404)

    def test_telechargement_reserve_aux_parties(self):
        r = self.ajouter()
        url = r.data["image"]
        self.assertIn(f"/api/retours/{self.demande.id}/photos/{r.data['id']}/", url)
        for utilisateur, attendu in ((self.client1, 200), (self.vendeur1, 200), (self.admin, 200),
                                     (self.client2, 404), (self.vendeur2, 404)):
            with self.subTest(utilisateur=utilisateur.email):
                self.client.force_authenticate(user=utilisateur)
                self.assertEqual(self.client.get(url).status_code, attendu)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_type_de_contenu_reel_documente_dans_le_schema(self):
        from config.schema import FICHIER_IMAGE

        (cle,) = FICHIER_IMAGE
        tampon = io.BytesIO()
        Image.new("RGB", (4, 4)).save(tampon, "WEBP")
        webp = SimpleUploadedFile("preuve.webp", tampon.getvalue(), content_type="image/webp")
        self.client.force_authenticate(user=self.client1)
        envois = {
            "image/png": self.ajouter(),
            "image/webp": self.client.post(f"{URL_RETOURS}{self.demande.id}/photos/", {"image": webp},
                                           format="multipart"),
        }
        for attendu, envoi in envois.items():
            with self.subTest(attendu=attendu):
                self.assertEqual(envoi.status_code, 201, envoi.data)
                reponse = self.client.get(envoi.data["image"])
                self.assertEqual(reponse["Content-Type"], attendu)
                self.assertIn(reponse["Content-Type"], cle[1:])

    def test_photo_d_une_autre_demande_introuvable(self):
        r = self.ajouter()
        autre = self.demande_au_statut(DemandeRetour.Statut.DEMANDE)
        self.client.force_authenticate(user=self.client1)
        self.assertEqual(self.client.get(f"{URL_RETOURS}{autre.id}/photos/{r.data['id']}/").status_code, 404)


class LimitesDeDebitTests(RetoursCycleBase):
    """Limites dédiées, avec les vraies valeurs de base.py."""

    def test_creation_limitee(self):
        from django.core.cache import cache
        from apps.core.tests import taux_de_production

        cache.clear()
        taux = taux_de_production()
        n = int(taux["retour_creation"].split("/")[0])
        Commande.objects.filter(pk=self.commande1.pk).update(status=Commande.Status.CONFIRMEE)
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [self.demander().status_code for _ in range(n + 1)]
            # La liste n'est pas comptée dans la limite de création.
            self.assertEqual(self.client.get(URL_RETOURS).status_code, 200)
        self.assertEqual(codes, [400] * n + [429])


class RemboursementUniqueTests(RetoursCycleBase):
    """La part du vendeur n'est déduite qu'une fois par retour."""

    def setUp(self):
        super().setUp()
        paiement = Paiement.objects.create(client=self.client1, fournisseur="simule", montant=Decimal("40000"),
                                           statut=Paiement.Statut.VALIDE, date_validation=timezone.now())
        paiement.commandes.add(self.commande1)
        self.reversement = reversements.creer_reversement(self.commande1)

    def test_double_appel_du_service_une_seule_deduction(self):
        from apps.paiements.services import rembourser_retour

        demande = self.demande_au_statut(DemandeRetour.Statut.REMBOURSE)
        rembourser_retour(demande)
        rembourser_retour(demande)
        self.reversement.refresh_from_db()
        self.assertEqual(self.reversement.montant_retours, reversements.part_vendeur_retournee(demande))
        self.assertEqual(Remboursement.objects.filter(retour=demande).count(), 1)

    def test_second_remboursement_refuse(self):
        demande = self.demande_au_statut(DemandeRetour.Statut.RECEPTIONNE)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "rembourser").status_code, 200)
        self.assertEqual(self.agir(demande.id, self.vendeur1, "rembourser").status_code, 400)


class RemboursementConcurrentTests(TransactionTestCase):
    """Même règle en concurrence réelle : deux « rembourser » simultanés ne
    répondent pas 200 tous les deux et la part du vendeur n'est déduite
    qu'une fois."""

    @skipUnless(connection.vendor == "postgresql", "select_for_update exige PostgreSQL.")
    def test_deux_remboursements_simultanes(self):
        client1 = Utilisateur.objects.create_user(email="c.rc@anitche.ci", password="TestPassword123!",
                                                  nom="C", prenom="C", role=Role.CLIENT)
        vendeur = Utilisateur.objects.create_user(email="v.rc@anitche.ci", password="TestPassword123!", nom="V",
                                                  prenom="V", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        boutique = Boutique.objects.create(proprietaire=vendeur, nom="Boutique RC", est_active=True)
        produit = Produit.objects.create(boutique=boutique, nom="P", prix_base=Decimal("20000"))
        variante = VarianteProduit.objects.create(produit=produit, nom="M", prix=Decimal("20000"))
        commande = Commande.objects.create(boutique=boutique, client=client1, montant_total=Decimal("40000"),
                                           status=Commande.Status.LIVREE)
        article = CommandeItem.objects.create(commande=commande, variante=variante, nom_produit="P",
                                              prix_unitaire=Decimal("20000"), quantite=2,
                                              montant_commission=Decimal("4000"), montant_frais_fixes=Decimal("200"))
        paiement = Paiement.objects.create(client=client1, fournisseur="simule", montant=Decimal("40000"),
                                           statut=Paiement.Statut.VALIDE, date_validation=timezone.now())
        paiement.commandes.add(commande)
        reversements.creer_reversement(commande)
        demande = DemandeRetour.objects.create(commande=commande, client=client1, boutique=boutique,
                                               description="Défaut constaté", montant_remboursement=Decimal("20000"),
                                               statut=DemandeRetour.Statut.RECEPTIONNE)
        RetourItem.objects.create(demande_retour=demande, commande_item=article, quantite=1)

        barriere = threading.Barrier(2)
        codes = []

        def appel():
            try:
                api = APIClient()
                api.force_authenticate(vendeur)
                barriere.wait()
                codes.append(api.patch(f"{URL_RETOURS}{demande.id}/traiter/", {"action": "rembourser"},
                                       format="json").status_code)
            finally:
                connection.close()

        fils = [threading.Thread(target=appel) for _ in range(2)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()

        self.assertEqual(sorted(codes), [200, 400])
        self.assertEqual(Reversement.objects.get(commande=commande).montant_retours,
                         reversements.part_vendeur_retournee(demande))
        self.assertEqual(Remboursement.objects.filter(retour=demande).count(), 1)
