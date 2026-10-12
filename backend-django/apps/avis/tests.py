import itertools
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Avg
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework.throttling import SimpleRateThrottle

from apps.catalogue.models import Produit, VarianteProduit
from apps.commandes.models import Commande, CommandeItem
from apps.core.tests import taux_de_production
from apps.livraison.models import Livraison
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique

from .models import Avis
from . import services

URL_CREER = "/api/avis/"
URL_A_NOTER = "/api/avis/a-noter/"
URL_MES_AVIS = "/api/avis/mes-avis/"
MOT_DE_PASSE = "TestPassword123!"

_compteur = itertools.count(1)


def url_avis(avis):
    return f"/api/avis/{avis.pk}/"


def creer_compte(role=Role.CLIENT, email_verifie=True, **extra):
    numero = next(_compteur)
    if role == Role.VENDEUR:
        extra.setdefault("statut_kyc", StatutKYC.VALIDE)
    return Utilisateur.objects.create_user(
        email=f"{role}{numero}@anitche.ci", password=MOT_DE_PASSE, nom="Nom", prenom="Prenom",
        role=role, email_verifie=email_verifie, **extra,
    )


def creer_boutique(nom=None):
    proprietaire = creer_compte(Role.VENDEUR)
    boutique = Boutique.objects.create(proprietaire=proprietaire, nom=nom or f"Boutique {next(_compteur)}")
    return boutique


def creer_produit(boutique, nom="Robe Baoulé"):
    produit = Produit.objects.create(boutique=boutique, nom=nom, prix_base=Decimal("20000.00"))
    variante = VarianteProduit.objects.create(produit=produit, nom="Taille M", prix=Decimal("20000.00"))
    return produit, variante


def creer_achat(client, variante, statut=Commande.Status.LIVREE, livree_il_y_a=timedelta(days=1)):
    """Ligne de commande de `client` ; livrée il y a `livree_il_y_a` si la
    commande est livrée (None : fiche de livraison sans date)."""
    boutique = variante.produit.boutique
    commande = Commande.objects.create(
        boutique=boutique, client=client, montant_total=Decimal("20000.00"), status=statut,
    )
    article = CommandeItem.objects.create(
        commande=commande, variante=variante, nom_produit=variante.produit.nom,
        prix_unitaire=Decimal("20000.00"), quantite=1,
    )
    if statut == Commande.Status.LIVREE:
        Livraison.objects.create(
            commande=commande, status=Livraison.Status.LIVREE,
            date_livraison=None if livree_il_y_a is None else timezone.now() - livree_il_y_a,
        )
    return article


class AvisTestCase(APITestCase):
    """Une boutique, un produit, un client acheteur d'un article livré hier."""

    def setUp(self):
        self.boutique = creer_boutique()
        self.vendeur = self.boutique.proprietaire
        self.produit, self.variante = creer_produit(self.boutique)
        self.acheteur = creer_compte()
        self.article = creer_achat(self.acheteur, self.variante)

    def noter(self, utilisateur, article=None, note=5, **champs):
        self.client.force_authenticate(user=utilisateur)
        corps = {"commande_item_id": str((article or self.article).pk), "note": note, **champs}
        return self.client.post(URL_CREER, corps, format="json")

    def avis_publie(self, client=None, note=5, variante=None):
        """Avis créé par l'API, pour préparer les tests de modification."""
        client = client or creer_compte()
        article = creer_achat(client, variante or self.variante)
        reponse = self.noter(client, article, note)
        self.assertEqual(reponse.status_code, 201, reponse.data)
        return Avis.objects.get(pk=reponse.data["id"])

    def produit_a_jour(self):
        self.produit.refresh_from_db()
        return self.produit


# =====================================================================
# ÉLIGIBILITÉ
# =====================================================================

class EligibiliteAvisTests(AvisTestCase):

    def test_acheteur_d_un_article_livre_publie_son_avis(self):
        reponse = self.noter(self.acheteur, note=4, commentaire="Très belle robe.")
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        self.assertEqual(reponse.data["note"], 4)
        self.assertEqual(reponse.data["statut"], "publie")
        self.assertFalse(reponse.data["est_modifie"])
        avis = Avis.objects.get()
        self.assertEqual((avis.client, avis.produit, avis.boutique, avis.commande_item),
                         (self.acheteur, self.produit, self.boutique, self.article))

    def test_note_seule_sans_commentaire(self):
        reponse = self.noter(self.acheteur, note=3)
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertEqual(reponse.data["commentaire"], "")

    def test_visiteur_anonyme_refuse(self):
        reponse = self.client.post(URL_CREER, {"commande_item_id": str(self.article.pk), "note": 5}, format="json")
        self.assertEqual(reponse.status_code, 401)
        self.assertEqual(Avis.objects.count(), 0)

    def test_email_non_verifie_refuse(self):
        non_verifie = creer_compte(email_verifie=False)
        article = creer_achat(non_verifie, self.variante)
        reponse = self.noter(non_verifie, article)
        self.assertEqual(reponse.status_code, 403)
        self.assertEqual(reponse.data["errors"]["code"], ["email_non_verifie"])
        self.assertEqual(Avis.objects.count(), 0)

    def test_article_d_un_autre_client_introuvable(self):
        """A ne peut pas noter à partir de l'achat de B, et la réponse ne
        distingue pas « inexistant » de « à quelqu'un d'autre »."""
        intrus = creer_compte()
        reponse = self.noter(intrus, self.article)
        self.assertEqual(reponse.status_code, 404)
        inconnu = self.client.post(
            URL_CREER, {"commande_item_id": "00000000-0000-4000-8000-000000000000", "note": 5}, format="json",
        )
        self.assertEqual(inconnu.status_code, 404)
        self.assertEqual(reponse.data["detail"], inconnu.data["detail"])
        self.assertEqual(Avis.objects.count(), 0)

    def test_commande_non_livree_refusee(self):
        for statut in (Commande.Status.CREEE, Commande.Status.CONFIRMEE, Commande.Status.PREPARATION,
                       Commande.Status.EXPEDIEE, Commande.Status.ANNULEE):
            with self.subTest(statut=statut):
                client = creer_compte()
                article = creer_achat(client, self.variante, statut=statut)
                reponse = self.noter(client, article)
                self.assertEqual(reponse.status_code, 400, reponse.data)
        self.assertEqual(Avis.objects.count(), 0)

    def test_statut_de_la_commande_prime_sur_la_date_de_livraison(self):
        """Même avec une fiche datée dans le délai, une commande qui n'est
        pas « livrée » n'ouvre aucun avis."""
        for statut in (Commande.Status.CONFIRMEE, Commande.Status.EXPEDIEE, Commande.Status.ANNULEE):
            with self.subTest(statut=statut):
                client = creer_compte()
                article = creer_achat(client, self.variante, statut=statut)
                Livraison.objects.create(
                    commande=article.commande, status=Livraison.Status.EXPEDIEE,
                    date_livraison=timezone.now() - timedelta(days=1),
                )
                reponse = self.noter(client, article)
                self.assertEqual(reponse.status_code, 400, reponse.data)
                self.assertIn("commande livrée", reponse.data["detail"])
        self.assertEqual(Avis.objects.count(), 0)

    def test_livraison_sans_date_refusee(self):
        client = creer_compte()
        article = creer_achat(client, self.variante, livree_il_y_a=None)
        self.assertEqual(self.noter(client, article).status_code, 400)

    def test_delai_pour_noter(self):
        with self.settings(AVIS_DELAI_JOURS=60):
            dans_le_delai = creer_compte()
            article = creer_achat(dans_le_delai, self.variante, livree_il_y_a=timedelta(days=59))
            self.assertEqual(self.noter(dans_le_delai, article).status_code, 201)

            hors_delai = creer_compte()
            article = creer_achat(hors_delai, self.variante, livree_il_y_a=timedelta(days=61))
            reponse = self.noter(hors_delai, article)
            self.assertEqual(reponse.status_code, 400)
            self.assertIn("60 jours", reponse.data["detail"])

    def test_delai_reglable(self):
        client = creer_compte()
        article = creer_achat(client, self.variante, livree_il_y_a=timedelta(days=10))
        with self.settings(AVIS_DELAI_JOURS=5):
            self.assertEqual(self.noter(client, article).status_code, 400)
        with self.settings(AVIS_DELAI_JOURS=15):
            self.assertEqual(self.noter(client, article).status_code, 201)

    def test_proprietaire_ne_note_pas_sa_propre_boutique(self):
        article = creer_achat(self.vendeur, self.variante)
        reponse = self.noter(self.vendeur, article)
        self.assertEqual(reponse.status_code, 403)
        self.assertEqual(Avis.objects.count(), 0)

    def test_vendeur_note_les_produits_d_une_autre_boutique(self):
        autre_boutique = creer_boutique()
        _, variante = creer_produit(autre_boutique, "Pagne")
        article = creer_achat(self.vendeur, variante)
        self.assertEqual(self.noter(self.vendeur, article).status_code, 201)

    def test_un_avis_par_client_et_produit(self):
        self.assertEqual(self.noter(self.acheteur).status_code, 201)
        # Même produit, autre variante, autre commande.
        variante_xl = VarianteProduit.objects.create(produit=self.produit, nom="Taille XL", prix=Decimal("21000.00"))
        second_article = creer_achat(self.acheteur, variante_xl)
        reponse = self.noter(self.acheteur, second_article, note=1)
        self.assertEqual(reponse.status_code, 409)
        self.assertEqual(Avis.objects.count(), 1)

    def test_meme_article_deux_fois_refuse(self):
        self.assertEqual(self.noter(self.acheteur).status_code, 201)
        self.assertEqual(self.noter(self.acheteur).status_code, 409)

    def test_apres_suppression_pas_de_nouvel_avis(self):
        avis = self.avis_publie(self.acheteur)
        self.client.force_authenticate(user=self.acheteur)
        self.assertEqual(self.client.delete(url_avis(avis)).status_code, 204)
        reponse = self.noter(self.acheteur, avis.commande_item)
        self.assertEqual(reponse.status_code, 409)
        self.assertEqual(Avis.objects.filter(client=self.acheteur).count(), 1)

    def test_deux_clients_notent_le_meme_produit(self):
        self.assertEqual(self.noter(self.acheteur).status_code, 201)
        autre = creer_compte()
        self.assertEqual(self.noter(autre, creer_achat(autre, self.variante)).status_code, 201)
        self.assertEqual(self.produit_a_jour().nombre_avis, 2)

    def test_produit_desactive_apres_l_achat_reste_notable(self):
        Produit.objects.filter(pk=self.produit.pk).update(est_actif=False, desactive_par="vendeur")
        self.assertEqual(self.noter(self.acheteur).status_code, 201)

    def test_champs_invalides(self):
        cas = {
            "note nulle": {"note": 0},
            "note trop haute": {"note": 6},
            "note négative": {"note": -1},
            "note décimale": {"note": 4.5},
            "note texte": {"note": "excellent"},
            "note absente": {"note": None},
            "commentaire trop long": {"note": 5, "commentaire": "x" * 2001},
            "identifiant mal formé": {"note": 5, "commande_item_id": "pas-un-uuid"},
        }
        for nom, corps in cas.items():
            with self.subTest(cas=nom):
                self.client.force_authenticate(user=self.acheteur)
                donnees = {"commande_item_id": str(self.article.pk), **corps}
                reponse = self.client.post(URL_CREER, donnees, format="json")
                self.assertEqual(reponse.status_code, 400, reponse.data)
        self.assertEqual(Avis.objects.count(), 0)

    def test_commentaire_a_la_limite_accepte(self):
        self.assertEqual(self.noter(self.acheteur, commentaire="x" * 2000).status_code, 201)

    def test_champs_serveur_ignores(self):
        """Statut, client et boutique ne se choisissent jamais dans le corps."""
        autre = creer_compte()
        reponse = self.noter(self.acheteur, statut="masque", client=autre.pk, boutique=999, est_modifie=True)
        self.assertEqual(reponse.status_code, 201, reponse.data)
        avis = Avis.objects.get()
        self.assertEqual((avis.statut, avis.client, avis.boutique, avis.est_modifie),
                         ("publie", self.acheteur, self.boutique, False))


class ContraintesBaseAvisTests(AvisTestCase):
    """Les règles d'unicité et de note tiennent aussi sans passer par l'API."""

    def creer(self, **champs):
        valeurs = dict(client=self.acheteur, produit=self.produit, boutique=self.boutique,
                       commande_item=self.article, note=5)
        valeurs.update(champs)
        with transaction.atomic():
            return Avis.objects.create(**valeurs)

    def test_un_avis_par_client_et_produit_en_base(self):
        self.creer()
        autre_article = creer_achat(self.acheteur, self.variante)
        with self.assertRaises(IntegrityError):
            self.creer(commande_item=autre_article)

    def test_une_ligne_de_commande_une_seule_fois_en_base(self):
        self.creer()
        autre_produit, _ = creer_produit(self.boutique, "Autre")
        with self.assertRaises(IntegrityError):
            self.creer(produit=autre_produit)

    def test_note_entre_1_et_5_en_base(self):
        for note in (0, 6):
            with self.subTest(note=note), self.assertRaises(IntegrityError):
                self.creer(note=note)

    def test_agregats_coherents_en_base(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Produit.objects.filter(pk=self.produit.pk).update(nombre_avis=2, note_moyenne=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Produit.objects.filter(pk=self.produit.pk).update(nombre_avis=0, note_moyenne=Decimal("4.00"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Produit.objects.filter(pk=self.produit.pk).update(nombre_avis=1, note_moyenne=Decimal("5.01"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Boutique.objects.filter(pk=self.boutique.pk).update(nombre_avis=1, nombre_avis_positifs=2)


# =====================================================================
# AGRÉGATS
# =====================================================================

class AgregatsAvisTests(AvisTestCase):

    def test_produit_sans_avis(self):
        produit = self.produit_a_jour()
        self.assertIsNone(produit.note_moyenne)
        self.assertEqual(produit.nombre_avis, 0)

    def test_note_moyenne_et_compteurs(self):
        self.avis_publie(note=5)
        self.avis_publie(note=4)
        self.avis_publie(note=2)
        produit = self.produit_a_jour()
        self.assertEqual(produit.nombre_avis, 3)
        self.assertEqual(produit.note_moyenne, Decimal("3.67"))
        self.boutique.refresh_from_db()
        self.assertEqual((self.boutique.nombre_avis, self.boutique.nombre_avis_positifs), (3, 2))

    def test_arrondi_au_centieme_superieur_a_la_moitie(self):
        for note in (5, 5, 5, 5, 5, 4, 4, 4):
            self.avis_publie(note=note)
        # 37 / 8 = 4,625 : arrondi à 4,63.
        self.assertEqual(self.produit_a_jour().note_moyenne, Decimal("4.63"))

    def test_compteurs_d_une_boutique_cumulent_ses_produits(self):
        autre_produit, autre_variante = creer_produit(self.boutique, "Pagne")
        self.avis_publie(note=5)
        self.avis_publie(note=1, variante=autre_variante)
        self.boutique.refresh_from_db()
        self.assertEqual((self.boutique.nombre_avis, self.boutique.nombre_avis_positifs), (2, 1))
        self.assertEqual(self.produit_a_jour().nombre_avis, 1)
        autre_produit.refresh_from_db()
        self.assertEqual(autre_produit.note_moyenne, Decimal("1.00"))

    def test_un_autre_produit_n_est_pas_affecte(self):
        autre_boutique = creer_boutique()
        autre_produit, autre_variante = creer_produit(autre_boutique, "Pagne")
        self.avis_publie(note=5)
        autre_produit.refresh_from_db()
        self.assertEqual(autre_produit.nombre_avis, 0)
        autre_boutique.refresh_from_db()
        self.assertEqual(autre_boutique.nombre_avis, 0)

    def test_modification_recalcule(self):
        avis = self.avis_publie(note=5)
        self.avis_publie(note=3)
        self.client.force_authenticate(user=avis.client)
        self.assertEqual(self.client.patch(url_avis(avis), {"note": 1}, format="json").status_code, 200)
        produit = self.produit_a_jour()
        self.assertEqual(produit.note_moyenne, Decimal("2.00"))
        self.boutique.refresh_from_db()
        self.assertEqual(self.boutique.nombre_avis_positifs, 0)

    def test_suppression_retire_l_avis_de_la_note(self):
        avis = self.avis_publie(note=5)
        self.avis_publie(note=3)
        self.client.force_authenticate(user=avis.client)
        self.assertEqual(self.client.delete(url_avis(avis)).status_code, 204)
        produit = self.produit_a_jour()
        self.assertEqual((produit.nombre_avis, produit.note_moyenne), (1, Decimal("3.00")))

    def test_derniere_suppression_remet_la_note_a_vide(self):
        avis = self.avis_publie(note=5)
        self.client.force_authenticate(user=avis.client)
        self.client.delete(url_avis(avis))
        produit = self.produit_a_jour()
        self.assertEqual((produit.nombre_avis, produit.note_moyenne), (0, None))
        self.boutique.refresh_from_db()
        self.assertEqual((self.boutique.nombre_avis, self.boutique.nombre_avis_positifs), (0, 0))

    def test_un_avis_masque_n_est_pas_compte(self):
        avis = self.avis_publie(note=5)
        self.avis_publie(note=3)
        Avis.objects.filter(pk=avis.pk).update(statut=Avis.Statut.MASQUE)
        services.recalculer_agregats(self.produit.pk, self.boutique.pk)
        produit = self.produit_a_jour()
        self.assertEqual((produit.nombre_avis, produit.note_moyenne), (1, Decimal("3.00")))

    def test_compte_ou_produit_avec_avis_ne_se_supprime_pas(self):
        avis = self.avis_publie()
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError), transaction.atomic():
            avis.client.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.produit.delete()


# =====================================================================
# MODIFICATION ET SUPPRESSION
# =====================================================================

class ModificationAvisTests(AvisTestCase):

    def setUp(self):
        super().setUp()
        self.avis = self.avis_publie(self.acheteur, note=4)
        self.client.force_authenticate(user=self.acheteur)

    def test_modifier_la_note_et_le_commentaire(self):
        reponse = self.client.patch(url_avis(self.avis), {"note": 2, "commentaire": "Déçu"}, format="json")
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertEqual((reponse.data["note"], reponse.data["commentaire"], reponse.data["est_modifie"]),
                         (2, "Déçu", True))

    def test_modifier_le_commentaire_seul_garde_la_note(self):
        reponse = self.client.patch(url_avis(self.avis), {"commentaire": "Mieux que prévu"}, format="json")
        self.assertEqual(reponse.status_code, 200)
        self.avis.refresh_from_db()
        self.assertEqual((self.avis.note, self.avis.est_modifie), (4, True))

    def test_corps_vide_refuse(self):
        self.assertEqual(self.client.patch(url_avis(self.avis), {}, format="json").status_code, 400)
        self.avis.refresh_from_db()
        self.assertFalse(self.avis.est_modifie)

    def test_delai_de_modification(self):
        with self.settings(AVIS_DELAI_MODIFICATION_JOURS=14):
            Avis.objects.filter(pk=self.avis.pk).update(date_creation=timezone.now() - timedelta(days=13))
            self.assertEqual(self.client.patch(url_avis(self.avis), {"note": 3}, format="json").status_code, 200)
            Avis.objects.filter(pk=self.avis.pk).update(date_creation=timezone.now() - timedelta(days=15))
            reponse = self.client.patch(url_avis(self.avis), {"note": 1}, format="json")
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("14 jours", reponse.data["detail"])
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.note, 3)

    def test_avis_masque_ni_modifiable_ni_supprimable(self):
        Avis.objects.filter(pk=self.avis.pk).update(statut=Avis.Statut.MASQUE)
        self.assertEqual(self.client.patch(url_avis(self.avis), {"note": 1}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url_avis(self.avis)).status_code, 403)
        self.avis.refresh_from_db()
        self.assertEqual((self.avis.statut, self.avis.note), ("masque", 4))

    def test_avis_d_un_autre_client_introuvable(self):
        intrus = creer_compte()
        self.client.force_authenticate(user=intrus)
        self.assertEqual(self.client.get(url_avis(self.avis)).status_code, 404)
        self.assertEqual(self.client.patch(url_avis(self.avis), {"note": 1}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url_avis(self.avis)).status_code, 404)
        self.avis.refresh_from_db()
        self.assertEqual((self.avis.statut, self.avis.note, self.avis.est_modifie), ("publie", 4, False))

    def test_suppression_douce(self):
        self.assertEqual(self.client.delete(url_avis(self.avis)).status_code, 204)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, "supprime")
        # Supprimé : il n'existe plus pour son auteur.
        self.assertEqual(self.client.get(url_avis(self.avis)).status_code, 404)
        self.assertEqual(self.client.delete(url_avis(self.avis)).status_code, 404)
        self.assertEqual(self.client.patch(url_avis(self.avis), {"note": 1}, format="json").status_code, 404)

    def test_champs_serveur_ignores_a_la_modification(self):
        self.client.patch(url_avis(self.avis), {"note": 5, "statut": "masque", "produit_id": 999}, format="json")
        self.avis.refresh_from_db()
        self.assertEqual((self.avis.statut, self.avis.produit), ("publie", self.produit))

    def test_le_detail_donne_la_fin_du_delai_de_modification(self):
        reponse = self.client.get(url_avis(self.avis))
        self.assertEqual(reponse.status_code, 200)
        attendu = (self.avis.date_creation + timedelta(days=14)).isoformat()
        self.assertEqual(reponse.data["modifiable_jusqu_au"], attendu)


# =====================================================================
# LISTES
# =====================================================================

class ListesAvisTests(AvisTestCase):

    def test_a_noter_liste_les_articles_livres_non_notes(self):
        self.client.force_authenticate(user=self.acheteur)
        reponse = self.client.get(URL_A_NOTER)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual([ligne["commande_item_id"] for ligne in reponse.data["results"]], [str(self.article.pk)])
        ligne = reponse.data["results"][0]
        self.assertEqual((ligne["produit_id"], ligne["produit_slug"], ligne["boutique_nom"]),
                         (self.produit.pk, self.produit.slug, self.boutique.nom))

    def test_a_noter_exclut_les_cas_non_eligibles(self):
        client = self.acheteur
        # Non livrée, hors délai, produit déjà noté, propre boutique : absents.
        creer_achat(client, self.variante, statut=Commande.Status.EXPEDIEE)
        hors_delai = creer_achat(client, self.variante, livree_il_y_a=timedelta(days=90))
        deja_note_produit, deja_note_variante = creer_produit(self.boutique, "Pagne")
        article_note = creer_achat(client, deja_note_variante)
        self.assertEqual(self.noter(client, article_note).status_code, 201)
        creer_achat(self.vendeur, self.variante)
        # Le même produit, une autre variante : exclu aussi (un avis par produit).
        variante_bis = VarianteProduit.objects.create(
            produit=deja_note_produit, nom="Bis", prix=Decimal("1000.00"),
        )
        article_bis = creer_achat(client, variante_bis)

        self.client.force_authenticate(user=client)
        ids = {ligne["commande_item_id"] for ligne in self.client.get(URL_A_NOTER).data["results"]}
        self.assertEqual(ids, {str(self.article.pk)})
        self.assertNotIn(str(hors_delai.pk), ids)
        self.assertNotIn(str(article_bis.pk), ids)

    def test_a_noter_ne_montre_pas_les_achats_d_un_autre(self):
        autre = creer_compte()
        self.client.force_authenticate(user=autre)
        self.assertEqual(self.client.get(URL_A_NOTER).data["count"], 0)

    def test_a_noter_exclut_la_propre_boutique(self):
        creer_achat(self.vendeur, self.variante)
        self.client.force_authenticate(user=self.vendeur)
        self.assertEqual(self.client.get(URL_A_NOTER).data["count"], 0)

    def test_mes_avis_sont_les_miens_sans_les_supprimes(self):
        mien = self.avis_publie(self.acheteur, note=5)
        supprime = self.avis_publie(self.acheteur, note=2, variante=creer_produit(self.boutique, "P2")[1])
        self.client.force_authenticate(user=self.acheteur)
        self.client.delete(url_avis(supprime))
        autre = self.avis_publie(creer_compte(), note=1)

        self.client.force_authenticate(user=self.acheteur)
        reponse = self.client.get(URL_MES_AVIS)
        self.assertEqual(reponse.status_code, 200)
        ids = [ligne["id"] for ligne in reponse.data["results"]]
        self.assertEqual(ids, [str(mien.pk)])
        self.assertNotIn(str(autre.pk), ids)

    def test_listes_reservees_aux_comptes_connectes(self):
        self.assertEqual(self.client.get(URL_A_NOTER).status_code, 401)
        self.assertEqual(self.client.get(URL_MES_AVIS).status_code, 401)
        avis = self.avis_publie()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url_avis(avis)).status_code, 401)


# =====================================================================
# TABLE DE VÉRITÉ RÔLE × ROUTE
# =====================================================================

ROLES = [Role.CLIENT, Role.VENDEUR, Role.LIVREUR, Role.SUPPORT, Role.MODERATEUR, Role.ADMIN, Role.SUPER_ADMIN]


class TableDeVeriteRolesAvisTests(AvisTestCase):
    """L'éligibilité repose sur l'achat, pas sur le rôle : tout compte qui a
    reçu un article peut le noter (sauf le propriétaire de la boutique) ; nul
    ne touche à l'avis d'un autre, administrateur compris."""

    def test_anonyme(self):
        avis = self.avis_publie()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL_A_NOTER).status_code, 401)
        self.assertEqual(self.client.get(URL_MES_AVIS).status_code, 401)
        self.assertEqual(self.client.post(URL_CREER, {}, format="json").status_code, 401)
        self.assertEqual(self.client.get(url_avis(avis)).status_code, 401)
        self.assertEqual(self.client.patch(url_avis(avis), {"note": 1}, format="json").status_code, 401)
        self.assertEqual(self.client.delete(url_avis(avis)).status_code, 401)

    def test_acheteur_de_chaque_role_peut_noter(self):
        for role in ROLES:
            with self.subTest(role=role):
                compte = creer_compte(role)
                article = creer_achat(compte, self.variante)
                self.assertEqual(self.noter(compte, article).status_code, 201)
                self.client.force_authenticate(user=compte)
                self.assertEqual(self.client.get(URL_MES_AVIS).data["count"], 1)

    def test_non_acheteur_de_chaque_role_introuvable(self):
        for role in ROLES:
            with self.subTest(role=role):
                compte = creer_compte(role)
                self.assertEqual(self.noter(compte, self.article).status_code, 404)

    def test_avis_d_un_autre_intouchable_pour_chaque_role(self):
        avis = self.avis_publie()
        for role in ROLES:
            with self.subTest(role=role):
                compte = creer_compte(role)
                self.client.force_authenticate(user=compte)
                self.assertEqual(self.client.get(url_avis(avis)).status_code, 404)
                self.assertEqual(self.client.patch(url_avis(avis), {"note": 1}, format="json").status_code, 404)
                self.assertEqual(self.client.delete(url_avis(avis)).status_code, 404)
        avis.refresh_from_db()
        self.assertEqual((avis.statut, avis.note), ("publie", 5))

    def test_proprietaire_de_la_boutique_refuse_meme_acheteur(self):
        article = creer_achat(self.vendeur, self.variante)
        self.assertEqual(self.noter(self.vendeur, article).status_code, 403)

    def test_listes_ouvertes_a_chaque_role_connecte(self):
        for role in ROLES:
            with self.subTest(role=role):
                self.client.force_authenticate(user=creer_compte(role))
                self.assertEqual(self.client.get(URL_A_NOTER).status_code, 200)
                self.assertEqual(self.client.get(URL_MES_AVIS).status_code, 200)


# =====================================================================
# LIMITES DE DÉBIT
# =====================================================================

class LimitesDeDebitAvisTests(AvisTestCase):
    """Limites dédiées, avec les vraies valeurs de base.py."""

    def test_publication_limitee(self):
        cache.clear()
        taux = taux_de_production()
        n = int(taux["avis_creation"].split("/")[0])
        client = creer_compte()
        articles = [creer_achat(client, creer_produit(self.boutique, f"P{i}")[1]) for i in range(n + 1)]
        self.client.force_authenticate(user=client)
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [
                self.client.post(URL_CREER, {"commande_item_id": str(a.pk), "note": 4}, format="json").status_code
                for a in articles
            ]
            # La lecture n'est pas comptée dans la limite de publication.
            self.assertEqual(self.client.get(URL_MES_AVIS).status_code, 200)
        self.assertEqual(codes, [201] * n + [429])

    def test_ecritures_limitees(self):
        cache.clear()
        taux = taux_de_production()
        n = int(taux["avis_ecriture"].split("/")[0])
        avis = self.avis_publie(self.acheteur, note=3)
        self.client.force_authenticate(user=self.acheteur)
        with patch.object(SimpleRateThrottle, "THROTTLE_RATES", taux):
            codes = [self.client.patch(url_avis(avis), {"note": 4}, format="json").status_code for _ in range(n + 1)]
            self.assertEqual(self.client.get(url_avis(avis)).status_code, 200)
        self.assertEqual(codes, [200] * n + [429])


# =====================================================================
# CONCURRENCE
# =====================================================================

@skipUnless(connection.vendor == "postgresql", "select_for_update exige PostgreSQL.")
class ConcurrenceAvisTests(TransactionTestCase):
    """Des avis simultanés sont appliqués l'un après l'autre : note moyenne et
    compteurs égaux à ce que donnerait un calcul séquentiel."""

    def setUp(self):
        self.boutique = creer_boutique()
        self.produit, self.variante = creer_produit(self.boutique)

    def lancer(self, travaux):
        """Exécute les `travaux` (callables → code HTTP) en même temps."""
        barriere = threading.Barrier(len(travaux))
        codes = [None] * len(travaux)
        erreurs = []

        def executer(index, travail):
            try:
                barriere.wait()
                codes[index] = travail()
            except Exception as erreur:  # noqa: BLE001 - remonté par l'assertion
                erreurs.append(erreur)
            finally:
                connection.close()

        fils = [threading.Thread(target=executer, args=(i, t)) for i, t in enumerate(travaux)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join()
        self.assertEqual(erreurs, [])
        return codes

    def publier(self, client, article, note):
        def travail():
            api = APIClient()
            api.force_authenticate(user=client)
            return api.post(
                URL_CREER, {"commande_item_id": str(article.pk), "note": note}, format="json",
            ).status_code
        return travail

    def verifier_agregats(self, produit):
        produit.refresh_from_db()
        publies = Avis.objects.filter(produit=produit, statut=Avis.Statut.PUBLIE)
        self.assertEqual(produit.nombre_avis, publies.count())
        moyenne = publies.aggregate(m=Avg("note"))["m"]
        attendu = None if moyenne is None else Decimal(str(moyenne)).quantize(Decimal("0.01"))
        self.assertEqual(produit.note_moyenne, attendu)

    def test_deux_avis_simultanes_sur_le_meme_produit(self):
        premier, second = creer_compte(), creer_compte()
        codes = self.lancer([
            self.publier(premier, creer_achat(premier, self.variante), 5),
            self.publier(second, creer_achat(second, self.variante), 2),
        ])
        self.assertEqual(codes, [201, 201])
        self.produit.refresh_from_db()
        self.assertEqual((self.produit.nombre_avis, self.produit.note_moyenne), (2, Decimal("3.50")))
        self.boutique.refresh_from_db()
        self.assertEqual((self.boutique.nombre_avis, self.boutique.nombre_avis_positifs), (2, 1))

    def test_huit_avis_simultanes(self):
        notes = [5, 4, 3, 2, 1, 5, 4, 3]
        travaux = []
        for note in notes:
            client = creer_compte()
            travaux.append(self.publier(client, creer_achat(client, self.variante), note))
        self.assertEqual(self.lancer(travaux), [201] * 8)
        self.produit.refresh_from_db()
        self.assertEqual((self.produit.nombre_avis, self.produit.note_moyenne), (8, Decimal("3.38")))
        self.boutique.refresh_from_db()
        self.assertEqual((self.boutique.nombre_avis, self.boutique.nombre_avis_positifs), (8, 4))

    def test_double_envoi_du_meme_client_un_seul_avis(self):
        client = creer_compte()
        article = creer_achat(client, self.variante)
        codes = self.lancer([self.publier(client, article, 5), self.publier(client, article, 1)])
        self.assertEqual(sorted(codes), [201, 409])
        self.assertEqual(Avis.objects.filter(client=client).count(), 1)
        self.verifier_agregats(self.produit)

    def test_deux_articles_du_meme_produit_un_seul_avis(self):
        client = creer_compte()
        variante_bis = VarianteProduit.objects.create(produit=self.produit, nom="Bis", prix=Decimal("1000.00"))
        codes = self.lancer([
            self.publier(client, creer_achat(client, self.variante), 5),
            self.publier(client, creer_achat(client, variante_bis), 1),
        ])
        self.assertEqual(sorted(codes), [201, 409])
        self.assertEqual(Avis.objects.filter(client=client).count(), 1)
        self.verifier_agregats(self.produit)

    def test_produits_differents_d_une_meme_boutique(self):
        """Six avis sur deux produits de la même boutique : pas d'interblocage
        (verrou produit puis boutique), compteurs de boutique exacts."""
        autre_produit, autre_variante = creer_produit(self.boutique, "Pagne")
        travaux = []
        for index in range(6):
            client = creer_compte()
            variante = self.variante if index % 2 == 0 else autre_variante
            travaux.append(self.publier(client, creer_achat(client, variante), 5 if index % 3 else 2))
        self.assertEqual(self.lancer(travaux), [201] * 6)
        self.verifier_agregats(self.produit)
        self.verifier_agregats(autre_produit)
        self.boutique.refresh_from_db()
        self.assertEqual(self.boutique.nombre_avis, 6)
        self.assertEqual(self.boutique.nombre_avis_positifs, Avis.objects.filter(
            boutique=self.boutique, note__gte=4).count())

    def test_modifications_et_publications_simultanees(self):
        anciens = []
        for note in (5, 5, 5):
            client = creer_compte()
            self.assertEqual(self.publier(client, creer_achat(client, self.variante), note)(), 201)
            anciens.append(Avis.objects.get(client=client))

        def modifier(avis, note):
            def travail():
                api = APIClient()
                api.force_authenticate(user=avis.client)
                return api.patch(url_avis(avis), {"note": note}, format="json").status_code
            return travail

        nouveau = creer_compte()
        codes = self.lancer([
            modifier(anciens[0], 1),
            modifier(anciens[1], 2),
            self.publier(nouveau, creer_achat(nouveau, self.variante), 3),
        ])
        self.assertEqual(codes, [200, 200, 201])
        self.verifier_agregats(self.produit)
        self.produit.refresh_from_db()
        self.assertEqual(self.produit.note_moyenne, Decimal("2.75"))
