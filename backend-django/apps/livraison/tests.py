import importlib
import threading
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle
from rest_framework_simplejwt.tokens import AccessToken

from apps.commandes.models import Commande, GroupeCommande
from apps.commandes.services import annuler_commande
from apps.notifications.models import Notification
from apps.paiements import reversements
from apps.paiements.models import Paiement, Remboursement, Reversement
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique

from . import services
from .frais import TarifIntrouvable, frais_de_la_commande, tarif_applicable
from .models import ContestationLivraison, Livraison, LivraisonHistorique, TarifLivraison, normaliser_commune

Statut = Livraison.Status


def recreer_tarifs_initiaux():
    """Tarifs créés par migration (défauts de zone, communes du district
    d'Abidjan), effacés par le vidage de base d'un TransactionTestCase."""
    tarifs = importlib.import_module("apps.livraison.migrations.0004_tarifs_de_livraison").TARIFS_INITIAUX
    communes = importlib.import_module(
        "apps.livraison.migrations.0005_communes_du_district_d_abidjan").COMMUNES_DISTRICT_ABIDJAN
    for zone, montant in tarifs.items():
        TarifLivraison.objects.get_or_create(zone=zone, commune_normalisee="", defaults={"montant": montant})
    for commune in communes:
        TarifLivraison.objects.get_or_create(
            commune_normalisee=normaliser_commune(commune),
            defaults={"zone": "abidjan", "commune": commune, "montant": tarifs["abidjan"]},
        )


class DonneesLivraison:
    """Client, livreur, vendeur (boutique), administrateur ; une commande en
    préparation, payée, avec son adresse, sa fiche de livraison assignée et
    son reversement « en attente de livraison »."""

    def creer_donnees(self):
        self.client_user = self._create_user("client@test.com", Role.CLIENT)
        self.autre_client = self._create_user("autre@test.com", Role.CLIENT)
        self.livreur = self._create_user("livreur@test.com", Role.LIVREUR, telephone="0102030405")
        self.autre_livreur = self._create_user("autrelivreur@test.com", Role.LIVREUR)
        self.admin = self._create_user("admin@test.com", Role.ADMIN)

        self.vendeur = self._create_user("vendeur@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique = Boutique.objects.create(proprietaire=self.vendeur, nom="Boutique Test", est_active=True)
        self.autre_vendeur = self._create_user("vendeur2@test.com", Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.autre_boutique = Boutique.objects.create(proprietaire=self.autre_vendeur, nom="Autre", est_active=True)

        self.groupe = GroupeCommande.objects.create(
            client=self.client_user, livraison_zone="abidjan", livraison_commune="Cocody", livraison_quartier="Angré",
            livraison_point_de_repere="Face pharmacie", livraison_telephone="0700000000",
        )
        # En préparation : la livraison peut être expédiée (l'expédition et
        # la livraison se répercutent sur la commande, apps.commandes.services).
        self.commande = Commande.objects.create(
            boutique=self.boutique, client=self.client_user, groupe=self.groupe,
            montant_total=Decimal("5000"), status=Commande.Status.PREPARATION,
        )
        self.paiement = Paiement.objects.create(
            client=self.client_user, commande=self.commande, montant=Decimal("5000"),
            statut=Paiement.Statut.VALIDE, fournisseur="simule",
        )
        self.paiement.commandes.add(self.commande)
        reversements.creer_reversement(self.commande)
        self.livraison = Livraison.objects.create(
            commande=self.commande, livreur=self.livreur, adresse_livraison="Cocody, Angré — Face pharmacie",
        )

    def _create_user(self, email, role, statut_kyc=StatutKYC.NON_SOUMIS, **extra):
        return Utilisateur.objects.create_user(
            email=email, password="testpass123", nom="Test", prenom="User",
            role=role, statut_kyc=statut_kyc, **extra,
        )

    def patch(self, utilisateur, statut, **corps):
        client = APIClient()
        client.force_authenticate(utilisateur)
        return client.patch(
            reverse("livraison:livraison-changer-status", args=[self.livraison.pk]),
            {"status": statut, **corps}, format="json",
        )

    def api(self, utilisateur):
        client = APIClient()
        client.force_authenticate(utilisateur)
        return client

    def code(self):
        self.livraison.refresh_from_db()
        return self.livraison.code_chiffre

    def mettre_en_cours(self):
        self.assertEqual(self.patch(self.livreur, Statut.EXPEDIEE).status_code, 200)
        self.assertEqual(self.patch(self.livreur, Statut.EN_COURS).status_code, 200)

    def livrer(self):
        self.mettre_en_cours()
        r = self.patch(self.livreur, Statut.LIVREE, code=self.code())
        self.assertEqual(r.status_code, 200, r.data)

    def reversement(self):
        return Reversement.objects.get(commande=self.commande)

    def detail(self, utilisateur):
        return self.api(utilisateur).get(reverse("livraison:livraison-detail", args=[self.livraison.pk]))


class LivraisonTestCase(DonneesLivraison, APITestCase):

    def setUp(self):
        self.creer_donnees()

    # ---------- Liste des livraisons (filtrage par rôle) ----------

    def test_client_ne_voit_que_ses_propres_livraisons(self):
        autre_commande = Commande.objects.create(
            boutique=self.boutique, client=self.autre_client, montant_total=Decimal("1000")
        )
        Livraison.objects.create(commande=autre_commande, adresse_livraison="Yopougon, Abidjan")

        response = self.api(self.client_user).get(reverse("livraison:livraison-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.livraison.id))

    def test_livreur_ne_voit_que_les_livraisons_assignees(self):
        autre_commande = Commande.objects.create(
            boutique=self.boutique, client=self.client_user, montant_total=Decimal("1000")
        )
        Livraison.objects.create(
            commande=autre_commande, livreur=self.autre_livreur, adresse_livraison="Marcory, Abidjan"
        )

        response = self.api(self.livreur).get(reverse("livraison:livraison-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.livraison.id))

    def test_admin_voit_toutes_les_livraisons(self):
        autre_commande = Commande.objects.create(
            boutique=self.boutique, client=self.autre_client, montant_total=Decimal("1000")
        )
        Livraison.objects.create(commande=autre_commande, adresse_livraison="Yopougon, Abidjan")

        response = self.api(self.admin).get(reverse("livraison:livraison-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    def test_liste_livraisons_non_authentifie_refuse(self):
        response = self.client.get(reverse("livraison:livraison-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ---------- Détail d'une livraison ----------

    def test_detail_livraison(self):
        response = self.detail(self.client_user)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], Statut.EN_ATTENTE)
        self.assertEqual(response.data["adresse_livraison"], "Cocody, Angré — Face pharmacie")
        self.assertEqual(response.data["telephone_contact"], "0700000000")

    # ---------- Changement de statut ----------

    def test_livreur_assigne_peut_changer_le_statut(self):
        response = self.patch(self.livreur, Statut.EXPEDIEE)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EXPEDIEE)
        self.assertIsNotNone(self.livraison.date_expedition)

    def test_parcours_complet_avec_code(self):
        self.livrer()
        self.livraison.refresh_from_db()
        self.commande.refresh_from_db()
        self.assertEqual((self.livraison.status, self.commande.status), (Statut.LIVREE, "livree"))
        self.assertIsNotNone(self.livraison.date_livraison)
        self.assertEqual(self.reversement().statut, Reversement.Statut.EN_RETRACTATION)

    def test_livreur_non_assigne_ne_peut_pas_changer_le_statut(self):
        response = self.patch(self.autre_livreur, Statut.EXPEDIEE)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EN_ATTENTE)

    def test_client_ne_peut_pas_changer_le_statut(self):
        self.assertEqual(self.patch(self.client_user, Statut.EXPEDIEE).status_code, status.HTTP_403_FORBIDDEN)

    def test_changement_statut_invalide_rejete(self):
        self.assertEqual(self.patch(self.livreur, "statut_inexistant").status_code, status.HTTP_400_BAD_REQUEST)

    def test_changement_statut_cree_un_historique(self):
        self.patch(self.livreur, Statut.EXPEDIEE, commentaire="Colis récupéré")

        historique = LivraisonHistorique.objects.filter(livraison=self.livraison).first()
        self.assertIsNotNone(historique)
        self.assertEqual(historique.ancien_status, Statut.EN_ATTENTE)
        self.assertEqual(historique.nouveau_status, Statut.EXPEDIEE)
        self.assertEqual(historique.effectue_par, self.livreur)
        self.assertEqual(historique.role_acteur, "livreur")
        self.assertEqual(historique.commentaire, "Colis récupéré")

    def test_date_expedition_definie_une_seule_fois(self):
        self.patch(self.livreur, Statut.EXPEDIEE)
        self.livraison.refresh_from_db()
        premiere_date = self.livraison.date_expedition

        self.patch(self.livreur, Statut.EN_COURS)
        self.livraison.refresh_from_db()

        self.assertEqual(self.livraison.date_expedition, premiere_date)

    def test_commande_pas_prete_409_et_rien_ne_change(self):
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.CONFIRMEE)
        self.assertEqual(self.patch(self.livreur, Statut.EXPEDIEE).status_code, status.HTTP_409_CONFLICT)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EN_ATTENTE)
        self.assertFalse(self.livraison.historique.exists())

    # ---------- Historique ----------

    def test_liste_historique_livraison(self):
        self.livrer()

        response = self.api(self.client_user).get(reverse("livraison:livraison-historique", args=[self.livraison.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 3)

    def test_changer_statut_declenche_le_signal(self):
        from .signals import livraison_status_change

        signaux_recus = []

        def handler(sender, **kwargs):
            signaux_recus.append(kwargs)

        livraison_status_change.connect(handler)
        try:
            services.changer_statut(self.livraison, Statut.EXPEDIEE, self.livreur)
        finally:
            livraison_status_change.disconnect(handler)

        self.assertEqual(len(signaux_recus), 1)
        self.assertEqual(signaux_recus[0]["nouveau_status"], Statut.EXPEDIEE)
        self.assertEqual(signaux_recus[0]["ancien_status"], Statut.EN_ATTENTE)
        self.assertEqual(signaux_recus[0]["effectue_par"], self.livreur)

    # ---------- Validation des transitions de statut ----------

    def test_livreur_ne_peut_pas_sauter_une_etape(self):
        """EN_ATTENTE -> LIVREE directement doit être refusé pour un livreur."""
        self.assertEqual(self.patch(self.livreur, Statut.LIVREE).status_code, status.HTTP_400_BAD_REQUEST)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EN_ATTENTE)

    def test_livreur_ne_peut_pas_faire_regresser_le_statut(self):
        """EN_COURS -> EN_ATTENTE doit être refusé pour un livreur."""
        Livraison.objects.filter(pk=self.livraison.pk).update(status=Statut.EN_COURS)

        self.assertEqual(self.patch(self.livreur, Statut.EN_ATTENTE).status_code, status.HTTP_400_BAD_REQUEST)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EN_COURS)

    def test_status_livraison_readonly_dans_admin(self):
        from django.contrib.admin.sites import AdminSite
        from apps.livraison.admin import LivraisonAdmin

        admin_instance = LivraisonAdmin(Livraison, AdminSite())
        self.assertIn("status", admin_instance.readonly_fields)


class CodeDeLivraisonTests(DonneesLivraison, APITestCase):
    """« Livrée » (qui déclenche le reversement) exige le code donné par le
    client : le livreur seul ne peut pas la déclarer."""

    def setUp(self):
        self.creer_donnees()
        self.mettre_en_cours()

    def test_livree_sans_code_refusee(self):
        self.assertEqual(self.patch(self.livreur, Statut.LIVREE).status_code, 400)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EN_COURS)
        self.assertEqual(self.reversement().statut, Reversement.Statut.EN_ATTENTE_LIVRAISON)

    def test_mauvais_code_compte_les_essais(self):
        faux = "000000" if self.code() != "000000" else "111111"
        r = self.patch(self.livreur, Statut.LIVREE, code=faux)
        self.assertEqual(r.status_code, 400)
        self.assertIn("4 essai", r.data["detail"])
        self.livraison.refresh_from_db()
        self.assertEqual((self.livraison.status, self.livraison.code_essais), (Statut.EN_COURS, 1))

    def test_code_bloque_apres_cinq_essais(self):
        faux = "000000" if self.code() != "000000" else "111111"
        for _ in range(services.CODE_ESSAIS_MAX):
            self.patch(self.livreur, Statut.LIVREE, code=faux)
        # Même le bon code est refusé : la livraison doit passer « échouée ».
        self.assertEqual(self.patch(self.livreur, Statut.LIVREE, code=self.code()).status_code, 400)
        self.assertEqual(self.reversement().statut, Reversement.Statut.EN_ATTENTE_LIVRAISON)

    def test_code_haché_et_chiffré_jamais_en_clair(self):
        code = self.code()
        self.assertEqual(len(code), 6)
        with connection.cursor() as curseur:
            curseur.execute("SELECT code_hash, code_chiffre FROM livraison_livraison WHERE id = %s", [self.livraison.pk])
            code_hash, code_chiffre = curseur.fetchone()
        self.assertNotIn(code, code_hash)
        self.assertNotIn(code, code_chiffre)

    def test_code_visible_par_le_client_seulement_en_cours(self):
        self.assertEqual(self.detail(self.client_user).data["code_livraison"], self.code())
        self.assertNotIn("code_livraison", self.detail(self.livreur).data)
        self.assertNotIn("code_livraison", self.detail(self.admin).data)
        vendeur = self.api(self.vendeur).get(reverse("livraison:vendeur-livraison-detail", args=[self.livraison.pk]))
        self.assertNotIn("code_livraison", vendeur.data)

    def test_code_efface_apres_livraison(self):
        self.assertEqual(self.patch(self.livreur, Statut.LIVREE, code=self.code()).status_code, 200)
        self.livraison.refresh_from_db()
        self.assertEqual((self.livraison.code_hash, self.livraison.code_chiffre), ("", ""))
        self.assertIsNone(self.detail(self.client_user).data["code_livraison"])

    def test_code_envoye_par_email_au_client(self):
        mail.outbox.clear()
        Livraison.objects.filter(pk=self.livraison.pk).update(status=Statut.EXPEDIEE)
        with self.captureOnCommitCallbacks(execute=True):
            self.patch(self.livreur, Statut.EN_COURS)
        # (EXPEDIEE → EN_COURS une seconde fois : nouveau code, nouvel email)
        emails = [m for m in mail.outbox if "code de livraison" in m.subject.lower()]
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0].to, [self.client_user.email])
        self.assertIn(self.code(), emails[0].body)


class AssignationTests(DonneesLivraison, APITestCase):
    """Assignation par l'administration, à un livreur actif ; jamais au
    propriétaire de la boutique."""

    def setUp(self):
        self.creer_donnees()
        Livraison.objects.filter(pk=self.livraison.pk).update(livreur=None)

    def assigner(self, utilisateur, livreur_id, **corps):
        return self.api(utilisateur).post(
            reverse("livraison:livraison-assigner", args=[self.livraison.pk]),
            {"livreur_id": livreur_id, **corps}, format="json",
        )

    def test_admin_assigne_un_livreur_avec_date_estimee(self):
        demain = (timezone.localdate() + timedelta(days=1)).isoformat()
        r = self.assigner(self.admin, self.livreur.pk, date_livraison_estimee=demain)
        self.assertEqual(r.status_code, 200, r.data)
        self.livraison.refresh_from_db()
        self.assertEqual((self.livraison.livreur, str(self.livraison.date_livraison_estimee)), (self.livreur, demain))
        self.assertEqual(self.livraison.historique.get().role_acteur, "administration")

    def test_reassignation_avant_livree(self):
        self.assigner(self.admin, self.livreur.pk)
        self.patch(self.livreur, Statut.EXPEDIEE)
        self.assertEqual(self.assigner(self.admin, self.autre_livreur.pk).status_code, 200)
        self.assertEqual(self.patch(self.livreur, Statut.EN_COURS).status_code, 403)
        self.assertEqual(self.patch(self.autre_livreur, Statut.EN_COURS).status_code, 200)

    def test_assignation_refusee_apres_livraison(self):
        self.assigner(self.admin, self.livreur.pk)
        self.livrer()
        self.assertEqual(self.assigner(self.admin, self.autre_livreur.pk).status_code, 400)

    def test_proprietaire_de_la_boutique_refuse(self):
        Utilisateur.objects.filter(pk=self.vendeur.pk).update(role=Role.LIVREUR)
        self.assertEqual(self.assigner(self.admin, self.vendeur.pk).status_code, 400)

    def test_non_livreur_ou_inactif_refuse(self):
        self.assertEqual(self.assigner(self.admin, self.autre_client.pk).status_code, 400)
        Utilisateur.objects.filter(pk=self.autre_livreur.pk).update(is_active=False)
        self.assertEqual(self.assigner(self.admin, self.autre_livreur.pk).status_code, 400)
        self.assertEqual(self.assigner(self.admin, 999999).status_code, 400)

    def test_assignation_reservee_a_l_administration(self):
        staff = self._create_user("staff@test.com", Role.CLIENT, is_staff=True)
        for utilisateur in (self.livreur, self.vendeur, self.client_user, staff):
            self.assertEqual(self.assigner(utilisateur, self.livreur.pk).status_code, 403)

    def test_vendeur_deja_assigne_ne_peut_pas_livrer_sa_commande(self):
        """Fiche déjà assignée au propriétaire de la boutique (donnée
        existante en base, créée hors du service d'assignation)."""
        Utilisateur.objects.filter(pk=self.vendeur.pk).update(role=Role.LIVREUR)
        self.vendeur.refresh_from_db()
        Livraison.objects.filter(pk=self.livraison.pk).update(livreur=self.vendeur)
        self.assertEqual(self.patch(self.vendeur, Statut.EXPEDIEE).status_code, 403)

    def test_django_admin_ne_propose_que_les_livreurs_actifs(self):
        from .admin import LivraisonAdminForm

        Utilisateur.objects.filter(pk=self.autre_livreur.pk).update(is_active=False)
        formulaire = LivraisonAdminForm(instance=self.livraison)
        self.assertEqual(list(formulaire.fields["livreur"].queryset), [self.livreur])

    def test_django_admin_refuse_le_proprietaire(self):
        from .admin import LivraisonAdminForm

        Utilisateur.objects.filter(pk=self.vendeur.pk).update(role=Role.LIVREUR)
        formulaire = LivraisonAdminForm(data={"livreur": self.vendeur.pk}, instance=self.livraison)
        self.assertFalse(formulaire.is_valid())


class RoleLivreurTests(DonneesLivraison, APITestCase):
    """Nommer / retirer un livreur ; le retrait est immédiat."""

    def setUp(self):
        self.creer_donnees()

    def nommer(self, utilisateur, cible):
        return self.api(utilisateur).post(reverse("livraison:livreur-nommer"), {"utilisateur_id": cible.pk}, format="json")

    def retirer(self, utilisateur, cible):
        return self.api(utilisateur).post(reverse("livraison:livreur-retirer", args=[cible.pk]))

    def test_ex_livreur_perd_immediatement_la_main(self):
        r = self.retirer(self.admin, self.livreur)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([l["id"] for l in r.data["livraisons_a_reassigner"]], [str(self.livraison.pk)])
        self.livreur.refresh_from_db()
        self.assertEqual(self.livreur.role, Role.CLIENT)
        self.assertEqual(self.patch(self.livreur, Statut.EXPEDIEE).status_code, 403)
        self.assertEqual(self.detail(self.livreur).status_code, 404)

    def test_ex_livreur_par_le_django_admin_perd_aussi_la_main(self):
        Utilisateur.objects.filter(pk=self.livreur.pk).update(role=Role.CLIENT)
        self.livreur.refresh_from_db()
        self.assertEqual(self.patch(self.livreur, Statut.EXPEDIEE).status_code, 403)

    def test_nommer_un_client(self):
        r = self.nommer(self.admin, self.autre_client)
        self.assertEqual(r.status_code, 200)
        self.autre_client.refresh_from_db()
        self.assertEqual(self.autre_client.role, Role.LIVREUR)
        ids = [l["id"] for l in self.api(self.admin).get(reverse("livraison:livreur-list")).data["results"]]
        self.assertIn(self.autre_client.pk, ids)

    def test_nommer_un_vendeur_refuse(self):
        self.assertEqual(self.nommer(self.admin, self.vendeur).status_code, 400)
        demandeur = self._create_user("demande@test.com", Role.CLIENT, statut_kyc=StatutKYC.EN_ATTENTE)
        self.assertEqual(self.nommer(self.admin, demandeur).status_code, 400)
        self.assertEqual(self.nommer(self.admin, self.admin).status_code, 400)

    def test_nommer_retirer_reserve_a_l_administration(self):
        staff = self._create_user("staff@test.com", Role.CLIENT, is_staff=True)
        for utilisateur in (staff, self.livreur, self.vendeur):
            self.assertEqual(self.nommer(utilisateur, self.autre_client).status_code, 403)
            self.assertEqual(self.retirer(utilisateur, self.livreur).status_code, 403)
            self.assertEqual(self.api(utilisateur).get(reverse("livraison:livreur-list")).status_code, 403)

    def test_livreur_desactive_refuse(self):
        """Suspension par is_active : le jeton n'est plus accepté."""
        client = APIClient()
        r = client.post("/api/utilisateurs/connexion/", {"email": "livreur@test.com", "password": "testpass123"},
                        format="json")
        jeton = r.data.get("access") or r.data.get("data", {}).get("access") or r.data.get("tokens", {}).get("access")
        Utilisateur.objects.filter(pk=self.livreur.pk).update(is_active=False)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {jeton}")
        self.assertEqual(client.get(reverse("livraison:livraison-list")).status_code, 401)


class TransitionsAdministrationTests(DonneesLivraison, APITestCase):
    """L'administration suit la même table de transitions : pas de saut
    d'étape, et jamais de sortie d'une livraison livrée (reversement en
    cours)."""

    def setUp(self):
        self.creer_donnees()

    def test_admin_ne_peut_pas_sauter_d_etape(self):
        self.assertEqual(self.patch(self.admin, Statut.EN_COURS).status_code, 400)
        self.assertEqual(self.patch(self.admin, Statut.EXPEDIEE).status_code, 403)  # étape du livreur

    def test_admin_ne_peut_pas_faire_regresser_une_livraison_livree(self):
        self.livrer()
        for statut in (Statut.EN_ATTENTE, Statut.EN_COURS, Statut.ECHOUEE):
            self.assertEqual(self.patch(self.admin, statut).status_code, 400)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.LIVREE)
        self.assertEqual(self.reversement().statut, Reversement.Statut.EN_RETRACTATION)

    def test_annulee_via_api_refusee(self):
        self.assertEqual(self.patch(self.admin, Statut.ANNULEE).status_code, 400)


class EchecTests(DonneesLivraison, APITestCase):
    """Échec : nouvelle tentative (au plus LIVRAISON_TENTATIVES_MAX) ou
    abandon (commande annulée, remboursement)."""

    def setUp(self):
        self.creer_donnees()
        self.mettre_en_cours()

    def echouer(self):
        return self.patch(self.livreur, Statut.ECHOUEE, commentaire="Client injoignable")

    def abandonner(self, utilisateur, commentaire="Trois échecs, client injoignable"):
        return self.api(utilisateur).post(
            reverse("livraison:livraison-abandonner", args=[self.livraison.pk]), {"commentaire": commentaire},
            format="json",
        )

    def test_echec_exige_un_motif(self):
        self.assertEqual(self.patch(self.livreur, Statut.ECHOUEE).status_code, 400)
        self.assertEqual(self.echouer().status_code, 200)

    def test_nouvelle_tentative_par_l_administration(self):
        self.echouer()
        self.assertEqual(self.patch(self.livreur, Statut.EN_COURS).status_code, 403)  # pas le livreur
        self.assertEqual(self.patch(self.admin, Statut.EN_COURS).status_code, 200)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.tentatives, 2)
        self.assertEqual(self.livraison.code_essais, 0)
        self.assertEqual(self.patch(self.livreur, Statut.LIVREE, code=self.code()).status_code, 200)

    def test_tentatives_limitees(self):
        with self.settings(LIVRAISON_TENTATIVES_MAX=2):
            self.echouer()
            self.assertEqual(self.patch(self.admin, Statut.EN_COURS).status_code, 200)
            self.echouer()
            self.assertEqual(self.patch(self.admin, Statut.EN_COURS).status_code, 400)

    def test_abandon_annule_la_commande_et_cree_le_remboursement(self):
        self.echouer()
        r = self.abandonner(self.admin)
        self.assertEqual(r.status_code, 200, r.data)
        self.livraison.refresh_from_db()
        self.commande.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.ANNULEE)
        self.assertEqual((self.commande.status, self.commande.motif_annulation), ("annulee", "livraison_echouee"))
        remboursement = Remboursement.objects.get(commande=self.commande)
        self.assertEqual((remboursement.statut, remboursement.motif), ("a_traiter", "commande_annulee"))
        self.assertEqual(self.reversement().statut, Reversement.Statut.ANNULE)
        derniere = self.livraison.historique.first()
        self.assertEqual((derniere.role_acteur, derniere.commentaire), ("administration", "Trois échecs, client injoignable"))

    def test_abandon_seulement_apres_echec_et_par_l_administration(self):
        self.assertEqual(self.abandonner(self.admin).status_code, 400)  # encore en cours
        self.echouer()
        self.assertEqual(self.abandonner(self.livreur).status_code, 403)
        self.assertEqual(self.abandonner(self.admin, commentaire="").status_code, 400)

    def test_commande_expediee_toujours_non_annulable_par_l_administration_classique(self):
        r = self.api(self.admin).post(f"/api/commandes/administration/{self.commande.pk}/annuler/")
        self.assertEqual(r.status_code, 409)


class AnnulationTests(DonneesLivraison, APITestCase):
    """Commande annulée : la fiche passe « annulée » et le livreur ne voit
    plus les coordonnées du client."""

    def setUp(self):
        self.creer_donnees()
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.CONFIRMEE)

    def test_annulation_de_la_commande_annule_la_livraison(self):
        annuler_commande(self.commande, Commande.MotifAnnulation.ADMINISTRATION, acteur=self.admin)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.ANNULEE)
        ligne = self.livraison.historique.get()
        self.assertEqual((ligne.ancien_status, ligne.nouveau_status, ligne.role_acteur),
                         (Statut.EN_ATTENTE, Statut.ANNULEE, "administration"))
        donnees = self.detail(self.livreur).data
        self.assertEqual((donnees["telephone_contact"], donnees["adresse"], donnees["adresse_livraison"]), ("", None, ""))
        self.assertEqual(self.patch(self.admin, Statut.EN_COURS).status_code, 400)

    def test_annulation_client_annule_la_livraison(self):
        r = self.api(self.client_user).post(f"/api/commandes/{self.commande.pk}/annuler/")
        self.assertEqual(r.status_code, 200)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.ANNULEE)

    def test_annulation_refusee_ne_touche_pas_la_livraison(self):
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.PREPARATION)
        r = self.api(self.client_user).post(f"/api/commandes/{self.commande.pk}/annuler/")
        self.assertEqual(r.status_code, 409)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.EN_ATTENTE)
        self.assertFalse(self.livraison.historique.exists())

    def test_migration_des_fiches_existantes(self):
        import importlib
        from django.apps import apps as registre

        migration = importlib.import_module("apps.livraison.migrations.0003_fiches_des_commandes_annulees")
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.ANNULEE)
        migration.annuler_fiches(registre, None)
        self.livraison.refresh_from_db()
        self.assertEqual(self.livraison.status, Statut.ANNULEE)
        self.assertEqual(self.livraison.historique.get().role_acteur, "systeme")

    def test_migration_des_fiches_orphelines(self):
        """Fiche non terminée d'une commande annulée (créée par une validation
        de paiement concurrente à l'annulation) : passée « annulée ».
        Rejouée, la reprise ne change rien ; une fiche livrée, ou celle
        d'une commande active, n'est jamais touchée."""
        import importlib
        from django.apps import apps as registre

        migration = importlib.import_module("apps.livraison.migrations.0006_fiches_orphelines_des_commandes_annulees")
        Livraison.objects.filter(pk=self.livraison.pk).update(code_hash="empreinte", code_chiffre="chiffre")
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.ANNULEE)

        def fiche(statut_commande, statut_fiche):
            commande = Commande.objects.create(boutique=self.boutique, client=self.client_user, groupe=self.groupe,
                                               montant_total=Decimal("5000"), status=statut_commande)
            return Livraison.objects.create(commande=commande, status=statut_fiche, adresse_livraison="Cocody")

        active = fiche(Commande.Status.CONFIRMEE, Statut.EN_ATTENTE)
        livree = fiche(Commande.Status.ANNULEE, Statut.LIVREE)
        for _ in range(2):
            migration.annuler_fiches_orphelines(registre, None)
        self.livraison.refresh_from_db()
        self.assertEqual((self.livraison.status, self.livraison.code_hash, self.livraison.code_chiffre),
                         (Statut.ANNULEE, "", ""))
        ligne = self.livraison.historique.get()  # une seule ligne malgré deux passages
        self.assertEqual((ligne.ancien_status, ligne.nouveau_status, ligne.role_acteur, ligne.effectue_par),
                         (Statut.EN_ATTENTE, Statut.ANNULEE, "systeme", None))
        for intacte, statut in ((active, Statut.EN_ATTENTE), (livree, Statut.LIVREE)):
            intacte.refresh_from_db()
            self.assertEqual(intacte.status, statut)
            self.assertFalse(intacte.historique.exists())


class ContestationTests(DonneesLivraison, APITestCase):
    """B — « non reçu » pendant les 7 jours : reversement suspendu jusqu'à
    la décision de l'administration."""

    def setUp(self):
        self.creer_donnees()
        self.livrer()

    def contester(self, utilisateur, motif="Je n'ai rien reçu"):
        return self.api(utilisateur).post(
            reverse("livraison:livraison-contester", args=[self.livraison.pk]), {"motif": motif}, format="json",
        )

    def resoudre(self, decision):
        return self.api(self.admin).post(
            reverse("livraison:livraison-contestation-resoudre", args=[self.livraison.pk]),
            {"decision": decision, "commentaire": "Vérifié"}, format="json",
        )

    def test_contestation_suspend_le_reversement_et_alerte(self):
        r = self.contester(self.client_user)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.reversement().statut, Reversement.Statut.SUSPENDU)
        self.assertTrue(Notification.objects.filter(destinataire=self.admin, titre="Livraison contestée").exists())
        self.assertEqual(self.detail(self.client_user).data["contestation"]["statut"], "ouverte")
        self.assertEqual(
            Notification.objects.get(destinataire=self.admin, titre="Livraison contestée").lien_redirection,
            f"/administration/livraisons/{self.livraison.pk}",
        )

    def test_une_seule_contestation_par_son_client(self):
        self.assertEqual(self.contester(self.autre_client).status_code, 404)
        self.assertEqual(self.contester(self.livreur).status_code, 404)
        self.assertEqual(self.contester(self.client_user).status_code, 201)
        self.assertEqual(self.contester(self.client_user).status_code, 400)

    def test_contestation_hors_delai_refusee(self):
        Livraison.objects.filter(pk=self.livraison.pk).update(date_livraison=timezone.now() - timedelta(days=8))
        self.assertEqual(self.contester(self.client_user).status_code, 400)

    def test_contestation_avant_livraison_refusee(self):
        Livraison.objects.filter(pk=self.livraison.pk).update(status=Statut.EN_COURS)
        self.assertEqual(self.contester(self.client_user).status_code, 400)

    def test_rejetee_le_reversement_reprend(self):
        self.contester(self.client_user)
        self.assertEqual(self.resoudre("rejetee").status_code, 200)
        self.assertEqual(self.reversement().statut, Reversement.Statut.EN_RETRACTATION)
        self.assertEqual(self.resoudre("fondee").status_code, 400)  # déjà traitée
        decision = Notification.objects.get(destinataire=self.client_user, titre__startswith="Contestation de la commande")
        self.assertEqual(decision.lien_redirection, f"/livraisons/{self.livraison.pk}")

    def test_fondee_remboursement_et_reversement_annule(self):
        self.contester(self.client_user)
        self.assertEqual(self.resoudre("fondee").status_code, 200)
        self.assertEqual(self.reversement().statut, Reversement.Statut.ANNULE)
        remboursement = Remboursement.objects.get(commande=self.commande)
        self.assertEqual((remboursement.motif, remboursement.statut), ("livraison_non_recue", "a_traiter"))

    def test_resolution_reservee_a_l_administration(self):
        self.contester(self.client_user)
        for utilisateur in (self.client_user, self.livreur, self.vendeur):
            r = self.api(utilisateur).post(
                reverse("livraison:livraison-contestation-resoudre", args=[self.livraison.pk]),
                {"decision": "rejetee"}, format="json",
            )
            self.assertEqual(r.status_code, 403)

    def test_retour_clos_ne_reprend_pas_un_reversement_conteste(self):
        self.contester(self.client_user)
        reversements.reprendre_reversement(self.commande)
        self.assertEqual(self.reversement().statut, Reversement.Statut.SUSPENDU)

    def test_filtre_administration_contestations_ouvertes(self):
        self.contester(self.client_user)
        r = self.api(self.admin).get(reverse("livraison:livraison-list"), {"contestation": "ouverte"})
        self.assertEqual([l["id"] for l in r.data["results"]], [str(self.livraison.pk)])


class IsolationEtSuiviTests(DonneesLivraison, APITestCase):
    """Ce que chacun voit."""

    def setUp(self):
        self.creer_donnees()

    def test_vendeur_voit_les_livraisons_de_sa_boutique_en_lecture_seule(self):
        r = self.api(self.vendeur).get(reverse("livraison:vendeur-livraison-list"))
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(
            set(r.data["results"][0]),
            {"id", "commande", "numero_commande", "livreur", "status", "date_livraison_estimee",
             "date_expedition", "date_livraison", "created_at", "updated_at"},
        )
        self.assertEqual(r.data["results"][0]["livreur"], {"prenom": "User"})
        autre = self.api(self.autre_vendeur)
        self.assertEqual(autre.get(reverse("livraison:vendeur-livraison-list")).data["count"], 0)
        self.assertEqual(
            autre.get(reverse("livraison:vendeur-livraison-detail", args=[self.livraison.pk])).status_code, 404,
        )
        self.assertEqual(self.patch(self.vendeur, Statut.EXPEDIEE).status_code, 403)

    def test_espace_vendeur_reserve_aux_vendeurs_valides(self):
        for utilisateur in (self.client_user, self.livreur):
            self.assertEqual(self.api(utilisateur).get(reverse("livraison:vendeur-livraison-list")).status_code, 403)

    def test_historique_tiers_404_et_role_a_la_place_de_l_id(self):
        self.patch(self.livreur, Statut.EXPEDIEE)
        url = reverse("livraison:livraison-historique", args=[self.livraison.pk])
        for tiers in (self.autre_client, self.autre_livreur, self.vendeur):
            self.assertEqual(self.api(tiers).get(url).status_code, 404)
        ligne = self.api(self.client_user).get(url).data["results"][0]
        self.assertEqual(ligne["acteur"], "livreur")
        self.assertNotIn("effectue_par", ligne)
        self.assertEqual(self.api(self.admin).get(url).data["results"][0]["effectue_par"], self.livreur.pk)

    def test_suivi_client(self):
        demain = timezone.localdate() + timedelta(days=1)
        services.assigner_livreur(self.livraison, self.livreur, self.admin, date_livraison_estimee=demain)
        donnees = self.detail(self.client_user).data
        self.assertEqual(donnees["livreur"], {"prenom": "User", "telephone": None})
        self.assertEqual(donnees["date_livraison_estimee"], demain.isoformat())
        # Point GPS : null, ce checkout n'en a pas (PositionLivraisonTests).
        self.assertEqual(donnees["adresse"], {"zone": "abidjan", "commune": "Cocody", "quartier": "Angré",
                                              "point_de_repere": "Face pharmacie", "telephone": "0700000000",
                                              "latitude": None, "longitude": None})
        self.mettre_en_cours()
        self.assertEqual(self.detail(self.client_user).data["livreur"]["telephone"], "0102030405")
        self.assertEqual(self.patch(self.livreur, Statut.LIVREE, code=self.code()).status_code, 200)
        donnees = self.detail(self.client_user).data
        self.assertIsNone(donnees["livreur"]["telephone"])
        self.assertIsNotNone(donnees["date_limite_contestation"])

    def test_livreur_ne_voit_plus_les_coordonnees_apres_livraison(self):
        self.assertEqual(self.detail(self.livreur).data["adresse"]["commune"], "Cocody")
        self.livrer()
        donnees = self.detail(self.livreur).data
        self.assertEqual((donnees["telephone_contact"], donnees["adresse"], donnees["adresse_livraison"]), ("", None, ""))

    def test_is_staff_sans_role_admin_n_a_aucun_pouvoir(self):
        staff = self._create_user("staff@test.com", Role.CLIENT, is_staff=True)
        self.assertEqual(self.patch(staff, Statut.EXPEDIEE).status_code, 403)
        self.assertEqual(self.api(staff).get(reverse("livraison:livraison-list")).data["count"], 0)

    def test_django_admin_modification_reservee_au_role_admin(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from .admin import LivraisonAdmin

        staff = self._create_user("staff2@test.com", Role.CLIENT, is_staff=True, is_superuser=True)
        requete = RequestFactory().get("/")
        requete.user = staff
        self.assertFalse(LivraisonAdmin(Livraison, AdminSite()).has_change_permission(requete, self.livraison))


class PositionLivraisonTests(DonneesLivraison, APITestCase):
    """Point GPS du lieu de livraison : mêmes règles que l'adresse (client,
    livreur assigné tant que la livraison n'est pas terminée,
    administration), et jamais le vendeur."""

    def setUp(self):
        self.creer_donnees()
        GroupeCommande.objects.filter(pk=self.groupe.pk).update(
            livraison_latitude=Decimal("5.359952"), livraison_longitude=Decimal("-3.986912"),
        )

    def passer(self, statut):
        Livraison.objects.filter(pk=self.livraison.pk).update(status=statut)

    def point(self, utilisateur):
        r = self.detail(utilisateur)
        self.assertEqual(r.status_code, 200)
        adresse = r.json()["adresse"]
        return None if adresse is None else (adresse["latitude"], adresse["longitude"])

    def test_client_et_administration_voient_le_point_a_tout_statut(self):
        for statut in Statut.values:
            with self.subTest(statut):
                self.passer(statut)
                for utilisateur in (self.client_user, self.admin):
                    self.assertEqual(self.point(utilisateur), (5.359952, -3.986912))

    def test_livreur_assigne_seulement_pendant_la_livraison(self):
        for statut in (Statut.EN_ATTENTE, Statut.EXPEDIEE, Statut.EN_COURS, Statut.ECHOUEE):
            with self.subTest(statut):
                self.passer(statut)
                self.assertEqual(self.point(self.livreur), (5.359952, -3.986912))
        for statut in (Statut.LIVREE, Statut.ANNULEE):
            with self.subTest(statut):
                self.passer(statut)
                self.assertIsNone(self.point(self.livreur))
                liste = self.api(self.livreur).get(reverse("livraison:livraison-list"))
                self.assertNotIn("5.359952", liste.content.decode())
        self.assertEqual(self.detail(self.autre_livreur).status_code, 404)

    def test_masque_apres_une_vraie_livraison(self):
        self.livrer()
        self.assertIsNone(self.point(self.livreur))
        self.assertEqual(self.point(self.client_user), (5.359952, -3.986912))

    def test_le_vendeur_ne_voit_jamais_le_point(self):
        vendeur = self.api(self.vendeur)
        urls = [
            reverse("livraison:vendeur-livraison-list"),
            reverse("livraison:vendeur-livraison-detail", args=[self.livraison.pk]),
            f"/api/commandes/vendeur/{self.commande.pk}/",
            "/api/commandes/vendeur/",
        ]
        for statut in Statut.values:
            self.passer(statut)
            for url in urls:
                with self.subTest(statut, url=url):
                    r = vendeur.get(url)
                    self.assertEqual(r.status_code, 200)
                    self.assertNotIn("5.359952", r.content.decode())
                    self.assertNotIn("latitude", r.content.decode())
        self.assertEqual(self.detail(self.vendeur).status_code, 404)

    def test_sans_point_null(self):
        GroupeCommande.objects.filter(pk=self.groupe.pk).update(livraison_latitude=None, livraison_longitude=None)
        for utilisateur in (self.client_user, self.livreur, self.admin):
            self.assertEqual(self.point(utilisateur), (None, None))


class PerimetreAchatsTests(DonneesLivraison, APITestCase):
    """?perimetre=achats : les livraisons des commandes du compte, en
    représentation client, quel que soit le rôle (livreur ou administrateur
    qui achète). Le paramètre restreint, il n'élargit jamais."""

    def setUp(self):
        self.creer_donnees()
        self.achat_livreur = self.achat(self.livreur, livreur=self.autre_livreur)
        self.achat_admin = self.achat(self.admin)

    def achat(self, acheteur, livreur=None):
        groupe = GroupeCommande.objects.create(
            client=acheteur, livraison_zone="abidjan", livraison_commune="Cocody", livraison_quartier="Riviera",
            livraison_point_de_repere="Carrefour", livraison_telephone="0700000009",
        )
        commande = Commande.objects.create(
            boutique=self.boutique, client=acheteur, groupe=groupe, montant_total=Decimal("3000"),
            status=Commande.Status.CONFIRMEE,
        )
        return Livraison.objects.create(commande=commande, livreur=livreur, adresse_livraison="Cocody, Riviera")

    def ids(self, utilisateur, **params):
        r = self.api(utilisateur).get(reverse("livraison:livraison-list"), params)
        self.assertEqual(r.status_code, 200)
        return {ligne["id"] for ligne in r.data["results"]}

    def detail_achat(self, utilisateur, livraison):
        return self.api(utilisateur).get(
            reverse("livraison:livraison-detail", args=[livraison.pk]), {"perimetre": "achats"},
        )

    def test_livreur_acheteur(self):
        self.assertEqual(self.ids(self.livreur), {str(self.livraison.pk)})  # sans paramètre : inchangé
        self.assertEqual(self.ids(self.livreur, perimetre="achats"), {str(self.achat_livreur.pk)})
        self.assertEqual(self.detail_achat(self.livreur, self.achat_livreur).status_code, 200)
        # Une livraison qu'il transporte n'est pas un de ses achats.
        self.assertEqual(self.detail_achat(self.livreur, self.livraison).status_code, 404)
        # L'achat d'un autre client : jamais visible.
        self.assertEqual(self.detail_achat(self.autre_livreur, self.achat_livreur).status_code, 404)
        self.assertEqual(self.ids(self.autre_livreur, perimetre="achats"), set())

    def test_representation_client(self):
        achat = self.detail_achat(self.livreur, self.achat_livreur).data
        cles_client = set(self.detail(self.client_user).data)
        self.assertEqual(set(achat), cles_client)

    def test_administrateur_acheteur(self):
        self.assertEqual(len(self.ids(self.admin)), 3)
        self.assertEqual(self.ids(self.admin, perimetre="achats"), {str(self.achat_admin.pk)})
        # Les filtres d'administration ne rouvrent pas le périmètre.
        self.assertEqual(self.ids(self.admin, perimetre="achats", status="en_attente"), {str(self.achat_admin.pk)})
        self.assertEqual(self.detail_achat(self.admin, self.livraison).status_code, 404)

    def test_client_et_valeur_inconnue_inchanges(self):
        attendu = {str(self.livraison.pk)}
        self.assertEqual(self.ids(self.client_user), attendu)
        self.assertEqual(self.ids(self.client_user, perimetre="achats"), attendu)
        self.assertEqual(self.ids(self.livreur, perimetre="tout"), attendu)
        self.assertEqual(len(self.ids(self.admin, perimetre="tout")), 3)


class PerformanceEtLimitesTests(DonneesLivraison, APITestCase):
    """Pas de N+1 et limite de débit dédiée."""

    def setUp(self):
        self.creer_donnees()

    def ajouter_livraisons(self, nombre):
        for _ in range(nombre):
            groupe = GroupeCommande.objects.create(client=self.client_user, livraison_commune="A",
                                                   livraison_quartier="B", livraison_telephone="0700000001")
            commande = Commande.objects.create(boutique=self.boutique, client=self.client_user, groupe=groupe,
                                               montant_total=Decimal("1"))
            Livraison.objects.create(commande=commande, livreur=self.livreur, adresse_livraison="x")

    def nombre_de_requetes(self, utilisateur, url):
        client = self.api(utilisateur)
        with CaptureQueriesContext(connection) as contexte:
            self.assertEqual(client.get(url).status_code, 200)
        return len(contexte)

    def test_listes_sans_n_plus_un(self):
        urls = {
            self.admin: reverse("livraison:livraison-list"),
            self.client_user: reverse("livraison:livraison-list"),
            self.livreur: reverse("livraison:livraison-list"),
            self.vendeur: reverse("livraison:vendeur-livraison-list"),
        }
        self.ajouter_livraisons(2)
        avant = {utilisateur: self.nombre_de_requetes(utilisateur, url) for utilisateur, url in urls.items()}
        self.ajouter_livraisons(6)
        for utilisateur, url in urls.items():
            self.assertEqual(self.nombre_de_requetes(utilisateur, url), avant[utilisateur], utilisateur.email)

    def test_limite_de_debit_sur_les_changements_de_statut(self):
        from django.core.cache import cache

        cache.clear()
        taux = {**ScopedRateThrottle.THROTTLE_RATES, "livraison_statut": "2/hour"}
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", taux):
            self.patch(self.livreur, Statut.EXPEDIEE)
            self.patch(self.livreur, Statut.EN_COURS)
            self.assertEqual(self.patch(self.livreur, Statut.LIVREE, code="1").status_code, 429)
        cache.clear()

    def test_limite_de_debit_sur_les_contestations(self):
        from .views import ContesterLivraisonView

        self.assertEqual(ContesterLivraisonView.throttle_scope, "livraison_contestation")


class ConcurrenceTests(DonneesLivraison, TransactionTestCase):
    """« Livrée » et « échouée » simultanés : une seule l'emporte, livraison
    et commande restent cohérentes (jamais une livraison « échouée » avec
    une commande « livrée » et un reversement ouvert)."""

    def setUp(self):
        self.creer_donnees()
        Commande.objects.filter(pk=self.commande.pk).update(status=Commande.Status.EXPEDIEE)
        Livraison.objects.filter(pk=self.livraison.pk).update(status=Statut.EN_COURS, tentatives=1)
        livraison = Livraison.objects.get(pk=self.livraison.pk)
        services._nouveau_code(livraison)
        livraison.save()

    def test_livree_et_echouee_simultanees(self):
        from apps.commandes import services as services_commandes

        code = self.code()
        original = services_commandes.synchroniser_depuis_livraison

        def lent(commande, statut):
            time.sleep(0.3)  # la première requête garde le verrou un moment
            return original(commande, statut)

        barriere = threading.Barrier(2)
        resultats = {}

        def lancer(statut, corps):
            try:
                barriere.wait(timeout=5)
                resultats[statut] = self.patch(self.livreur, statut, **corps).status_code
            finally:
                connection.close()

        with mock.patch("apps.commandes.services.synchroniser_depuis_livraison", lent):
            fils = [
                threading.Thread(target=lancer, args=(Statut.LIVREE, {"code": code})),
                threading.Thread(target=lancer, args=(Statut.ECHOUEE, {"commentaire": "Client absent"})),
            ]
            for fil in fils:
                fil.start()
            for fil in fils:
                fil.join()

        self.assertEqual(sorted(resultats.values()), [200, 400], resultats)
        self.livraison.refresh_from_db()
        self.commande.refresh_from_db()
        if self.livraison.status == Statut.LIVREE:
            self.assertEqual(self.commande.status, "livree")
            self.assertEqual(self.reversement().statut, Reversement.Statut.EN_RETRACTATION)
        else:
            self.assertEqual(self.livraison.status, Statut.ECHOUEE)
            self.assertEqual(self.commande.status, "expediee")
            self.assertEqual(self.reversement().statut, Reversement.Statut.EN_ATTENTE_LIVRAISON)
        self.assertEqual(self.livraison.historique.count(), 1)


# =====================================================================
# TARIFS DE LIVRAISON
# =====================================================================

class TarifsDeLivraisonTests(APITestCase):
    """Tarifs de la migration : Abidjan 1 500 et hors Abidjan 3 000 par défaut."""

    URL_PUBLIQUE = "/api/livraison/tarifs/"
    URL_ADMIN = "/api/livraison/admin/tarifs/"

    def setUp(self):
        creer = Utilisateur.objects.create_user
        self.admin = creer(email="admin@tarif.ci", password="x", nom="A", prenom="D", role=Role.ADMIN)
        self.client_user = creer(email="client@tarif.ci", password="x", nom="C", prenom="L", email_verifie=True)
        self.vendeur = creer(email="vendeur@tarif.ci", password="x", nom="V", prenom="E", role=Role.VENDEUR,
                             statut_kyc=StatutKYC.VALIDE)
        self.livreur = creer(email="livreur@tarif.ci", password="x", nom="L", prenom="I", role=Role.LIVREUR)
        self.defaut_abidjan = TarifLivraison.objects.get(zone="abidjan", commune="")

    def api(self, utilisateur=None):
        api = APIClient()
        if utilisateur is not None:
            api.force_authenticate(utilisateur)
        return api

    def test_normalisation_des_communes(self):
        self.assertEqual(normaliser_commune("Port-Bouët"), normaliser_commune("  port bouet "))
        self.assertEqual(normaliser_commune("Yopougon"), "yopougon")
        self.assertEqual(normaliser_commune(""), "")

    def test_communes_du_district_creees_par_migration(self):
        communes = set(TarifLivraison.objects.filter(zone="abidjan").exclude(commune="").values_list("commune", flat=True))
        self.assertEqual(communes, {
            "Abobo", "Adjamé", "Attécoubé", "Cocody", "Koumassi", "Marcory", "Plateau", "Port-Bouët",
            "Treichville", "Yopougon", "Anyama", "Bingerville", "Songon",
        })

    def test_zone_deduite_de_la_commune(self):
        for commune in ("Cocody", "COCODY", "port bouet", "Port-Bouët", " adjame ", "Attecoubé"):
            with self.subTest(commune):
                self.assertEqual(tarif_applicable(commune).zone, "abidjan")
        for commune in ("Bouaké", "Grand-Bassam", "Abidjan", "", "Riviera"):
            with self.subTest(commune):
                self.assertEqual(tarif_applicable(commune).zone, "hors_abidjan")

    def test_tarif_applicable(self):
        TarifLivraison.objects.filter(commune="Cocody").update(montant=1000)
        TarifLivraison.objects.create(zone="hors_abidjan", commune="Bouaké", montant=2500)
        self.assertEqual(tarif_applicable("COCODY").tarif.montant, 1000)
        self.assertEqual(tarif_applicable("Abobo").tarif.montant, 1500)
        self.assertEqual(tarif_applicable("bouake").tarif.montant, 2500)
        self.assertEqual(tarif_applicable("Korhogo").tarif.montant, 3000)
        # Commune désactivée : reste dans sa zone, tarif par défaut de la zone.
        TarifLivraison.objects.filter(commune="Cocody").update(est_actif=False)
        self.assertEqual((tarif_applicable("Cocody").zone, tarif_applicable("Cocody").tarif.montant), ("abidjan", 1500))
        TarifLivraison.objects.filter(zone="hors_abidjan", commune="").delete()
        with self.assertRaises(TarifIntrouvable):
            tarif_applicable("Korhogo")

    def test_livraison_offerte_frais_au_vendeur(self):
        boutique = Boutique(proprietaire=self.vendeur, nom="B", livraison_offerte=True)
        self.assertEqual(frais_de_la_commande(boutique, self.defaut_abidjan), {
            "frais_livraison": Decimal("0"), "livraison_offerte": True, "frais_livraison_vendeur": Decimal("1500"),
        })

    def test_grille_publique_pour_le_menu_deroulant(self):
        TarifLivraison.objects.filter(commune="Cocody").update(montant=1000)
        TarifLivraison.objects.filter(commune="Abobo").update(est_actif=False)
        TarifLivraison.objects.create(zone="hors_abidjan", commune="Bouaké", montant=2500)
        TarifLivraison.objects.create(zone="hors_abidjan", commune="Korhogo", montant=2800, est_actif=False)
        with self.assertNumQueries(1):
            r = self.api().get(self.URL_PUBLIQUE)
        self.assertEqual(r.status_code, 200)
        communes = {ligne["commune"]: ligne for ligne in r.data["communes"]}
        self.assertEqual(len(communes), 14)  # 13 communes du district + Bouaké
        self.assertEqual(communes["Cocody"], {"commune": "Cocody", "zone": "abidjan", "montant": 1000})
        self.assertEqual(communes["Abobo"]["montant"], 1500)  # désactivée : tarif par défaut d'Abidjan
        self.assertEqual(communes["Bouaké"], {"commune": "Bouaké", "zone": "hors_abidjan", "montant": 2500})
        self.assertNotIn("Korhogo", communes)  # inactive : couverte par « autres villes »
        self.assertEqual(r.data["autres_villes"], {"zone": "hors_abidjan", "montant": 3000})

    def test_grille_publique_jeton_expire_ignore(self):
        """Un vieux jeton gardé par le client ne casse pas le checkout (pas de 401)."""
        jeton = AccessToken.for_user(self.client_user)
        jeton.set_exp(lifetime=timedelta(seconds=-1))
        api = self.api()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {jeton}")
        r = api.get(self.URL_PUBLIQUE)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["autres_villes"], {"zone": "hors_abidjan", "montant": 3000})

    def test_grille_publique_sans_limite_anon(self):
        """Le checkout d'un visiteur ne dépend que du seau 'catalogue_public'."""
        with mock.patch.object(AnonRateThrottle, "allow_request", return_value=False), \
                mock.patch.object(UserRateThrottle, "allow_request", return_value=False):
            self.assertEqual(self.api().get(self.URL_PUBLIQUE).status_code, 200)
            self.assertEqual(self.api(self.client_user).get(self.URL_PUBLIQUE).status_code, 200)

    def test_administration_reservee_aux_administrateurs(self):
        url_detail = f"{self.URL_ADMIN}{self.defaut_abidjan.pk}/"
        self.assertEqual(self.api().get(self.URL_ADMIN).status_code, 401)
        for utilisateur in (self.client_user, self.vendeur, self.livreur):
            with self.subTest(utilisateur.role):
                api = self.api(utilisateur)
                self.assertEqual(api.get(self.URL_ADMIN).status_code, 403)
                self.assertEqual(api.post(self.URL_ADMIN, {"zone": "abidjan", "commune": "Cocody",
                                                           "montant": 1}).status_code, 403)
                self.assertEqual(api.patch(url_detail, {"montant": 1}).status_code, 403)
        self.defaut_abidjan.refresh_from_db()
        self.assertEqual((self.defaut_abidjan.montant, TarifLivraison.objects.count()), (1500, 15))

    def test_creation_et_modification_tracees(self):
        api = self.api(self.admin)
        r = api.post(self.URL_ADMIN, {"zone": "hors_abidjan", "commune": " Grand-Bassam ", "montant": 2000})
        self.assertEqual(r.status_code, 201, r.data)
        tarif = TarifLivraison.objects.get(pk=r.data["id"])
        self.assertEqual((tarif.commune, tarif.commune_normalisee, tarif.modifie_par),
                         ("Grand-Bassam", "grandbassam", self.admin))
        r = api.patch(f"{self.URL_ADMIN}{self.defaut_abidjan.pk}/", {"montant": 2000})
        self.assertEqual((r.status_code, r.data["montant"]), (200, 2000))
        r = api.patch(f"{self.URL_ADMIN}{tarif.pk}/", {"est_actif": False})
        self.assertEqual((r.status_code, r.data["est_actif"]), (200, False))

    def test_validations(self):
        api = self.api(self.admin)
        url_defaut = f"{self.URL_ADMIN}{self.defaut_abidjan.pk}/"
        cas = {
            "doublon de commune (casse, accents)": api.post(self.URL_ADMIN, {"zone": "abidjan", "commune": "port bouet",
                                                                              "montant": 1200}),
            # Une commune du district ne peut pas recevoir un tarif « hors
            # Abidjan » : sa zone deviendrait ambiguë.
            "commune du district dans l'autre zone": api.post(self.URL_ADMIN, {"zone": "hors_abidjan",
                                                                                "commune": "COCODY", "montant": 1}),
            "second tarif par défaut": api.post(self.URL_ADMIN, {"zone": "abidjan", "commune": "", "montant": 900}),
            "montant négatif": api.post(self.URL_ADMIN, {"zone": "hors_abidjan", "commune": "Man", "montant": -5}),
            "zone inconnue": api.post(self.URL_ADMIN, {"zone": "lune", "commune": "Man", "montant": 1000}),
            "zone non modifiable": api.patch(url_defaut, {"zone": "hors_abidjan"}),
            "commune non modifiable": api.patch(url_defaut, {"commune": "Plateau"}),
            "défaut non désactivable": api.patch(url_defaut, {"est_actif": False}),
        }
        for libelle, reponse in cas.items():
            with self.subTest(libelle):
                self.assertEqual(reponse.status_code, 400, reponse.data)
        self.defaut_abidjan.refresh_from_db()
        self.assertEqual((self.defaut_abidjan.zone, self.defaut_abidjan.commune, self.defaut_abidjan.est_actif),
                         ("abidjan", "", True))
        self.assertEqual(TarifLivraison.objects.count(), 15)

    def test_zone_unique_par_commune_en_base(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            TarifLivraison.objects.create(zone="hors_abidjan", commune="Port Bouet", montant=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TarifLivraison.objects.create(zone="abidjan", commune="", montant=1)

    def test_pas_de_suppression_par_l_api(self):
        r = self.api(self.admin).delete(f"{self.URL_ADMIN}{self.defaut_abidjan.pk}/")
        self.assertEqual(r.status_code, 405)
        self.assertTrue(TarifLivraison.objects.filter(pk=self.defaut_abidjan.pk).exists())
