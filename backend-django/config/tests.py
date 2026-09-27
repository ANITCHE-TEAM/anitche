"""Format d'erreur commun de l'API (config/exceptions.py), vérifié module
par module sur les erreurs principales : toute réponse d'erreur a la forme
{success: false, status_code, detail: str, errors: {clé: [str, ...]}}.

Seules exceptions documentées : les notifications des fournisseurs de
paiement (apps.paiements.views._VueNotification), qui répondent au format
attendu par le fournisseur.
"""

import uuid
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from apps.commandes.models import Commande, GroupeCommande
from apps.paiements.models import Paiement
from apps.support.models import SupportTicket
from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.vendeurs.models import Boutique

from .exceptions import MESSAGE_ERREUR_INTERNE, custom_exception_handler, normaliser_erreurs


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
