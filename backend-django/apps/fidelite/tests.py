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

    def test_gain_points_sur_validation_paiement(self):
        paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande,
            montant=Decimal("25000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Cocody, Abidjan",
        )

        # Validation du paiement -> émet le signal paiement_valide
        paiement.valider(transaction_id_externe="wave_trx_888")

        compte = CompteFidelite.objects.get(utilisateur=self.client1)
        # 25 000 FCFA // 1000 = 25 points
        self.assertEqual(compte.solde_points, 25)
        self.assertEqual(compte.points_cumules_total, 25)
        self.assertEqual(compte.palier, CompteFidelite.Palier.BRONZE)

        # Vérification de la transaction enregistrée
        transaction = TransactionFidelite.objects.get(compte=compte)
        self.assertEqual(transaction.points, 25)
        self.assertEqual(transaction.type_transaction, TransactionFidelite.TypeTransaction.GAIN)

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
        self.assertFalse(response.data["valide"])

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

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["valide"])


class FideliteThrottleTestCase(BaseFideliteTestCase):
    """Sécurité (A04:2025) : vérifie que les throttle_scope dédiés de
    VerifierCouponView et ConvertirPointsEnCouponView sont réellement
    appliqués par DRF (pas seulement présents comme attribut mort) —
    en abaissant temporairement leur taux, la N+1-ième requête dans la
    fenêtre doit être rejetée en 429.

    DIAGNOSTIC (test préexistant, corrigé sans toucher au module) :
    override_settings(REST_FRAMEWORK=...) ne suffit pas ici. DRF fige
    ScopedRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES
    comme attribut de CLASSE au moment de l'import de
    rest_framework.throttling (une seule fois par process de test) ; ce
    n'est pas une propriété relue à chaque requête. override_settings
    remplace l'objet settings.REST_FRAMEWORK par un nouveau dict, mais
    l'attribut de classe déjà résolu continue de pointer vers l'ancien
    objet (celui de config/settings/test.py, qui met tous les taux à
    100000/jour pour éviter les 429 parasites ailleurs dans la suite) —
    d'où le 200/201 observé au lieu du 429 attendu. Le fix correct est
    de patcher directement ce dict de classe pour la durée du test.

    Second point d'isolation nécessaire : le cache de throttling
    (LocMemCache en test, voir config/settings/test.py) n'est jamais
    vidé entre deux tests par Django. Comme les PK SQLite sont réutilisées
    après le rollback de transaction propre à chaque test (client1 est
    quasi systématiquement pk=1), l'historique de requêtes laissé par un
    AUTRE test ayant déjà sollicité la même vue sous le même scope pour
    ce même pk (au taux normal, non abaissé) pollue le compteur ici. On
    repart donc d'un cache vide à chaque test de cette classe.
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


class FideliteRemboursementTestCase(BaseFideliteTestCase):
    """Sécurité (A04:2025 — Insecure Design) : un remboursement de retour
    doit reprendre les points de fidélité gagnés sur la part remboursée,
    sinon un client peut accumuler des points sans risque financier réel
    en achetant puis en se faisant systématiquement rembourser."""

    def _creer_retour_rembourse(self, montant_remboursement, client=None):
        """Simule directement l'émission du signal de remboursement, sans
        repasser par toute la machine à états de TraiterDemandeRetourView :
        c'est le signal lui-même (et son écouteur côté fidelite) qui est
        sous test ici, pas le flux retours dans son ensemble (déjà testé
        dans apps.retours.tests)."""
        demande = DemandeRetour.objects.create(
            commande=self.commande,
            client=client or self.client1,
            boutique=self.boutique,
            description="Produit défectueux, remboursement demandé.",
            montant_remboursement=montant_remboursement,
            statut=DemandeRetour.Statut.RECEPTIONNE,
        )
        demande.statut = DemandeRetour.Statut.REMBOURSE
        demande.save(update_fields=["statut"])

        retour_status_change.send(
            sender=DemandeRetour,
            demande_retour=demande,
            ancien_statut=DemandeRetour.Statut.RECEPTIONNE,
            nouveau_statut=DemandeRetour.Statut.REMBOURSE,
        )
        return demande

    def test_reprise_integrale_des_points_gagnes_sur_achat_rembourse(self):
        """25 000 FCFA payés puis intégralement remboursés -> les 25 points
        gagnés au paiement doivent être intégralement repris."""
        paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande,
            montant=Decimal("25000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Cocody, Abidjan",
        )
        paiement.valider(transaction_id_externe="wave_trx_remb_1")

        compte = CompteFidelite.objects.get(utilisateur=self.client1)
        self.assertEqual(compte.solde_points, 25)

        demande = self._creer_retour_rembourse(Decimal("25000.00"))

        compte.refresh_from_db()
        self.assertEqual(compte.solde_points, 0)

        reprise = TransactionFidelite.objects.filter(
            compte=compte, reference_externe=demande.numero_retour
        ).first()
        self.assertIsNotNone(reprise)
        self.assertEqual(reprise.points, -25)
        self.assertEqual(reprise.type_transaction, TransactionFidelite.TypeTransaction.AJUSTEMENT_ADMIN)

    def test_reprise_plafonnee_si_points_deja_depenses(self):
        """Si le client a déjà converti ses points en coupon avant le
        remboursement, la reprise doit se limiter au solde encore
        disponible (jamais lever d'erreur ni faire échouer le retour)."""
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=self.client1)
        compte.crediter_points(25, description="Gain simulé")
        # Le client dépense la totalité de son solde avant le remboursement.
        compte.debiter_points(25, description="Conversion en coupon")
        self.assertEqual(compte.solde_points, 0)

        demande = self._creer_retour_rembourse(Decimal("25000.00"))

        compte.refresh_from_db()
        # Rien à reprendre (solde déjà à 0) : ne doit pas passer en négatif,
        # et ne doit pas avoir levé d'exception dans le récepteur du signal.
        self.assertEqual(compte.solde_points, 0)

    def test_pas_de_reprise_si_client_absent(self):
        """Un GroupeCommande sans client (SET_NULL) ne doit jamais faire
        planter le récepteur du signal."""
        demande = DemandeRetour.objects.create(
            commande=self.commande,
            client=self.client1,
            boutique=self.boutique,
            description="Test",
            montant_remboursement=Decimal("25000.00"),
            statut=DemandeRetour.Statut.RECEPTIONNE,
        )
        demande.client = None
        # Émission directe du signal avec un objet dont client a été mis à
        # None en mémoire (sans toucher la FK réelle en base, non-nullable
        # ici) : vérifie uniquement que le garde-fou `if not client: return`
        # est bien atteint sans lever d'exception.
        retour_status_change.send(
            sender=DemandeRetour,
            demande_retour=demande,
            ancien_statut=DemandeRetour.Statut.RECEPTIONNE,
            nouveau_statut=DemandeRetour.Statut.REMBOURSE,
        )

    def test_pas_de_reprise_sur_transition_non_remboursement(self):
        """Une transition vers un autre statut (ex: 'approuve') ne doit
        jamais déclencher de reprise de points."""
        paiement = Paiement.objects.create(
            client=self.client1,
            commande=self.commande,
            montant=Decimal("25000.00"),
            methode=Paiement.Methode.WAVE,
            adresse_livraison="Cocody, Abidjan",
        )
        paiement.valider(transaction_id_externe="wave_trx_remb_2")
        compte = CompteFidelite.objects.get(utilisateur=self.client1)
        self.assertEqual(compte.solde_points, 25)

        demande = DemandeRetour.objects.create(
            commande=self.commande,
            client=self.client1,
            boutique=self.boutique,
            description="Test",
            montant_remboursement=Decimal("25000.00"),
            statut=DemandeRetour.Statut.APPROUVE,
        )
        retour_status_change.send(
            sender=DemandeRetour,
            demande_retour=demande,
            ancien_statut=DemandeRetour.Statut.DEMANDE,
            nouveau_statut=DemandeRetour.Statut.APPROUVE,
        )

        compte.refresh_from_db()
        self.assertEqual(compte.solde_points, 25)