from decimal import Decimal
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from apps.utilisateurs.models import Utilisateur, Role, StatutKYC
from apps.vendeurs.models import Boutique
from apps.commandes.models import Commande
from .models import Livraison, LivraisonHistorique


class LivraisonTestCase(APITestCase):

    def setUp(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.autre_client = self._create_user("autre@test.com", Role.CLIENT)
        self.livreur = self._create_user("livreur@test.com", Role.LIVREUR)
        self.autre_livreur = self._create_user("autrelivreur@test.com", Role.LIVREUR)
        self.admin = self._create_user("admin@test.com", Role.ADMIN)

        self.vendeur = self._create_user("vendeur@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique = Boutique.objects.create(proprietaire=self.vendeur, nom="Boutique Test", est_active=True)

        self.commande = Commande.objects.create(
            boutique=self.boutique,
            client=self.client_user,
            montant_total=Decimal("5000"),
        )

        self.livraison = Livraison.objects.create(
            commande=self.commande,
            livreur=self.livreur,
            adresse_livraison="Cocody, Abidjan",
        )

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS):
        return Utilisateur.objects.create_user(
            email=email, password="testpass123", nom="Test", prenom="User",
            role=role, statut_kyc=statut_kyc,
        )

    # ---------- Liste des livraisons (filtrage par rôle) ----------

    def test_client_ne_voit_que_ses_propres_livraisons(self):
        autre_commande = Commande.objects.create(
            boutique=self.boutique, client=self.autre_client, montant_total=Decimal("1000")
        )
        Livraison.objects.create(commande=autre_commande, adresse_livraison="Yopougon, Abidjan")

        self.client.force_authenticate(user=self.client_user)
        response = self.client.get(reverse("livraison:livraison-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(self.livraison.id))

    def test_livreur_ne_voit_que_les_livraisons_assignees(self):
        autre_commande = Commande.objects.create(
            boutique=self.boutique, client=self.client_user, montant_total=Decimal("1000")
        )
        Livraison.objects.create(
            commande=autre_commande, livreur=self.autre_livreur, adresse_livraison="Marcory, Abidjan"
        )

        self.client.force_authenticate(user=self.livreur)
        response = self.client.get(reverse("livraison:livraison-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(self.livraison.id))

    def test_admin_voit_toutes_les_livraisons(self):
        autre_commande = Commande.objects.create(
            boutique=self.boutique, client=self.autre_client, montant_total=Decimal("1000")
        )
        Livraison.objects.create(commande=autre_commande, adresse_livraison="Yopougon, Abidjan")

        self.client.force_authenticate(user=self.admin)
        response = self.client.get(reverse("livraison:livraison-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_liste_livraisons_non_authentifie_refuse(self):
        response = self.client.get(reverse("livraison:livraison-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ---------- Détail d'une livraison ----------

    def test_detail_livraison(self):
        self.client.force_authenticate(user=self.client_user)
        response = self.client.get(reverse("livraison:livraison-detail", args=[self.livraison.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], Livraison.Status.EN_ATTENTE)
        self.assertEqual(response.data["adresse_livraison"], "Cocody, Abidjan")

    # ---------- Changement de statut ----------

    def test_livreur_assigne_peut_changer_le_statut(self):
        self.client.force_authenticate(user=self.livreur)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])
        response = self.client.patch(url, {"status": Livraison.Status.EXPEDIEE})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Livraison.Status.EXPEDIEE)
        self.assertIsNotNone(self.livraison.date_expedition)

    def test_admin_peut_changer_le_statut(self):
        self.client.force_authenticate(user=self.admin)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])
        response = self.client.patch(url, {"status": Livraison.Status.LIVREE})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Livraison.Status.LIVREE)
        self.assertIsNotNone(self.livraison.date_livraison)

    def test_livreur_non_assigne_ne_peut_pas_changer_le_statut(self):
        self.client.force_authenticate(user=self.autre_livreur)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])
        response = self.client.patch(url, {"status": Livraison.Status.EXPEDIEE})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Livraison.Status.EN_ATTENTE)

    def test_client_ne_peut_pas_changer_le_statut(self):
        self.client.force_authenticate(user=self.client_user)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])
        response = self.client.patch(url, {"status": Livraison.Status.EXPEDIEE})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_changement_statut_invalide_rejete(self):
        self.client.force_authenticate(user=self.livreur)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])
        response = self.client.patch(url, {"status": "statut_inexistant"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_changement_statut_cree_un_historique(self):
        self.client.force_authenticate(user=self.livreur)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])
        self.client.patch(url, {"status": Livraison.Status.EXPEDIEE, "commentaire": "Colis récupéré"})

        historique = LivraisonHistorique.objects.filter(livraison=self.livraison).first()
        self.assertIsNotNone(historique)
        self.assertEqual(historique.ancien_status, Livraison.Status.EN_ATTENTE)
        self.assertEqual(historique.nouveau_status, Livraison.Status.EXPEDIEE)
        self.assertEqual(historique.effectue_par, self.livreur)
        self.assertEqual(historique.commentaire, "Colis récupéré")

    def test_date_expedition_definie_une_seule_fois(self):
        self.client.force_authenticate(user=self.livreur)
        url = reverse("livraison:livraison-changer-status", args=[self.livraison.id])

        self.client.patch(url, {"status": Livraison.Status.EXPEDIEE})
        self.livraison.refresh_from_db()
        premiere_date = self.livraison.date_expedition

        self.client.patch(url, {"status": Livraison.Status.EN_COURS})
        self.livraison.refresh_from_db()

        self.assertEqual(self.livraison.date_expedition, premiere_date)

    # ---------- Historique ----------

    def test_liste_historique_livraison(self):
        self.livraison.changer_status(Livraison.Status.EXPEDIEE, effectue_par=self.livreur)
        self.livraison.changer_status(Livraison.Status.LIVREE, effectue_par=self.livreur)

        self.client.force_authenticate(user=self.client_user)
        url = reverse("livraison:livraison-historique", args=[self.livraison.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    # ---------- Modèle : signal ----------

    def test_changer_status_declenche_le_signal(self):
        from .signals import livraison_status_change

        signaux_recus = []

        def handler(sender, **kwargs):
            signaux_recus.append(kwargs)

        livraison_status_change.connect(handler)
        try:
            self.livraison.changer_status(Livraison.Status.EXPEDIEE, effectue_par=self.livreur)
        finally:
            livraison_status_change.disconnect(handler)

        self.assertEqual(len(signaux_recus), 1)
        self.assertEqual(signaux_recus[0]["nouveau_status"], Livraison.Status.EXPEDIEE)
        self.assertEqual(signaux_recus[0]["ancien_status"], Livraison.Status.EN_ATTENTE)
        self.assertEqual(signaux_recus[0]["effectue_par"], self.livreur)