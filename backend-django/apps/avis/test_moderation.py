from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from rest_framework.throttling import SimpleRateThrottle

from apps.core.tests import taux_de_production
from apps.utilisateurs.models import Role, StatutKYC
from apps.vendeurs.models import Boutique

from . import services
from .models import Avis, DecisionModeration, ReponseVendeur, SignalementAvis
from .tests import AvisTestCase, creer_achat, creer_boutique, creer_compte, creer_produit, url_avis

URL_RECUS = "/api/avis/recus/"
URL_FILE = "/api/avis/moderation/signalements/"


def url_reponse(avis):
    return f"{url_avis(avis)}reponse/"


def url_signaler(avis):
    return f"{url_avis(avis)}signaler/"


def url_masquer(avis):
    return f"/api/avis/moderation/avis/{avis.pk}/masquer/"


def url_retablir(avis):
    return f"/api/avis/moderation/avis/{avis.pk}/retablir/"


def url_rejeter(signalement):
    return f"/api/avis/moderation/signalements/{signalement.pk}/rejeter/"


def url_produit(produit):
    return f"/api/avis/produit/{produit.pk}/"


def signaler_en_base(avis, auteur=None, motif=SignalementAvis.Motif.SPAM):
    return SignalementAvis.objects.create(avis=avis, auteur=auteur or creer_compte(), motif=motif)


# =====================================================================
# RÉPONSE DU VENDEUR
# =====================================================================

class ReponseVendeurTests(AvisTestCase):
    def setUp(self):
        super().setUp()
        self.auteur_avis = creer_compte()
        self.avis = self.avis_publie(self.auteur_avis, note=2)

    def repondre(self, utilisateur, avis=None, contenu="Merci pour votre retour."):
        self.client.force_authenticate(user=utilisateur)
        return self.client.post(url_reponse(avis or self.avis), {"contenu": contenu}, format="json")

    def test_le_proprietaire_repond_et_la_reponse_est_horodatee(self):
        reponse = self.repondre(self.vendeur)
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertEqual(reponse.data["contenu"], "Merci pour votre retour.")
        self.assertTrue(reponse.data["date_creation"])
        enregistree = ReponseVendeur.objects.get()
        self.assertEqual((enregistree.avis, enregistree.auteur), (self.avis, self.vendeur))

    def test_une_seule_reponse_par_avis(self):
        self.assertEqual(self.repondre(self.vendeur).status_code, 201)
        second = self.repondre(self.vendeur, contenu="Une autre réponse.")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(ReponseVendeur.objects.get().contenu, "Merci pour votre retour.")

    def test_contrainte_en_base_une_reponse_par_avis(self):
        ReponseVendeur.objects.create(avis=self.avis, auteur=self.vendeur, contenu="Première")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReponseVendeur.objects.create(avis=self.avis, auteur=self.vendeur, contenu="Seconde")

    def test_la_reponse_est_modifiable(self):
        self.repondre(self.vendeur)
        self.client.force_authenticate(user=self.vendeur)
        reponse = self.client.patch(url_reponse(self.avis), {"contenu": "Réponse corrigée."}, format="json")
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertEqual(reponse.data["contenu"], "Réponse corrigée.")
        enregistree = ReponseVendeur.objects.get()
        self.assertEqual(enregistree.contenu, "Réponse corrigée.")
        self.assertGreater(enregistree.date_modification, enregistree.date_creation)

    def test_modifier_sans_reponse_existante(self):
        self.client.force_authenticate(user=self.vendeur)
        self.assertEqual(self.client.patch(url_reponse(self.avis), {"contenu": "x"}, format="json").status_code, 404)

    def test_contenu_obligatoire_et_borne(self):
        for contenu in ("", "   ", "x" * 2001):
            with self.subTest(longueur=len(contenu)):
                self.assertEqual(self.repondre(self.vendeur, contenu=contenu).status_code, 400)
        self.client.force_authenticate(user=self.vendeur)
        self.assertEqual(self.client.post(url_reponse(self.avis), {}, format="json").status_code, 400)
        self.assertEqual(ReponseVendeur.objects.count(), 0)

    def test_droits_pour_repondre(self):
        autre_boutique = creer_boutique()
        non_valide = creer_compte(Role.VENDEUR, statut_kyc=StatutKYC.NON_SOUMIS)
        refuses = {
            "auteur de l'avis": self.auteur_avis,
            "autre client": creer_compte(),
            "acheteur du même produit": self.acheteur,
            "autre vendeur": autre_boutique.proprietaire,
            "vendeur non validé": non_valide,
            "livreur": creer_compte(Role.LIVREUR),
            "support": creer_compte(Role.SUPPORT),
            "modérateur": creer_compte(Role.MODERATEUR),
            "admin": creer_compte(Role.ADMIN),
        }
        for nom, utilisateur in refuses.items():
            with self.subTest(nom):
                self.assertEqual(self.repondre(utilisateur).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post(url_reponse(self.avis), {"contenu": "x"}, format="json").status_code, 401)
        self.assertEqual(ReponseVendeur.objects.count(), 0)

    def test_droits_pour_modifier(self):
        self.repondre(self.vendeur)
        for nom, utilisateur in {
            "autre vendeur": creer_boutique().proprietaire, "auteur de l'avis": self.auteur_avis,
            "admin": creer_compte(Role.ADMIN),
        }.items():
            with self.subTest(nom):
                self.client.force_authenticate(user=utilisateur)
                self.assertEqual(
                    self.client.patch(url_reponse(self.avis), {"contenu": "Piraté"}, format="json").status_code, 403,
                )
        self.assertEqual(ReponseVendeur.objects.get().contenu, "Merci pour votre retour.")

    def test_boutique_suspendue_gelee(self):
        Boutique.objects.filter(pk=self.boutique.pk).update(est_suspendue=True)
        self.assertEqual(self.repondre(self.vendeur).status_code, 403)

    def test_avis_masque_ou_supprime_introuvable(self):
        for statut in (Avis.Statut.MASQUE, Avis.Statut.SUPPRIME):
            Avis.objects.filter(pk=self.avis.pk).update(statut=statut)
            with self.subTest(statut):
                self.assertEqual(self.repondre(self.vendeur).status_code, 404)

    def test_avis_inconnu(self):
        self.client.force_authenticate(user=self.vendeur)
        reponse = self.client.post(
            "/api/avis/00000000-0000-4000-8000-000000000000/reponse/", {"contenu": "x"}, format="json",
        )
        self.assertEqual(reponse.status_code, 404)

    def test_la_reponse_est_visible_du_public_et_de_l_auteur(self):
        self.repondre(self.vendeur)
        self.client.force_authenticate(user=None)
        publique = self.client.get(url_produit(self.produit)).json()["results"][0]
        self.assertEqual(publique["reponse_vendeur"]["contenu"], "Merci pour votre retour.")
        self.client.force_authenticate(user=self.auteur_avis)
        mine = self.client.get(url_avis(self.avis)).json()
        self.assertEqual(mine["reponse_vendeur"]["contenu"], "Merci pour votre retour.")

    def test_sans_reponse_le_champ_est_nul(self):
        self.client.force_authenticate(user=self.auteur_avis)
        self.assertIsNone(self.client.get(url_avis(self.avis)).json()["reponse_vendeur"])

    def test_une_reponse_ne_change_pas_les_moyennes(self):
        avant = self.produit_a_jour().note_moyenne
        self.repondre(self.vendeur)
        self.assertEqual(self.produit_a_jour().note_moyenne, avant)

    def test_avis_recus_par_la_boutique(self):
        autre = creer_boutique()
        _, variante = creer_produit(autre)
        self.avis_publie(variante=variante)
        sans_reponse = self.avis_publie()
        self.repondre(self.vendeur)

        self.client.force_authenticate(user=self.vendeur)
        tous = self.client.get(URL_RECUS).json()
        self.assertEqual(tous["count"], 2)
        self.assertEqual({ligne["id"] for ligne in tous["results"]}, {str(self.avis.pk), str(sans_reponse.pk)})
        filtres = self.client.get(URL_RECUS, {"sans_reponse": "true"}).json()
        self.assertEqual([ligne["id"] for ligne in filtres["results"]], [str(sans_reponse.pk)])

        self.client.force_authenticate(user=autre.proprietaire)
        self.assertEqual(self.client.get(URL_RECUS).json()["count"], 1)

    def test_avis_recus_reserve_aux_vendeurs_valides(self):
        for utilisateur in (self.auteur_avis, creer_compte(Role.MODERATEUR), creer_compte(Role.ADMIN)):
            self.client.force_authenticate(user=utilisateur)
            self.assertEqual(self.client.get(URL_RECUS).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL_RECUS).status_code, 401)

    def test_ecritures_limitees(self):
        cache.clear()
        taux = taux_de_production()
        n = int(taux["avis_ecriture"].split("/")[0])
        self.repondre(self.vendeur)
        self.client.force_authenticate(user=self.vendeur)
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [
                self.client.patch(url_reponse(self.avis), {"contenu": "Merci"}, format="json").status_code
                for _ in range(n + 1)
            ]
        # La réponse initiale a déjà consommé une écriture de la fenêtre.
        self.assertEqual(codes[-1], 429)


# =====================================================================
# LECTURE PUBLIQUE
# =====================================================================

class AvisPublicsTests(AvisTestCase):
    def test_public_sans_authentification_et_sans_donnees_personnelles(self):
        avis = self.avis_publie(note=4)
        reponse = self.client.get(url_produit(self.produit))
        self.assertEqual(reponse.status_code, 200)
        ligne = reponse.json()["results"][0]
        self.assertEqual(
            set(ligne), {"id", "auteur", "note", "commentaire", "est_modifie", "date_creation", "reponse_vendeur"},
        )
        self.assertEqual(ligne["id"], str(avis.pk))
        self.assertEqual(ligne["auteur"], "Prenom N.")
        self.assertNotIn(avis.client.email, reponse.content.decode())

    def test_seuls_les_avis_publies_les_plus_recents_d_abord(self):
        ancien = self.avis_publie()
        recent = self.avis_publie()
        Avis.objects.filter(pk=ancien.pk).update(date_creation=recent.date_creation - timedelta(days=3))
        masque = self.avis_publie()
        supprime = self.avis_publie()
        Avis.objects.filter(pk=masque.pk).update(statut=Avis.Statut.MASQUE)
        Avis.objects.filter(pk=supprime.pk).update(statut=Avis.Statut.SUPPRIME)
        ids = [ligne["id"] for ligne in self.client.get(url_produit(self.produit)).json()["results"]]
        self.assertEqual(ids, [str(recent.pk), str(ancien.pk)])

    def test_produit_inconnu_ou_non_visible(self):
        self.assertEqual(self.client.get("/api/avis/produit/999999/").status_code, 404)
        self.produit.est_actif = False
        self.produit.desactive_par = "vendeur"
        self.produit.save(update_fields=["est_actif", "desactive_par"])
        self.assertEqual(self.client.get(url_produit(self.produit)).status_code, 404)

    def test_pagination(self):
        for _ in range(22):
            self.avis_publie()
        page1 = self.client.get(url_produit(self.produit)).json()
        page2 = self.client.get(url_produit(self.produit), {"page": 2}).json()
        self.assertEqual((page1["count"], len(page1["results"]), len(page2["results"])), (22, 20, 2))

    def test_nombre_de_requetes_constant(self):
        def requetes():
            with CaptureQueriesContext(connection) as capture:
                self.client.get(url_produit(self.produit))
            return len(capture)

        avis = self.avis_publie()
        ReponseVendeur.objects.create(avis=avis, auteur=self.vendeur, contenu="Merci")
        une_ligne = requetes()
        for _ in range(4):
            autre = self.avis_publie()
            ReponseVendeur.objects.create(avis=autre, auteur=self.vendeur, contenu="Merci")
        self.assertEqual(requetes(), une_ligne)


# =====================================================================
# SIGNALEMENT
# =====================================================================

class SignalementAvisTests(AvisTestCase):
    def setUp(self):
        super().setUp()
        self.auteur_avis = creer_compte()
        self.avis = self.avis_publie(self.auteur_avis, note=1)

    def signaler(self, utilisateur, avis=None, motif="spam", **champs):
        self.client.force_authenticate(user=utilisateur)
        return self.client.post(url_signaler(avis or self.avis), {"motif": motif, **champs}, format="json")

    def test_tout_utilisateur_connecte_signale(self):
        for role in (Role.CLIENT, Role.LIVREUR, Role.SUPPORT, Role.MODERATEUR):
            with self.subTest(role=role):
                reponse = self.signaler(creer_compte(role), motif="injurieux", commentaire="Propos haineux.")
                self.assertEqual(reponse.status_code, 201, reponse.data)
                self.assertEqual(reponse.data["statut"], "ouvert")
        self.assertEqual(SignalementAvis.objects.count(), 4)

    def test_le_vendeur_de_la_boutique_peut_signaler(self):
        self.assertEqual(self.signaler(self.vendeur, motif="faux_avis").status_code, 201)

    def test_anonyme_refuse(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post(url_signaler(self.avis), {"motif": "spam"}, format="json").status_code, 401)

    def test_jamais_sur_son_propre_avis(self):
        self.assertEqual(self.signaler(self.auteur_avis).status_code, 403)
        self.assertEqual(SignalementAvis.objects.count(), 0)

    def test_un_seul_signalement_par_utilisateur_et_par_avis(self):
        utilisateur = creer_compte()
        self.assertEqual(self.signaler(utilisateur).status_code, 201)
        self.assertEqual(self.signaler(utilisateur, motif="autre").status_code, 409)
        self.assertEqual(SignalementAvis.objects.count(), 1)
        self.assertEqual(self.signaler(creer_compte()).status_code, 201)

    def test_contrainte_en_base_d_unicite(self):
        utilisateur = creer_compte()
        signaler_en_base(self.avis, utilisateur)
        with self.assertRaises(IntegrityError), transaction.atomic():
            signaler_en_base(self.avis, utilisateur, SignalementAvis.Motif.AUTRE)

    def test_motif_en_liste_fermee_et_commentaire_borne(self):
        utilisateur = creer_compte()
        self.assertEqual(self.signaler(utilisateur, motif="je-n-aime-pas").status_code, 400)
        self.assertEqual(self.signaler(utilisateur, motif="").status_code, 400)
        self.assertEqual(self.signaler(utilisateur, commentaire="x" * 501).status_code, 400)
        self.client.force_authenticate(user=utilisateur)
        self.assertEqual(self.client.post(url_signaler(self.avis), {}, format="json").status_code, 400)
        self.assertEqual(SignalementAvis.objects.count(), 0)
        self.assertEqual(self.signaler(utilisateur, commentaire="").status_code, 201)

    def test_tous_les_motifs_acceptes(self):
        for valeur, _ in SignalementAvis.Motif.choices:
            with self.subTest(valeur):
                self.assertEqual(self.signaler(creer_compte(), motif=valeur).status_code, 201)

    def test_avis_masque_supprime_ou_inconnu(self):
        utilisateur = creer_compte()
        for statut in (Avis.Statut.MASQUE, Avis.Statut.SUPPRIME):
            Avis.objects.filter(pk=self.avis.pk).update(statut=statut)
            with self.subTest(statut):
                self.assertEqual(self.signaler(utilisateur).status_code, 404)
        self.client.force_authenticate(user=utilisateur)
        inconnu = "/api/avis/00000000-0000-4000-8000-000000000000/signaler/"
        self.assertEqual(self.client.post(inconnu, {"motif": "spam"}, format="json").status_code, 404)

    def test_signaler_ne_masque_ni_ne_change_les_moyennes(self):
        avant = self.produit_a_jour().note_moyenne
        for _ in range(3):
            self.signaler(creer_compte())
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, Avis.Statut.PUBLIE)
        self.assertEqual(self.produit_a_jour().note_moyenne, avant)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url_produit(self.produit)).json()["count"], 1)

    def test_signalements_limites(self):
        cache.clear()
        taux = taux_de_production()
        n = int(taux["avis_signalement"].split("/")[0])
        signalant = creer_compte()
        avis = [self.avis_publie() for _ in range(n + 1)]
        self.client.force_authenticate(user=signalant)
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [
                self.client.post(url_signaler(a), {"motif": "spam"}, format="json").status_code for a in avis
            ]
        self.assertEqual(codes, [201] * n + [429])


# =====================================================================
# MODÉRATION
# =====================================================================

class ModerationAvisTestCase(AvisTestCase):
    """Deux avis sur le même produit : 1/5 (signalé) et 5/5. Moyenne 3,00."""

    def setUp(self):
        super().setUp()
        self.moderateur = creer_compte(Role.MODERATEUR)
        self.admin = creer_compte(Role.ADMIN)
        self.auteur_avis = creer_compte()
        self.avis = self.avis_publie(self.auteur_avis, note=1)
        self.bon_avis = self.avis_publie(note=5)
        self.signalant1, self.signalant2 = creer_compte(), creer_compte()
        self.signalement1 = signaler_en_base(self.avis, self.signalant1, SignalementAvis.Motif.INJURIEUX)
        self.signalement2 = signaler_en_base(self.avis, self.signalant2, SignalementAvis.Motif.SPAM)

    def decider(self, url, utilisateur, motif="Propos injurieux envers la boutique."):
        self.client.force_authenticate(user=utilisateur)
        corps = {} if motif is None else {"motif": motif}
        return self.client.post(url, corps, format="json")

    def masquer(self, utilisateur=None, motif="Propos injurieux envers la boutique."):
        return self.decider(url_masquer(self.avis), utilisateur or self.moderateur, motif)

    def etat_produit(self):
        produit = self.produit_a_jour()
        self.boutique.refresh_from_db()
        return (
            produit.note_moyenne, produit.nombre_avis, self.boutique.nombre_avis, self.boutique.nombre_avis_positifs,
        )


class DroitsModerationTests(ModerationAvisTestCase):
    def test_table_de_verite_des_roles(self):
        cas = {
            "client": creer_compte(), "auteur de l'avis": self.auteur_avis, "signalant": self.signalant1,
            "vendeur propriétaire": self.vendeur, "autre vendeur": creer_boutique().proprietaire,
            "livreur": creer_compte(Role.LIVREUR), "support": creer_compte(Role.SUPPORT),
            "client avec is_staff": creer_compte(is_staff=True),
        }
        for nom, utilisateur in cas.items():
            self.client.force_authenticate(user=utilisateur)
            with self.subTest(nom):
                self.assertEqual(self.client.get(URL_FILE).status_code, 403)
                self.assertEqual(self.decider(url_masquer(self.avis), utilisateur).status_code, 403)
                self.assertEqual(self.decider(url_retablir(self.avis), utilisateur).status_code, 403)
                self.assertEqual(self.decider(url_rejeter(self.signalement1), utilisateur).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL_FILE).status_code, 401)
        self.assertEqual(self.client.post(url_masquer(self.avis), {"motif": "x"}, format="json").status_code, 401)
        self.assertEqual(Avis.objects.get(pk=self.avis.pk).statut, Avis.Statut.PUBLIE)
        self.assertEqual(DecisionModeration.objects.count(), 0)
        self.assertEqual(SignalementAvis.objects.filter(statut="ouvert").count(), 2)

    def test_moderateur_admin_et_super_admin_autorises(self):
        for role in (Role.MODERATEUR, Role.ADMIN, Role.SUPER_ADMIN):
            self.client.force_authenticate(user=creer_compte(role))
            with self.subTest(role):
                self.assertEqual(self.client.get(URL_FILE).status_code, 200)

    def test_le_moderateur_n_a_aucun_autre_pouvoir(self):
        self.client.force_authenticate(user=self.moderateur)
        for url in (
            "/api/utilisateurs/administration/", "/api/utilisateurs/administration/connexions/", URL_RECUS,
        ):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 403)
        reponse = self.client.post(url_reponse(self.avis), {"contenu": "x"}, format="json")
        self.assertEqual(reponse.status_code, 403)
        self.assertFalse(self.moderateur.is_staff)


class FileDeModerationTests(ModerationAvisTestCase):
    def test_signalements_ouverts_les_plus_anciens_d_abord(self):
        autre_avis = self.avis_publie(note=3)
        recent = signaler_en_base(autre_avis, motif=SignalementAvis.Motif.HORS_SUJET)
        SignalementAvis.objects.filter(pk=self.signalement2.pk).update(
            date_creation=self.signalement1.date_creation - timedelta(days=1),
        )
        self.client.force_authenticate(user=self.moderateur)
        reponse = self.client.get(URL_FILE)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(
            [ligne["id"] for ligne in reponse.json()["results"]],
            [self.signalement2.pk, self.signalement1.pk, recent.pk],
        )

    def test_representation_sans_donnee_personnelle(self):
        self.client.force_authenticate(user=self.admin)
        ligne = self.client.get(URL_FILE).json()["results"][0]
        self.assertEqual(
            set(ligne),
            {"id", "avis", "signalant_id", "motif", "commentaire", "statut", "date_creation", "date_traitement"},
        )
        self.assertEqual(
            set(ligne["avis"]),
            {"id", "produit_id", "produit_nom", "boutique_nom", "client_id", "note", "commentaire", "statut"},
        )
        contenu = self.client.get(URL_FILE).content.decode()
        self.assertNotIn(self.signalant1.email, contenu)
        self.assertNotIn(self.auteur_avis.email, contenu)

    def test_filtre_statut_et_valeur_invalide(self):
        self.decider(url_rejeter(self.signalement1), self.moderateur)
        self.client.force_authenticate(user=self.moderateur)
        ouverts = self.client.get(URL_FILE).json()["results"]
        self.assertEqual([ligne["id"] for ligne in ouverts], [self.signalement2.pk])
        rejetes = self.client.get(URL_FILE, {"statut": "rejete"}).json()["results"]
        self.assertEqual([ligne["id"] for ligne in rejetes], [self.signalement1.pk])
        self.assertEqual(self.client.get(URL_FILE, {"statut": "inconnu"}).status_code, 400)

    def test_un_avis_supprime_par_son_auteur_quitte_la_file(self):
        Avis.objects.filter(pk=self.avis.pk).update(statut=Avis.Statut.SUPPRIME)
        self.client.force_authenticate(user=self.moderateur)
        self.assertEqual(self.client.get(URL_FILE).json()["count"], 0)

    def test_pagination(self):
        for _ in range(21):
            signaler_en_base(self.bon_avis)
        self.client.force_authenticate(user=self.moderateur)
        reponse = self.client.get(URL_FILE).json()
        self.assertEqual((reponse["count"], len(reponse["results"])), (23, 20))


class MasquerRetablirTests(ModerationAvisTestCase):
    def test_masquer_retire_l_avis_du_public_et_recalcule_les_moyennes(self):
        self.assertEqual(self.etat_produit(), (Decimal("3.00"), 2, 2, 1))

        reponse = self.masquer()

        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, Avis.Statut.MASQUE)
        self.assertEqual(self.etat_produit(), (Decimal("5.00"), 1, 1, 1))
        self.client.force_authenticate(user=None)
        ids = [ligne["id"] for ligne in self.client.get(url_produit(self.produit)).json()["results"]]
        self.assertEqual(ids, [str(self.bon_avis.pk)])

    def test_le_dernier_avis_masque_vide_la_moyenne(self):
        Avis.objects.filter(pk=self.bon_avis.pk).update(statut=Avis.Statut.SUPPRIME)
        services.recalculer_agregats(self.produit.pk, self.boutique.pk)
        self.masquer()
        self.assertEqual(self.etat_produit(), (None, 0, 0, 0))

    def test_l_auteur_voit_que_son_avis_est_masque(self):
        self.masquer()
        self.client.force_authenticate(user=self.auteur_avis)
        liste = self.client.get("/api/avis/mes-avis/").json()["results"]
        self.assertEqual({ligne["id"]: ligne["statut"] for ligne in liste}[str(self.avis.pk)], "masque")
        self.assertEqual(self.client.get(url_avis(self.avis)).json()["statut"], "masque")
        # Et il ne peut plus le modifier ni le supprimer.
        self.assertEqual(self.client.patch(url_avis(self.avis), {"note": 5}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url_avis(self.avis)).status_code, 403)

    def test_la_reponse_du_vendeur_disparait_avec_l_avis(self):
        ReponseVendeur.objects.create(avis=self.avis, auteur=self.vendeur, contenu="Merci")
        self.masquer()
        self.client.force_authenticate(user=self.vendeur)
        self.assertEqual(self.client.get(URL_RECUS).json()["count"], 1)
        self.assertNotIn("Merci", self.client.get(url_produit(self.produit)).content.decode())

    def test_masquer_accepte_les_signalements_ouverts_et_les_relie_a_la_decision(self):
        self.masquer()
        decision = DecisionModeration.objects.get()
        for signalement in (self.signalement1, self.signalement2):
            signalement.refresh_from_db()
            self.assertEqual(signalement.statut, SignalementAvis.Statut.ACCEPTE)
            self.assertEqual(signalement.decision, decision)
            self.assertIsNotNone(signalement.date_traitement)
        self.client.force_authenticate(user=self.moderateur)
        self.assertEqual(self.client.get(URL_FILE).json()["count"], 0)
        self.assertEqual(self.client.get(URL_FILE, {"statut": "accepte"}).json()["count"], 2)

    def test_chaque_decision_est_tracee(self):
        masquer = self.masquer(self.moderateur, "Propos injurieux envers la boutique.")
        retablir = self.decider(url_retablir(self.avis), self.admin, "Erreur de modération, avis conforme.")
        self.assertEqual((masquer.status_code, retablir.status_code), (201, 201))

        premiere, seconde = DecisionModeration.objects.order_by("id")
        self.assertEqual(
            (premiere.avis, premiere.moderateur, premiere.type_decision, premiere.motif),
            (self.avis, self.moderateur, "masquer", "Propos injurieux envers la boutique."),
        )
        self.assertEqual(
            (seconde.avis, seconde.moderateur, seconde.type_decision, seconde.motif),
            (self.avis, self.admin, "retablir", "Erreur de modération, avis conforme."),
        )
        self.assertLessEqual(premiere.date_creation, seconde.date_creation)
        self.assertEqual(
            set(masquer.json()), {"id", "avis_id", "moderateur_id", "type_decision", "motif", "date_creation"},
        )
        self.assertEqual(masquer.json()["moderateur_id"], self.moderateur.pk)

    def test_motif_obligatoire(self):
        for motif in (None, "", "   ", "x" * 1001):
            with self.subTest(motif=motif):
                self.assertEqual(self.masquer(motif=motif).status_code, 400)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, Avis.Statut.PUBLIE)
        self.assertEqual(self.etat_produit(), (Decimal("3.00"), 2, 2, 1))
        self.assertEqual(DecisionModeration.objects.count(), 0)

    def test_contrainte_en_base_motif_non_vide(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DecisionModeration.objects.create(
                avis=self.avis, moderateur=self.moderateur, type_decision=DecisionModeration.TypeDecision.MASQUER, motif="",
            )

    def test_masquer_deux_fois_ou_un_avis_supprime(self):
        self.assertEqual(self.masquer().status_code, 201)
        self.assertEqual(self.masquer().status_code, 409)
        Avis.objects.filter(pk=self.bon_avis.pk).update(statut=Avis.Statut.SUPPRIME)
        self.assertEqual(self.decider(url_masquer(self.bon_avis), self.moderateur).status_code, 409)
        self.assertEqual(DecisionModeration.objects.count(), 1)

    def test_avis_inconnu(self):
        self.client.force_authenticate(user=self.moderateur)
        for action in ("masquer", "retablir"):
            reponse = self.client.post(
                f"/api/avis/moderation/avis/00000000-0000-4000-8000-000000000000/{action}/",
                {"motif": "x"}, format="json",
            )
            self.assertEqual(reponse.status_code, 404)

    def test_retablir_remet_l_avis_dans_le_public_et_les_moyennes(self):
        self.masquer()
        reponse = self.decider(url_retablir(self.avis), self.moderateur, "Avis conforme après relecture.")
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, Avis.Statut.PUBLIE)
        self.assertEqual(self.etat_produit(), (Decimal("3.00"), 2, 2, 1))
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url_produit(self.produit)).json()["count"], 2)

    def test_retablir_exige_un_avis_masque_et_un_motif(self):
        self.assertEqual(self.decider(url_retablir(self.avis), self.moderateur).status_code, 409)
        self.masquer()
        self.assertEqual(self.decider(url_retablir(self.avis), self.moderateur, None).status_code, 400)
        self.assertEqual(self.decider(url_retablir(self.avis), self.moderateur, " ").status_code, 400)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, Avis.Statut.MASQUE)

    def test_un_signalant_ne_peut_pas_resignaler_apres_le_retablissement(self):
        self.masquer()
        self.decider(url_retablir(self.avis), self.moderateur)
        self.client.force_authenticate(user=self.signalant1)
        reponse = self.client.post(url_signaler(self.avis), {"motif": "spam"}, format="json")
        self.assertEqual(reponse.status_code, 409)

    def test_moyennes_identiques_a_un_calcul_direct(self):
        for note in (2, 3, 4):
            self.avis_publie(note=note)
        self.masquer()
        publies = Avis.objects.filter(produit=self.produit, statut=Avis.Statut.PUBLIE)
        self.assertEqual(self.etat_produit()[1], publies.count())
        self.assertEqual(self.etat_produit()[0], Decimal(sum(a.note for a in publies)) / publies.count())


class RejeterSignalementTests(ModerationAvisTestCase):
    def test_rejeter_laisse_l_avis_publie(self):
        reponse = self.decider(url_rejeter(self.signalement1), self.moderateur, "Avis conforme, simple désaccord.")
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.signalement1.refresh_from_db()
        self.assertEqual(self.signalement1.statut, SignalementAvis.Statut.REJETE)
        self.assertIsNotNone(self.signalement1.date_traitement)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, Avis.Statut.PUBLIE)
        self.assertEqual(self.etat_produit(), (Decimal("3.00"), 2, 2, 1))
        # Les autres signalements de l'avis restent ouverts.
        self.signalement2.refresh_from_db()
        self.assertEqual(self.signalement2.statut, SignalementAvis.Statut.OUVERT)

    def test_la_decision_est_tracee(self):
        self.decider(url_rejeter(self.signalement1), self.admin, "Avis conforme, simple désaccord.")
        decision = DecisionModeration.objects.get()
        self.assertEqual(
            (decision.avis, decision.moderateur, decision.type_decision, decision.motif),
            (self.avis, self.admin, "rejeter_signalement", "Avis conforme, simple désaccord."),
        )
        self.signalement1.refresh_from_db()
        self.assertEqual(self.signalement1.decision, decision)

    def test_motif_obligatoire(self):
        for motif in (None, "", "  "):
            with self.subTest(motif=motif):
                self.assertEqual(self.decider(url_rejeter(self.signalement1), self.moderateur, motif).status_code, 400)
        self.signalement1.refresh_from_db()
        self.assertEqual(self.signalement1.statut, SignalementAvis.Statut.OUVERT)
        self.assertEqual(DecisionModeration.objects.count(), 0)

    def test_signalement_deja_traite_ou_inconnu(self):
        self.assertEqual(self.decider(url_rejeter(self.signalement1), self.moderateur).status_code, 201)
        self.assertEqual(self.decider(url_rejeter(self.signalement1), self.moderateur).status_code, 409)
        self.masquer()
        self.assertEqual(self.decider(url_rejeter(self.signalement2), self.moderateur).status_code, 409)
        self.assertEqual(self.decider(url_rejeter(SignalementAvis(pk=999999)), self.moderateur).status_code, 404)

    def test_contrainte_en_base_traitement_coherent(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SignalementAvis.objects.filter(pk=self.signalement1.pk).update(statut=SignalementAvis.Statut.REJETE)


class AdminDjangoAvisTests(ModerationAvisTestCase):
    def test_modeles_en_lecture_seule(self):
        requete = RequestFactory().get("/")
        requete.user = creer_compte(Role.ADMIN, is_staff=True, is_superuser=True)
        for modele in (Avis, ReponseVendeur, SignalementAvis, DecisionModeration):
            modele_admin = admin.site._registry[modele]
            with self.subTest(modele.__name__):
                self.assertFalse(modele_admin.has_add_permission(requete))
                self.assertFalse(modele_admin.has_change_permission(requete))
                self.assertFalse(modele_admin.has_delete_permission(requete))
                self.assertTrue(modele_admin.has_view_permission(requete))
