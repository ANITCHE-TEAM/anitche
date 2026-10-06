from decimal import Decimal
from unittest.mock import patch
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from rest_framework.throttling import ScopedRateThrottle

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.commandes.models import Commande
from apps.paiements.models import Paiement
from apps.paiements.services import valider_paiement
from apps.retours.models import DemandeRetour
from apps.retours.signals import retour_status_change
from .models import CompteFidelite, TransactionFidelite, CouponReduction


class BaseFideliteTestCase(APITestCase):
    def setUp(self):
        # Client 1
        self.client1 = Utilisateur.objects.create_user(
            email="client1@anitche.ci",
            password="TestPassword123!",
            nom="Konan",
            prenom="Aya",
            role=Role.CLIENT,
            statut_kyc=StatutKYC.VALIDE,
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
        self.vendeur = Utilisateur.objects.create_user(
            email="vendeur@anitche.ci",
            password="TestPassword123!",
            nom="Kouassi",
            prenom="Jean",
            role=Role.VENDEUR,
            statut_kyc=StatutKYC.VALIDE,
        )
        self.boutique = Boutique.objects.create(
            proprietaire=self.vendeur,
            nom="Boutique Artisanat",
            est_active=True,
        )

        # Commande pour client 1
        self.commande = Commande.objects.create(
            boutique=self.boutique,
            client=self.client1,
            montant_total=Decimal("25000.00"),
            status=Commande.Status.CREEE,
        )


class FideliteAPITestCase(BaseFideliteTestCase):

    def test_paliers_fidelite_evolution(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)

        compte.crediter_points(600)
        self.assertEqual(compte.palier, CompteFidelite.Palier.ARGENT)

        compte.crediter_points(1500)  # Total 2100
        self.assertEqual(compte.palier, CompteFidelite.Palier.OR)

        compte.crediter_points(3000)  # Total 5100
        self.assertEqual(compte.palier, CompteFidelite.Palier.PLATINE)

    def test_mon_compte_fidelite_api(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(150)

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-mon-compte")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["solde_points"], 150)
        self.assertEqual(response.data["palier"], "bronze")

    def test_convertir_points_en_coupon_succes(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(200)

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-convertir")
        data = {"option": "100_PTS_10PCT"}

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["code"].startswith("FID-"))
        self.assertEqual(Decimal(str(response.data["valeur"])), Decimal("10.00"))
        self.assertEqual(response.data["type_reduction"], "pourcentage")

        # Vérification du solde restant : 200 - 100 = 100 points
        compte.refresh_from_db()
        self.assertEqual(compte.solde_points, 100)

    def test_convertir_points_solde_insuffisant(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(30)  # Moins que les 50 requis

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-convertir")
        data = {"option": "50_PTS_5PCT"}

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verifier_coupon_reduction_pourcentage(self):
        coupon = CouponReduction.objects.create(
            code="PROMO10",
            client=self.client1,
            type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("10.00"),
            montant_minimum_commande=Decimal("5000.00"),
        )

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {
            "code": "PROMO10",
            "montant_commande": "20000.00",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["valide"])
        # 10% sur 20 000 FCFA = 2 000 FCFA de remise
        self.assertEqual(Decimal(response.data["remise"]), Decimal("2000.00"))
        self.assertEqual(Decimal(response.data["montant_final"]), Decimal("18000.00"))

    def test_rejet_coupon_montant_minimum(self):
        CouponReduction.objects.create(
            code="MIN15000",
            client=self.client1,
            type_reduction=CouponReduction.TypeReduction.MONTANT_FIXE,
            valeur=Decimal("2000.00"),
            montant_minimum_commande=Decimal("15000.00"),
        )

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {
            "code": "MIN15000",
            "montant_commande": "8000.00",  # Inférieur au minimum
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Format d'erreur commun : le motif est rattaché au champ `code`.
        self.assertFalse(response.data["success"])
        self.assertIn("minimum", response.data["errors"]["code"][0])

    def test_rejet_coupon_autre_client(self):
        CouponReduction.objects.create(
            code="NOMINATIF_AYA",
            client=self.client1,
            type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("15.00"),
        )

        # Client 2 tente d'utiliser le coupon de Client 1
        self.client.force_authenticate(user=self.client2)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {
            "code": "NOMINATIF_AYA",
            "montant_commande": "20000.00",
        }

        # Même réponse qu'un code inexistant (404), sans révéler le coupon.
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        inexistant = self.client.post(url, {**data, "code": "INEXISTANT"}, format="json")
        self.assertEqual(inexistant.status_code, status.HTTP_404_NOT_FOUND)
        # Même forme qu'un code inexistant : seul le code saisi diffère.
        self.assertEqual(
            response.data["detail"].replace("NOMINATIF_AYA", "X"),
            inexistant.data["detail"].replace("INEXISTANT", "X"),
        )
        self.assertEqual(response.data["errors"], {})


class FideliteThrottleTestCase(BaseFideliteTestCase):
    """Sécurité (A04:2025) : vérifie que les throttle_scope dédiés de
    VerifierCouponView et ConvertirPointsEnCouponView sont réellement
    appliqués par DRF (pas seulement présents comme attribut mort) —
    en abaissant temporairement leur taux, la N+1-ième requête dans la
    fenêtre doit être rejetée en 429.

    override_settings(REST_FRAMEWORK=...) ne suffit pas ici. DRF fige
    ScopedRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
    comme attribut de CLASSE au moment de l'import de
    rest_framework.throttling (une seule fois par process de test) ; ce
    n'est pas une propriété relue à chaque requête. override_settings
    substitue un autre dict à settings.REST_FRAMEWORK, mais l'attribut de
    classe déjà résolu pointe toujours vers le dict d'origine (celui de
    config/settings/test.py, qui met tous les taux à 100000/jour pour
    éviter les 429 parasites ailleurs dans la suite) : la requête
    passerait en 200/201 au lieu du 429 attendu. Le test patche donc
    directement ce dict de classe pour la durée du test.

    Second point d'isolation nécessaire : le cache de throttling
    (LocMemCache en test, voir config/settings/test.py) n'est jamais
    vidé entre deux tests par Django. Si un AUTRE test a déjà sollicité
    la même vue sous le même scope pour une clé identique (même pk
    d'utilisateur, ce qui arrive quand la base réutilise les PK après le
    rollback, comme SQLite), son historique de requêtes pollue le
    compteur ici. On repart donc d'un cache vide à chaque test de cette
    classe.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_verifier_coupon_est_bien_throttle(self):
        CouponReduction.objects.create(
            code="THROTTLETEST",
            type_reduction=CouponReduction.TypeReduction.POURCENTAGE,
            valeur=Decimal("5.00"),
        )
        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-verifier-coupon")
        data = {"code": "THROTTLETEST", "montant_commande": "10000.00"}

        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"coupon_verification": "2/min"}):
            # Les 2 premières passent (taux abaissé à 2/min pour ce test).
            for _ in range(2):
                response = self.client.post(url, data, format="json")
                self.assertEqual(response.status_code, status.HTTP_200_OK)

            # La 3e requête dans la même fenêtre doit être bloquée.
            response = self.client.post(url, data, format="json")
            self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_convertir_points_est_bien_throttle(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(1000)

        self.client.force_authenticate(user=self.client1)
        url = reverse("fidelite:fidelite-convertir")
        data = {"option": "50_PTS_5PCT"}

        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"fidelite_conversion": "1/min"}):
            premiere = self.client.post(url, data, format="json")
            self.assertEqual(premiere.status_code, status.HTTP_201_CREATED)

            deuxieme = self.client.post(url, data, format="json")
            self.assertEqual(deuxieme.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


# =====================================================================
# Gains en attente, retours, coupons et administration
# (docs/MODULE_FIDELITE.md, § Sécurité).
# =====================================================================

from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError, transaction as transaction_db
from django.test import RequestFactory
from django.utils import timezone

from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import CommandeItem, GroupeCommande
from apps.commandes.services import annuler_commande, synchroniser_depuis_livraison
from apps.livraison.models import ContestationLivraison, Livraison
from apps.paiements import reversements
from .models import GainFidelite, UtilisationCoupon
from . import services
from .tasks import crediter_points_echus


class GainsBase(BaseFideliteTestCase):
    """Commande de 25 000 FCFA payée (25 points à terme)."""

    def setUp(self):
        super().setUp()
        produit = Produit.objects.create(boutique=self.boutique, nom="Panier tressé", prix_base=Decimal("12500"))
        self.variante = VarianteProduit.objects.create(produit=produit, nom="Grand", prix=Decimal("12500"))
        self.article = CommandeItem.objects.create(
            commande=self.commande, variante=self.variante, nom_produit="Panier tressé",
            prix_unitaire=Decimal("12500"), quantite=2,
        )
        self.paiement = Paiement.objects.create(
            client=self.client1, montant=Decimal("25000"), fournisseur="simule",
            methode=Paiement.Methode.WAVE, adresse_livraison="Cocody",
        )
        self.paiement.commandes.add(self.commande)
        valider_paiement(self.paiement)
        self.commande.refresh_from_db()

    def livrer(self):
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.EXPEDIEE)
        synchroniser_depuis_livraison(self.commande, "livree")
        Livraison.objects.filter(commande=self.commande).update(status=Livraison.Status.LIVREE, date_livraison=timezone.now())
        return GainFidelite.objects.get(commande=self.commande)

    def solde(self):
        compte = CompteFidelite.objects.filter(utilisateur=self.client1).first()
        return compte.solde_points if compte else 0

    def plus_tard(self, jours=8):
        return timezone.now() + timedelta(days=jours)

    def retour_rembourse(self, montant, quantite=1):
        demande = DemandeRetour.objects.create(
            commande=self.commande, client=self.client1, boutique=self.boutique, description="Défaut constaté",
            montant_remboursement=Decimal(montant), statut=DemandeRetour.Statut.REMBOURSE,
        )
        retour_status_change.send(sender=DemandeRetour, demande_retour=demande,
                                  ancien_statut=DemandeRetour.Statut.RECEPTIONNE,
                                  nouveau_statut=DemandeRetour.Statut.REMBOURSE, action="rembourser")
        return demande


class PointsEnAttenteTests(GainsBase):
    """Aucun point au paiement ; « en attente » à la
    livraison, crédités à la fin du délai de rétractation."""

    def test_aucun_point_au_paiement(self):
        self.assertEqual(self.solde(), 0)
        self.assertFalse(GainFidelite.objects.exists())

    def test_annulation_apres_paiement_ne_laisse_aucun_point(self):
        annuler_commande(self.commande, Commande.MotifAnnulation.CLIENT, acteur=self.client1)
        self.assertEqual(self.solde(), 0)
        self.assertEqual(services.crediter_gains_echus(self.plus_tard(30)), 0)
        self.assertFalse(GainFidelite.objects.exists())

    def test_livraison_ouvre_un_gain_en_attente_visible_du_client(self):
        gain = self.livrer()
        self.assertEqual((gain.points, gain.statut), (25, GainFidelite.Statut.EN_ATTENTE))
        self.assertEqual(self.solde(), 0)
        self.client.force_authenticate(user=self.client1)
        compte = self.client.get(reverse("fidelite:fidelite-mon-compte")).data
        self.assertEqual((compte["solde_points"], compte["points_en_attente"]), (0, 25))
        liste = self.client.get(reverse("fidelite:fidelite-gains")).data["results"]
        self.assertEqual((liste[0]["numero_commande"], liste[0]["statut"]), (self.commande.numero_commande, "en_attente"))

    def test_credit_a_la_fin_du_delai_une_seule_fois(self):
        self.livrer()
        self.assertEqual(services.crediter_gains_echus(), 0)  # délai pas écoulé
        self.assertEqual(services.crediter_gains_echus(self.plus_tard()), 1)
        self.assertEqual(services.crediter_gains_echus(self.plus_tard()), 0)
        self.assertEqual(self.solde(), 25)
        self.assertEqual(TransactionFidelite.objects.filter(type_transaction="gain").count(), 1)

    def test_tache_periodique(self):
        gain = self.livrer()
        GainFidelite.objects.filter(pk=gain.pk).update(date_disponibilite=timezone.now() - timedelta(minutes=1))
        self.assertIn("1 gain", crediter_points_echus())
        self.assertEqual(self.solde(), 25)

    def test_livraison_rejouee_un_seul_gain(self):
        self.livrer()
        self.assertIsNone(services.ouvrir_gain(self.commande))
        self.assertEqual(GainFidelite.objects.count(), 1)

    def test_commande_deja_creditee_au_paiement_pas_de_second_gain(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(25, reference_externe=self.paiement.reference)
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.EXPEDIEE)
        synchroniser_depuis_livraison(self.commande, "livree")
        self.assertFalse(GainFidelite.objects.exists())

    def test_contestation_fondee_annule_les_points_en_attente(self):
        from apps.livraison.services import resoudre_contestation

        self.livrer()
        admin = Utilisateur.objects.create_user(email="admin.fid@anitche.ci", password="TestPassword123!",
                                                nom="A", prenom="D", role=Role.ADMIN)
        livraison = Livraison.objects.get(commande=self.commande)
        ContestationLivraison.objects.create(livraison=livraison, motif="Colis jamais reçu")
        self.assertEqual(services.crediter_gains_echus(self.plus_tard()), 0)  # contestation ouverte : attente
        resoudre_contestation(livraison, admin, ContestationLivraison.Statut.FONDEE)
        self.assertEqual(GainFidelite.objects.get().statut, GainFidelite.Statut.ANNULE)
        self.assertEqual(services.crediter_gains_echus(self.plus_tard()), 0)
        self.assertEqual(self.solde(), 0)

    def test_retour_ouvert_retarde_le_credit(self):
        self.livrer()
        DemandeRetour.objects.create(commande=self.commande, client=self.client1, boutique=self.boutique,
                                     description="Défaut constaté", montant_remboursement=Decimal("12500"))
        self.assertEqual(services.crediter_gains_echus(self.plus_tard()), 0)


class RetourEtPointsTests(GainsBase):
    """Retour pendant l'attente → points recalculés ; après le
    crédit → reprise plafonnée, une seule fois par retour."""

    def test_retour_partiel_pendant_l_attente(self):
        self.livrer()
        self.retour_rembourse("12500")
        self.assertEqual(GainFidelite.objects.get().points, 12)
        services.crediter_gains_echus(self.plus_tard())
        self.assertEqual(self.solde(), 12)

    def test_retour_total_pendant_l_attente_annule_le_gain(self):
        self.livrer()
        self.retour_rembourse("25000")
        self.assertEqual(GainFidelite.objects.get().statut, GainFidelite.Statut.ANNULE)
        self.assertEqual(services.crediter_gains_echus(self.plus_tard()), 0)

    def test_signal_rejoue_pendant_l_attente_idempotent(self):
        self.livrer()
        demande = self.retour_rembourse("12500")
        services.appliquer_retour_rembourse(demande)
        self.assertEqual(GainFidelite.objects.get().points, 12)

    def test_reprise_apres_credit_une_seule_fois(self):
        self.livrer()
        services.crediter_gains_echus(self.plus_tard())
        demande = self.retour_rembourse("12500")
        services.appliquer_retour_rembourse(demande)  # rejeu
        self.assertEqual(self.solde(), 25 - 12)
        self.assertEqual(TransactionFidelite.objects.filter(type_transaction="reprise").count(), 1)

    def test_reprise_plafonnee_au_solde(self):
        self.livrer()
        services.crediter_gains_echus(self.plus_tard())
        CompteFidelite.objects.get(utilisateur=self.client1).debiter_points(20, reference_externe="FID-TEST")
        self.retour_rembourse("25000")
        self.assertEqual(self.solde(), 0)

    def test_transition_autre_que_rembourse_ignoree(self):
        self.livrer()
        demande = DemandeRetour.objects.create(commande=self.commande, client=self.client1, boutique=self.boutique,
                                               description="x", montant_remboursement=Decimal("25000"),
                                               statut=DemandeRetour.Statut.APPROUVE)
        retour_status_change.send(sender=DemandeRetour, demande_retour=demande, ancien_statut="demande",
                                  nouveau_statut="approuve", action="approuver")
        self.assertEqual(GainFidelite.objects.get().points, 25)

    def test_idempotence_en_base(self):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(5, reference_externe="CMD-X")
        with self.assertRaises(IntegrityError), transaction_db.atomic():
            compte.crediter_points(5, reference_externe="CMD-X")


class CouponsTests(BaseFideliteTestCase):
    """Utilisation, restitution, valeurs, confidentialité."""

    def setUp(self):
        super().setUp()
        self.groupe = GroupeCommande.objects.create(client=self.client1)
        Commande.objects.filter(pk=self.commande.pk).update(groupe=self.groupe)
        self.commande.refresh_from_db()

    def coupon(self, **champs):
        valeurs = {"code": "PROMO10", "valeur": Decimal("10"), **champs}
        return CouponReduction.objects.create(**valeurs)

    def test_coupon_public_utilisable_par_chaque_client_une_fois(self):
        coupon = self.coupon(utilisations_max=None)
        services.consommer_coupon(coupon, self.client1, self.groupe)
        coupon.refresh_from_db()
        self.assertTrue(coupon.est_valide_pour(self.client2, Decimal("50000"))[0])
        valide, message = coupon.est_valide_pour(self.client1, Decimal("50000"))
        self.assertFalse(valide)
        self.assertIn("déjà utilisé", message)

    def test_limite_globale(self):
        coupon = self.coupon(utilisations_max=1)
        services.consommer_coupon(coupon, self.client1, self.groupe)
        coupon.refresh_from_db()
        self.assertTrue(coupon.est_utilise)
        self.assertFalse(coupon.est_valide_pour(self.client2, Decimal("50000"))[0])

    def test_une_utilisation_par_client_en_base(self):
        coupon = self.coupon(utilisations_max=None)
        UtilisationCoupon.objects.create(coupon=coupon, client=self.client1)
        with self.assertRaises(IntegrityError), transaction_db.atomic():
            UtilisationCoupon.objects.create(coupon=coupon, client=self.client1)

    def test_coupon_rendu_si_tout_le_checkout_est_annule(self):
        coupon = self.coupon(client=self.client1)
        services.consommer_coupon(coupon, self.client1, self.groupe)
        Commande.objects.filter(pk=self.commande.pk).update(coupon_code=coupon.code)
        self.commande.refresh_from_db()
        annuler_commande(self.commande, Commande.MotifAnnulation.EXPIRATION)
        coupon.refresh_from_db()
        self.assertEqual((coupon.est_utilise, coupon.nombre_utilisations), (False, 0))
        self.assertTrue(coupon.est_valide_pour(self.client1, Decimal("50000"))[0])

    def test_coupon_garde_si_une_commande_du_checkout_reste(self):
        coupon = self.coupon(client=self.client1)
        services.consommer_coupon(coupon, self.client1, self.groupe)
        Commande.objects.filter(groupe=self.groupe).update(coupon_code=coupon.code)
        autre = Commande.objects.create(boutique=self.boutique, client=self.client1, groupe=self.groupe,
                                        montant_total=Decimal("1000"), coupon_code=coupon.code)
        self.commande.refresh_from_db()
        annuler_commande(self.commande, Commande.MotifAnnulation.EXPIRATION)
        coupon.refresh_from_db()
        self.assertTrue(coupon.est_utilise)
        annuler_commande(autre, Commande.MotifAnnulation.EXPIRATION)
        coupon.refresh_from_db()
        self.assertFalse(coupon.est_utilise)

    def test_coupon_expire_non_rendu(self):
        coupon = self.coupon(client=self.client1, date_expiration=timezone.now() - timedelta(days=1))
        UtilisationCoupon.objects.create(coupon=coupon, client=self.client1, groupe=self.groupe)
        CouponReduction.objects.filter(pk=coupon.pk).update(est_utilise=True, nombre_utilisations=1)
        Commande.objects.filter(pk=self.commande.pk).update(coupon_code=coupon.code)
        self.commande.refresh_from_db()
        self.assertFalse(services.restituer_coupon(self.commande))

    def test_valeurs_impossibles_refusees_en_base(self):
        for champs in ({"valeur": Decimal("150")}, {"valeur": Decimal("0")},
                       {"type_reduction": "montant_fixe", "valeur": Decimal("-500")},
                       {"montant_minimum_commande": Decimal("-1")}):
            with self.subTest(champs=champs), self.assertRaises(IntegrityError), transaction_db.atomic():
                self.coupon(**champs)

    def test_remise_jamais_superieure_au_montant(self):
        coupon = self.coupon(type_reduction="montant_fixe", valeur=Decimal("20000"))
        self.assertEqual(coupon.calculer_remise(Decimal("5000")), Decimal("5000"))

    def test_coupon_nominatif_d_autrui_indiscernable_d_un_code_inexistant(self):
        self.coupon(code="FID-AYA00001", client=self.client1)
        self.client.force_authenticate(user=self.client2)
        url = reverse("fidelite:fidelite-verifier-coupon")
        reponse = self.client.post(url, {"code": "FID-AYA00001", "montant_commande": "20000"}, format="json")
        inexistant = self.client.post(url, {"code": "FID-ZZZ00000", "montant_commande": "20000"}, format="json")
        self.assertEqual((reponse.status_code, inexistant.status_code), (404, 404))
        self.assertNotIn("nominatif", str(reponse.data))


class AdminFideliteTests(BaseFideliteTestCase):
    """Aucun point ni journal fabriqué à la main dans l'admin."""

    def test_solde_et_journal_en_lecture_seule(self):
        from .admin import CompteFideliteAdmin, TransactionFideliteAdmin

        requete = RequestFactory().get("/")
        requete.user = Utilisateur.objects.create_superuser(email="su@anitche.ci", password="TestPassword123!",
                                                            nom="S", prenom="U")
        self.assertIn("solde_points", CompteFideliteAdmin(CompteFidelite, AdminSite()).readonly_fields)
        journal = TransactionFideliteAdmin(TransactionFidelite, AdminSite())
        self.assertFalse(journal.has_add_permission(requete))
        self.assertFalse(journal.has_change_permission(requete))
        self.assertFalse(journal.has_delete_permission(requete))
