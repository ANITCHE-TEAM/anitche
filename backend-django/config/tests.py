"""Format d'erreur commun de l'API (config/exceptions.py), vérifié module
par module sur les erreurs principales : toute réponse d'erreur a la forme
{success: false, status_code, detail: str, errors: {clé: [str, ...]}}.

Seules exceptions documentées : les notifications des fournisseurs de
paiement (apps.paiements.views._VueNotification), qui répondent au format
attendu par le fournisseur.
"""

import importlib
import inspect
import io
import uuid
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404, QueryDict
from django.test import SimpleTestCase, override_settings
from PIL import Image
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.fields import empty
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from apps.catalogue.models import ImageProduit, Produit, VarianteProduit
from apps.commandes.models import Commande, GroupeCommande
from apps.livraison.models import TarifLivraison
from apps.paiements.models import Paiement
from apps.support.models import SupportTicket, TicketMessage
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique

from .exceptions import (
    MESSAGE_ERREUR_INTERNE,
    MESSAGE_RESSOURCE_INTROUVABLE,
    custom_exception_handler,
    normaliser_erreurs,
)


MOT_DE_PASSE = "MotDePasseSolide123!"


def creer_utilisateur(email, **champs):
    champs.setdefault("email_verifie", True)
    return Utilisateur.objects.create_user(email=email, password=MOT_DE_PASSE, nom="Kouassi", prenom="Awa", **champs)


class VerificationFormatMixin:
    def verifier_format(self, reponse, code_http):
        self.assertEqual(reponse.status_code, code_http, reponse.data)
        donnees = reponse.json()
        self.assertEqual(set(donnees), {"success", "status_code", "detail", "errors"}, donnees)
        self.assertIs(donnees["success"], False)
        self.assertEqual(donnees["status_code"], code_http)
        self.assertIsInstance(donnees["detail"], str)
        self.assertTrue(donnees["detail"])
        self.assertIsInstance(donnees["errors"], dict)
        for cle, messages in donnees["errors"].items():
            self.assertIsInstance(cle, str)
            self.assertIsInstance(messages, list, (cle, messages))
            self.assertTrue(messages)
            for message in messages:
                self.assertIsInstance(message, str)
        return donnees


class NormalisationErreursTests(SimpleTestCase):
    """Formes produites par DRF → errors toujours clé → liste de messages."""

    def test_erreur_simple(self):
        self.assertEqual(normaliser_erreurs({"detail": "Introuvable."}), ("Introuvable.", {}))

    def test_champs_et_imbrication(self):
        detail, erreurs = normaliser_erreurs({
            "prix": ["Le prix doit être un entier."],
            "adresse_livraison": {"commune": ["Commune inconnue."]},
            "items": [{}, {"quantite": ["Trop grand."]}],
            "nom": "Déjà utilisé.",
        })
        self.assertEqual(detail, "prix: Le prix doit être un entier.")
        self.assertEqual(erreurs, {
            "prix": ["Le prix doit être un entier."],
            "adresse_livraison.commune": ["Commune inconnue."],
            "items.1.quantite": ["Trop grand."],
            "nom": ["Déjà utilisé."],
        })

    def test_liste_nue_devient_non_field_errors(self):
        detail, erreurs = normaliser_erreurs(["Stock insuffisant."])
        self.assertEqual(detail, "Stock insuffisant.")
        self.assertEqual(erreurs, {"non_field_errors": ["Stock insuffisant."]})

    def test_detail_en_liste_devient_une_chaine(self):
        self.assertEqual(normaliser_erreurs({"detail": ["Refusé."]}), ("Refusé.", {}))

    def test_code_machine_conserve_dans_errors(self):
        detail, erreurs = normaliser_erreurs({"detail": "Vérifiez votre email.", "code": "email_non_verifie"})
        self.assertEqual(detail, "Vérifiez votre email.")
        self.assertEqual(erreurs, {"code": ["email_non_verifie"]})

    def test_validation_sans_champ(self):
        detail, erreurs = normaliser_erreurs(ValidationError("Panier vide.").detail)
        self.assertEqual((detail, erreurs), ("Panier vide.", {"non_field_errors": ["Panier vide."]}))

    def test_erreur_500(self):
        reponse = custom_exception_handler(RuntimeError("boom"), {"view": None})
        self.assertEqual(reponse.status_code, 500)
        self.assertEqual(reponse.data, {
            "success": False, "status_code": 500, "detail": MESSAGE_ERREUR_INTERNE, "errors": {},
        })


class FormatErreursParModuleTests(VerificationFormatMixin, APITestCase):
    """Erreurs principales de chaque module, au format commun."""

    def setUp(self):
        self.client_user = creer_utilisateur("client@anitche.ci")
        self.admin = creer_utilisateur("admin@anitche.ci", role=Role.ADMIN)
        self.vendeur = creer_utilisateur("vendeur@anitche.ci", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.boutique = Boutique.objects.create(proprietaire=self.vendeur, nom="Boutique Format")

    def en_tant_que(self, utilisateur):
        self.client.force_authenticate(utilisateur)

    # --- Transverses ---------------------------------------------------

    def test_401_non_authentifie(self):
        self.verifier_format(self.client.get("/api/utilisateurs/profil/"), 401)

    def test_401_jeton_invalide(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer jeton-invalide")
        self.verifier_format(self.client.get("/api/utilisateurs/profil/"), 401)

    def test_405_methode_non_autorisee(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.delete("/api/utilisateurs/profil/"), 405)

    def test_429_limite_de_debit(self):
        cache.clear()
        taux = {**ScopedRateThrottle.THROTTLE_RATES, "passeport_verification": "1/hour"}
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", taux):
            self.client.get("/api/passeports/verifier/INCONNU/")
            donnees = self.verifier_format(self.client.get("/api/passeports/verifier/INCONNU/"), 429)
        self.assertEqual(donnees["errors"], {})
        cache.clear()

    # --- Utilisateurs ----------------------------------------------------

    def test_utilisateurs_validation_inscription(self):
        donnees = self.verifier_format(self.client.post("/api/utilisateurs/inscription/", {}, format="json"), 400)
        self.assertIn("email", donnees["errors"])

    def test_utilisateurs_connexion_refusee(self):
        reponse = self.client.post(
            "/api/utilisateurs/connexion/", {"email": "client@anitche.ci", "password": "faux"}, format="json",
        )
        self.verifier_format(reponse, 401)

    def test_utilisateurs_otp_sans_code_en_attente(self):
        self.en_tant_que(self.client_user)
        reponse = self.client.post(
            "/api/utilisateurs/verification-otp/", {"code": "123456", "type_usage": "changement_email"}, format="json",
        )
        self.assertEqual(self.verifier_format(reponse, 400)["errors"], {})

    def test_utilisateurs_email_deja_verifie(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.post("/api/utilisateurs/renvoyer-code-inscription/"), 400)

    def test_utilisateurs_mot_de_passe_oublie_code_invalide(self):
        reponse = self.client.post("/api/utilisateurs/mot-de-passe-oublie/confirmer/", {
            "email": "inconnu@anitche.ci", "code": "123456",
            "nouveau_password": "NouveauMotDePasse123!", "confirmation_password": "NouveauMotDePasse123!",
        }, format="json")
        self.verifier_format(reponse, 400)

    def test_utilisateurs_deconnexion_sans_refresh(self):
        donnees = self.verifier_format(self.client.post("/api/utilisateurs/deconnexion/", {}, format="json"), 400)
        self.assertIn("refresh", donnees["errors"])

    def test_utilisateurs_deconnexion_refresh_invalide(self):
        reponse = self.client.post("/api/utilisateurs/deconnexion/", {"refresh": "invalide"}, format="json")
        self.assertIn("refresh", self.verifier_format(reponse, 400)["errors"])

    def test_utilisateurs_email_non_verifie_code_machine(self):
        self.en_tant_que(creer_utilisateur("nonverifie@anitche.ci", email_verifie=False))
        reponse = self.client.post("/api/commandes/valider-panier/", {}, format="json")
        self.assertEqual(self.verifier_format(reponse, 403)["errors"], {"code": ["email_non_verifie"]})

    # --- Vendeurs ----------------------------------------------------------

    def test_vendeurs_403_et_404(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.get("/api/vendeurs/administration/demandes/"), 403)
        self.en_tant_que(self.admin)
        reponse = self.client.post(
            "/api/vendeurs/administration/demandes/999999/valider/", {"commentaire": "ok"}, format="json",
        )
        self.verifier_format(reponse, 404)

    # --- Catalogue ---------------------------------------------------------

    def test_catalogue_404_et_403(self):
        self.verifier_format(self.client.get("/api/catalogue/produits/inexistant/"), 404)
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.get("/api/catalogue/vendeur/produits/"), 403)

    def test_catalogue_validation(self):
        self.en_tant_que(self.vendeur)
        donnees = self.verifier_format(self.client.post("/api/catalogue/vendeur/produits/", {}, format="json"), 400)
        self.assertTrue(donnees["errors"])

    # --- Panier ------------------------------------------------------------

    def test_panier_validation(self):
        self.en_tant_que(self.client_user)
        reponse = self.client.post("/api/panier/panier/items/", {"variante": 999999, "quantite": 1}, format="json")
        self.assertIn("variante", self.verifier_format(reponse, 400)["errors"])

    # --- Commandes ---------------------------------------------------------

    def test_commandes_adresse_obligatoire(self):
        self.en_tant_que(self.client_user)
        reponse = self.client.post("/api/commandes/valider-panier/", {}, format="json")
        self.assertIn("adresse_livraison", self.verifier_format(reponse, 400)["errors"])

    def test_commandes_404(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.post(f"/api/commandes/{uuid.uuid4()}/annuler/"), 404)

    def test_commandes_409_transition_impossible(self):
        groupe = GroupeCommande.objects.create(client=self.client_user)
        commande = Commande.objects.create(
            groupe=groupe, boutique=self.boutique, client=self.client_user,
            montant_total=Decimal("5000"), status=Commande.Status.ANNULEE,
        )
        self.en_tant_que(self.client_user)
        reponse = self.client.post(f"/api/commandes/{commande.pk}/annuler/")
        self.assertEqual(self.verifier_format(reponse, 409)["errors"], {})

    # --- Paiements ---------------------------------------------------------

    def test_paiements_validation(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.post("/api/paiements/initier/", {}, format="json"), 400)

    def test_paiements_409(self):
        paiement = Paiement.objects.create(
            client=self.client_user, montant=Decimal("5000"), fournisseur="simule", statut=Paiement.Statut.ANNULE,
        )
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.post(f"/api/paiements/{paiement.pk}/annuler/"), 409)

    def test_paiements_502_fournisseur_indisponible(self):
        from apps.paiements.fournisseurs.base import ErreurFournisseur
        self.en_tant_que(self.client_user)
        with mock.patch("apps.paiements.views.services.initier_paiement", side_effect=ErreurFournisseur("hors ligne")):
            reponse = self.client.post(
                "/api/paiements/initier/", {"methode": "wave", "commande_id": str(uuid.uuid4())}, format="json",
            )
        self.assertEqual(self.verifier_format(reponse, 502)["errors"], {})

    def test_paiements_webhook_garde_le_format_du_fournisseur(self):
        """Exception documentée : réponse au format attendu par le fournisseur."""
        reponse = self.client.post("/api/paiements/webhook/simule/", {}, format="json")
        self.assertGreaterEqual(reponse.status_code, 400)
        self.assertNotIn("success", reponse.json())

    # --- Livraison ---------------------------------------------------------

    def test_livraison_refus_metier(self):
        self.en_tant_que(self.admin)
        reponse = self.client.post(f"/api/livraison/livreurs/{self.client_user.pk}/retirer/")
        self.assertEqual(self.verifier_format(reponse, 400)["errors"], {})

    def test_livraison_livreur_introuvable(self):
        from apps.livraison.models import Livraison
        groupe = GroupeCommande.objects.create(client=self.client_user)
        commande = Commande.objects.create(
            groupe=groupe, boutique=self.boutique, client=self.client_user, montant_total=Decimal("5000"),
        )
        livraison, _ = Livraison.objects.get_or_create(commande=commande)
        self.en_tant_que(self.admin)
        reponse = self.client.post(f"/api/livraison/{livraison.pk}/assigner/", {"livreur_id": 999999}, format="json")
        self.assertIn("livreur_id", self.verifier_format(reponse, 400)["errors"])

    # --- Retours -----------------------------------------------------------

    def test_retours_validation_et_404(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.post("/api/retours/", {}, format="json"), 400)
        self.verifier_format(self.client.get(f"/api/retours/{uuid.uuid4()}/"), 404)

    def test_retours_photo_manquante(self):
        self.en_tant_que(self.client_user)
        reponse = self.client.post(f"/api/retours/{uuid.uuid4()}/photos/", {}, format="multipart")
        self.assertIn("image", self.verifier_format(reponse, 400)["errors"])

    # --- Fidélité ----------------------------------------------------------

    def test_fidelite_coupon_inexistant(self):
        self.en_tant_que(self.client_user)
        reponse = self.client.post(
            "/api/fidelite/verifier-coupon/", {"code": "NEXISTEPAS", "montant_commande": "10000"}, format="json",
        )
        self.assertEqual(self.verifier_format(reponse, 404)["errors"], {})

    # --- Notifications -----------------------------------------------------

    def test_notifications_404(self):
        self.en_tant_que(self.client_user)
        self.verifier_format(self.client.patch(f"/api/notifications/{uuid.uuid4()}/lire/"), 404)

    # --- Support -----------------------------------------------------------

    def test_support_note_refusee(self):
        ticket = SupportTicket.objects.create(created_by=self.client_user, subject="Colis", description="Où est-il ?")
        self.en_tant_que(self.client_user)
        url = f"/api/support/tickets/{ticket.pk}/rate/"
        self.assertEqual(self.verifier_format(self.client.patch(url, {"satisfaction_rating": 5}), 400)["errors"], {})
        ticket.status = SupportTicket.Status.RESOLVED
        ticket.save()
        reponse = self.client.patch(url, {"satisfaction_rating": 9}, format="json")
        self.assertIn("satisfaction_rating", self.verifier_format(reponse, 400)["errors"])
        self.en_tant_que(self.admin)
        self.verifier_format(self.client.patch(url, {"satisfaction_rating": 5}, format="json"), 403)

    # --- Passeports QR -----------------------------------------------------

    def test_passeport_introuvable(self):
        self.verifier_format(self.client.get("/api/passeports/verifier/INCONNU/"), 404)


# =========================================================================
# 404 génériques : message français, sans nom de modèle
# =========================================================================

class Introuvable404Tests(VerificationFormatMixin, APITestCase):
    """Les 404 de get_object_or_404 (« No Commande matches the given
    query. ») deviennent un message neutre dans tous les modules ; un
    message 404 écrit par une vue est conservé."""

    def setUp(self):
        self.client_user = creer_utilisateur("client404@anitche.ci")
        self.admin = creer_utilisateur("admin404@anitche.ci", role=Role.ADMIN)
        self.vendeur = creer_utilisateur("vendeur404@anitche.ci", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        Boutique.objects.create(proprietaire=self.vendeur, nom="Boutique 404")

    def test_message_generique_dans_chaque_module(self):
        inconnu = uuid.uuid4()
        cas = [
            (None, "get", "/api/catalogue/produits/inexistant/"),
            (None, "get", "/api/vendeurs/boutiques/inexistante/"),
            ("vendeur", "get", "/api/catalogue/vendeur/produits/999999/"),
            ("admin", "post", "/api/vendeurs/administration/demandes/999999/valider/"),
            ("client", "get", f"/api/commandes/{inconnu}/"),
            ("client", "post", f"/api/commandes/{inconnu}/annuler/"),
            ("client", "get", f"/api/paiements/{inconnu}/"),
            ("client", "get", f"/api/livraison/{inconnu}/"),
            ("admin", "patch", f"/api/livraison/admin/tarifs/{inconnu}/"),
            ("client", "get", f"/api/retours/{inconnu}/"),
            ("client", "get", f"/api/support/tickets/{inconnu}/"),
            ("client", "patch", f"/api/notifications/{inconnu}/lire/"),
        ]
        comptes = {"client": self.client_user, "admin": self.admin, "vendeur": self.vendeur, None: None}
        for compte, methode, url in cas:
            with self.subTest(url=url):
                self.client.force_authenticate(comptes[compte])
                donnees = self.verifier_format(getattr(self.client, methode)(url, {}, format="json"), 404)
                self.assertEqual(donnees["detail"], MESSAGE_RESSOURCE_INTROUVABLE)
                self.assertEqual(donnees["errors"], {})

    def test_message_ecrit_par_la_vue_conserve(self):
        self.client.force_authenticate(self.client_user)
        reponse = self.client.get(f"/api/utilisateurs/kyc/{self.client_user.pk}/champ_inconnu/")
        self.assertEqual(self.verifier_format(reponse, 404)["detail"], "Document demandé inconnu.")

    def test_handler(self):
        for exception in (Http404(), Http404("No Commande matches the given query.")):
            reponse = custom_exception_handler(exception, {})
            self.assertEqual(reponse.status_code, 404)
            self.assertEqual(reponse.data["detail"], MESSAGE_RESSOURCE_INTROUVABLE)
        self.assertEqual(custom_exception_handler(NotFound("Aucune boutique."), {}).data["detail"], "Aucune boutique.")


# =========================================================================
# Multipart : un booléen absent prend la valeur par défaut, pas False
# =========================================================================

APPLICATIONS_METIER = [
    "utilisateurs", "vendeurs", "catalogue", "panier", "commandes", "paiements", "livraison",
    "retours", "fidelite", "notifications", "support", "passeport_qr",
]


ORIGINE_PORTAIL = "https://vendeur.exemple.test"


@override_settings(CORS_ALLOWED_ORIGINS=[ORIGINE_PORTAIL])
class CorsEnTetesExposesTests(APITestCase):
    """Un portail servi depuis une autre origine lit le délai d'un 429 et le
    nom d'un fichier téléchargé (django-cors-headers)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_retry_after_lisible_sur_un_429(self):
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"catalogue_public": "0/hour"}):
            reponse = self.client.get("/api/catalogue/produits/", HTTP_ORIGIN=ORIGINE_PORTAIL)
        self.assertEqual(reponse.status_code, 429)
        self.assertTrue(reponse.has_header("Retry-After"))
        self.assertEqual(reponse["Access-Control-Allow-Origin"], ORIGINE_PORTAIL)
        exposes = {nom.strip().lower() for nom in reponse["Access-Control-Expose-Headers"].split(",")}
        self.assertEqual(exposes, {"retry-after", "content-disposition"})

    def test_requete_prealable_mise_en_cache_un_jour(self):
        reponse = self.client.options(
            "/api/catalogue/produits/", HTTP_ORIGIN=ORIGINE_PORTAIL, HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )
        self.assertEqual(reponse["Access-Control-Max-Age"], "86400")

    def test_origine_non_autorisee_sans_en_tete_cors(self):
        reponse = self.client.get("/api/catalogue/produits/", HTTP_ORIGIN="https://autre.exemple.test")
        self.assertFalse(reponse.has_header("Access-Control-Allow-Origin"))
        self.assertFalse(reponse.has_header("Access-Control-Expose-Headers"))


def image_png(nom="image.png"):
    tampon = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 50, 50)).save(tampon, format="PNG")
    return SimpleUploadedFile(nom, tampon.getvalue(), content_type="image/png")


class BooleensMultipartTests(APITestCase):
    """En multipart, DRF lit par défaut un booléen absent comme False (une
    boutique créée avec son logo naîtrait fermée, un produit ou une variante
    inactifs). Un booléen absent est ignoré : valeur par défaut du modèle à
    la création, valeur actuelle conservée en mise à jour."""

    def setUp(self):
        self.vendeur = creer_utilisateur("vendeur.multipart@anitche.ci", role=Role.VENDEUR, statut_kyc=StatutKYC.VALIDE)
        self.client.force_authenticate(self.vendeur)

    def creer_boutique(self):
        reponse = self.client.post(
            "/api/vendeurs/ma-boutique/", {"nom": "Boutique Multipart", "logo": image_png()}, format="multipart",
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        return Boutique.objects.get(proprietaire=self.vendeur)

    def creer_produit(self, boutique):
        return Produit.objects.create(boutique=boutique, nom="Pagne", prix_base=Decimal("5000"))

    def test_boutique_creee_avec_logo_reste_ouverte(self):
        boutique = self.creer_boutique()
        self.assertTrue(boutique.est_active)
        self.assertFalse(boutique.livraison_offerte)
        self.assertTrue(boutique.est_publiable)

    def test_boutique_mise_a_jour_complete_ne_la_ferme_pas(self):
        boutique = self.creer_boutique()
        reponse = self.client.put(
            "/api/vendeurs/ma-boutique/", {"nom": boutique.nom, "description": "Nouvelle"}, format="multipart",
        )
        self.assertEqual(reponse.status_code, 200, reponse.data)
        boutique.refresh_from_db()
        self.assertTrue(boutique.est_active)
        self.assertEqual(boutique.description, "Nouvelle")

    def test_booleen_envoye_reste_pris_en_compte(self):
        self.creer_boutique()
        reponse = self.client.patch("/api/vendeurs/ma-boutique/", {"est_active": "false"}, format="multipart")
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertFalse(Boutique.objects.get(proprietaire=self.vendeur).est_active)

    def test_produit_cree_actif(self):
        self.creer_boutique()
        reponse = self.client.post(
            "/api/catalogue/vendeur/produits/", {"nom": "Pagne", "prix_base": "5000"}, format="multipart",
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertTrue(Produit.objects.get(pk=reponse.data["id"]).est_actif)

    def test_variante_creee_active(self):
        produit = self.creer_produit(self.creer_boutique())
        reponse = self.client.post(
            f"/api/catalogue/vendeur/produits/{produit.pk}/variantes/",
            {"nom": "Taille M", "prix": "5000", "quantite_initiale": "3"}, format="multipart",
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertTrue(VarianteProduit.objects.get(pk=reponse.data["id"]).est_active)

    def test_image_produit(self):
        produit = self.creer_produit(self.creer_boutique())
        url = f"/api/catalogue/vendeur/produits/{produit.pk}/images/"
        secondaire = self.client.post(url, {"image": image_png()}, format="multipart")
        principale = self.client.post(url, {"image": image_png(), "est_principale": "true"}, format="multipart")
        self.assertEqual((secondaire.status_code, principale.status_code), (201, 201))
        self.assertFalse(ImageProduit.objects.get(pk=secondaire.data["id"]).est_principale)
        self.assertTrue(ImageProduit.objects.get(pk=principale.data["id"]).est_principale)

    def test_tarif_de_livraison_cree_actif(self):
        self.client.force_authenticate(creer_utilisateur("admin.multipart@anitche.ci", role=Role.ADMIN))
        reponse = self.client.post(
            "/api/livraison/admin/tarifs/",
            {"zone": "hors_abidjan", "commune": "Yamoussoukro", "montant": "3000"}, format="multipart",
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertTrue(TarifLivraison.objects.get(pk=reponse.data["id"]).est_actif)

    def test_message_support_sans_note_interne(self):
        agent = creer_utilisateur("agent.multipart@anitche.ci", role=Role.SUPPORT)
        ticket = SupportTicket.objects.create(created_by=self.vendeur, subject="Colis", description="Où est-il ?")
        self.client.force_authenticate(agent)
        reponse = self.client.post(
            f"/api/support/tickets/{ticket.pk}/messages/", {"content": "Nous vérifions."}, format="multipart",
        )
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertFalse(TicketMessage.objects.get(pk=reponse.data["id"]).is_internal_note)

    def test_aucun_booleen_ecrit_ne_retombe_sur_false_en_multipart(self):
        """Garde-fou sur tous les serializers (KYC, retours, notifications,
        passeports… compris) : absent d'un formulaire, un booléen écrit est
        ignoré, ou prend son défaut déclaré (`restock` des retours : True)."""
        formulaire_vide = QueryDict("")
        for application in APPLICATIONS_METIER:
            module = importlib.import_module(f"apps.{application}.serializers")
            for nom, classe in inspect.getmembers(module, inspect.isclass):
                if not issubclass(classe, serializers.Serializer) or classe.__module__ != module.__name__:
                    continue
                if issubclass(classe, serializers.ModelSerializer) and not hasattr(classe, "Meta"):
                    continue
                for nom_champ, champ in classe().fields.items():
                    if isinstance(champ, serializers.BooleanField) and not champ.read_only:
                        with self.subTest(serializer=nom, champ=nom_champ):
                            self.assertIs(champ.get_value(formulaire_vide), empty)
        from apps.retours.serializers import TraiterDemandeRetourSerializer

        traitement = TraiterDemandeRetourSerializer(data=QueryDict("action=approuver"))
        self.assertTrue(traitement.is_valid(), traitement.errors)
        self.assertIs(traitement.validated_data["restock"], True)
