"""Configuration de production : prod.py refuse de démarrer sans vrai serveur SMTP,
avec un fournisseur de paiement ou des clés de sandbox non autorisés.

prod.py est chargé dans un sous-processus (il ne s'importe pas sans ses
secrets), avec des valeurs factices évidentes. Comme dans le conteneur, où
.dockerignore exclut backend-django/.env, python-decouple n'y lit que
l'environnement : une variable retirée reste absente quel que soit le .env
local du développeur.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.test import SimpleTestCase

MOT_DE_PASSE_SMTP = "mot-de-passe-smtp-factice"
SANS_FICHIER_ENV = "import decouple\ndecouple.config = decouple.Config(decouple.RepositoryEmpty())\n"
IMPORT_PROD = SANS_FICHIER_ENV + "import config.settings.prod\n"
LECTURE_EMAIL = (
    SANS_FICHIER_ENV
    + "import json\n"
    "import config.settings.prod as p\n"
    "print(json.dumps({'timeout': p.EMAIL_TIMEOUT, 'tls': p.EMAIL_USE_TLS, 'ssl': p.EMAIL_USE_SSL,\n"
    "                  'expediteur': p.DEFAULT_FROM_EMAIL, 'serveur': p.SERVER_EMAIL}))\n"
)


class ImportProdMixin:
    def importer_prod(self, script=IMPORT_PROD, **variables):
        """Variables à None : retirées de l'environnement."""
        env = {
            **os.environ,
            # Messages accentués : même encodage des deux côtés, quel que soit l'OS.
            "PYTHONIOENCODING": "utf-8",
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": "x" * 50,
            "FIELD_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
            "ALLOWED_HOSTS": "anitche.com,backend-django",
            "CORS_ALLOWED_ORIGINS": "https://anitche.com",
            "BACKEND_BASE_URL": "https://anitche.com",
            "PAIEMENT_FOURNISSEUR": "cinetpay",
            "CINETPAY_API_KEY": "sk_live_cle",
            "CINETPAY_API_PASSWORD": "mdp",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.exemple.test",
            "EMAIL_PORT": "587",
            "EMAIL_USE_TLS": "True",
            "EMAIL_USE_SSL": "False",
            "EMAIL_TIMEOUT": None,
            # Absentes par défaut, quel que soit l'environnement du développeur.
            "ANITCHE_STAGING_SANDBOX_PAIEMENT": None,
            "GENIUSPAY_API_KEY": None,
            "GENIUSPAY_API_SECRET": None,
            "GENIUSPAY_WEBHOOK_SECRET": None,
            "GENIUSPAY_ENVIRONNEMENT": None,
            "EMAIL_HOST_USER": "identifiant-factice",
            "EMAIL_HOST_PASSWORD": MOT_DE_PASSE_SMTP,
            "DEFAULT_FROM_EMAIL": "ANITCHE <no-reply@anitche.com>",
            **variables,
        }
        env = {cle: valeur for cle, valeur in env.items() if valeur is not None}
        return subprocess.run(
            [sys.executable, "-c", script], cwd=Path(settings.BASE_DIR), env=env, capture_output=True,
            encoding="utf-8",
        )

    def verifier_refus(self, cas, message):
        for libelle, variables in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(**variables)
                self.assertNotEqual(resultat.returncode, 0)
                self.assertIn("ImproperlyConfigured", resultat.stderr)
                self.assertIn(message, resultat.stderr)
                self.assertNotIn(MOT_DE_PASSE_SMTP, resultat.stderr)


class ConfigurationEmailProductionTests(ImportProdMixin, SimpleTestCase):
    def test_configuration_valide(self):
        cas = {
            # Brevo : STARTTLS sur le port 587, délai par défaut.
            "STARTTLS avec nom affiché": ({}, {"timeout": 10, "tls": True, "ssl": False,
                                               "expediteur": "ANITCHE <no-reply@anitche.com>"}),
            "TLS implicite sur 465": ({"EMAIL_PORT": "465", "EMAIL_USE_TLS": "False", "EMAIL_USE_SSL": "True",
                                       "DEFAULT_FROM_EMAIL": "no-reply@anitche.com", "EMAIL_TIMEOUT": "30"},
                                      {"timeout": 30, "tls": False, "ssl": True, "expediteur": "no-reply@anitche.com"}),
            "domaine en majuscules": ({"DEFAULT_FROM_EMAIL": "No-Reply@ANITCHE.COM"},
                                      {"expediteur": "No-Reply@ANITCHE.COM"}),
        }
        for libelle, (variables, attendu) in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(script=LECTURE_EMAIL, **variables)
                self.assertEqual(resultat.returncode, 0, resultat.stderr[-500:])
                lu = json.loads(resultat.stdout.strip().splitlines()[-1])
                self.assertEqual(lu["serveur"], lu["expediteur"])
                self.assertEqual({cle: lu[cle] for cle in attendu}, attendu)

    def test_refus_backend_autre_que_smtp(self):
        self.verifier_refus({
            "console": {"EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"},
            "locmem": {"EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
            "dummy": {"EMAIL_BACKEND": "django.core.mail.backends.dummy.EmailBackend"},
            "filebased": {"EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend"},
            # base.py : console par défaut.
            "absent": {"EMAIL_BACKEND": None},
        }, "EMAIL_BACKEND doit valoir")

    def test_refus_serveur_absent(self):
        self.verifier_refus({
            "vide": {"EMAIL_HOST": ""},
            # Jamais la valeur par défaut de base.py (un serveur tiers).
            "absent": {"EMAIL_HOST": None},
        }, "EMAIL_HOST : à définir")

    def test_refus_identifiant_absent(self):
        self.verifier_refus({"vide": {"EMAIL_HOST_USER": ""}, "espaces": {"EMAIL_HOST_USER": "  "}},
                            "EMAIL_HOST_USER : à définir")

    def test_refus_mot_de_passe_absent(self):
        self.verifier_refus({"vide": {"EMAIL_HOST_PASSWORD": ""}, "absent": {"EMAIL_HOST_PASSWORD": None}},
                            "EMAIL_HOST_PASSWORD : à définir")

    def test_refus_tls_et_ssl_ensemble(self):
        self.verifier_refus({"les deux": {"EMAIL_USE_TLS": "True", "EMAIL_USE_SSL": "True"}},
                            "EMAIL_USE_TLS et EMAIL_USE_SSL sont exclusifs")

    def test_refus_sans_chiffrement(self):
        self.verifier_refus({
            "les deux à False": {"EMAIL_USE_TLS": "False", "EMAIL_USE_SSL": "False"},
            # python-decouple lit une valeur vide comme False.
            "les deux vides": {"EMAIL_USE_TLS": "", "EMAIL_USE_SSL": ""},
        }, "EMAIL_USE_TLS ou EMAIL_USE_SSL doit être activé")

    def test_refus_expediteur_hors_domaine(self):
        self.verifier_refus({
            "autre domaine": {"DEFAULT_FROM_EMAIL": "no-reply@gmail.com"},
            "nom affiché, autre domaine": {"DEFAULT_FROM_EMAIL": "ANITCHE <no-reply@exemple.test>"},
            "suffixe trompeur": {"DEFAULT_FROM_EMAIL": "no-reply@anitche.com.exemple.test"},
            "sous-domaine": {"DEFAULT_FROM_EMAIL": "no-reply@mail.anitche.com"},
            "deux @": {"DEFAULT_FROM_EMAIL": "pirate@exemple.test@anitche.com"},
            "sans adresse": {"DEFAULT_FROM_EMAIL": "ANITCHE"},
            "vide": {"DEFAULT_FROM_EMAIL": ""},
        }, "DEFAULT_FROM_EMAIL doit être une adresse @anitche.com")

    def test_refus_delai_smtp_hors_bornes(self):
        self.verifier_refus({"zéro": {"EMAIL_TIMEOUT": "0"}, "trop long": {"EMAIL_TIMEOUT": "61"}},
                            "EMAIL_TIMEOUT doit être compris entre 1 et 60")


LECTURE_PAIEMENT = (
    SANS_FICHIER_ENV
    + "import json\n"
    "import config.settings.prod as p\n"
    "print(json.dumps({'fournisseur': p.PAIEMENT_FOURNISSEUR, 'environnement': p.GENIUSPAY_ENVIRONNEMENT}))\n"
)
# Préfixes relevés sur le tableau de bord GeniusPay (ceux de « live » restent
# à confirmer) : sk_ pour la clé publique, ss_ pour la clé secrète.
CLE_PUBLIQUE_LIVE = "sk_live_factice"
CLE_SECRETE_LIVE = "ss_live_factice"
CLE_PUBLIQUE_SANDBOX = "sk_sandbox_factice"
CLE_SECRETE_SANDBOX = "ss_sandbox_factice"
SECRET_WEBHOOK_FACTICE = "whsec_factice"
GENIUSPAY_LIVE = {
    "PAIEMENT_FOURNISSEUR": "geniuspay",
    "GENIUSPAY_API_KEY": CLE_PUBLIQUE_LIVE,
    "GENIUSPAY_API_SECRET": CLE_SECRETE_LIVE,
    "GENIUSPAY_WEBHOOK_SECRET": SECRET_WEBHOOK_FACTICE,
    "GENIUSPAY_ENVIRONNEMENT": "live",
}
GENIUSPAY_SANDBOX = {
    **GENIUSPAY_LIVE,
    "GENIUSPAY_API_KEY": CLE_PUBLIQUE_SANDBOX,
    "GENIUSPAY_API_SECRET": CLE_SECRETE_SANDBOX,
    "GENIUSPAY_ENVIRONNEMENT": "sandbox",
    "ANITCHE_STAGING_SANDBOX_PAIEMENT": "true",
}
STAGING = "ANITCHE_STAGING_SANDBOX_PAIEMENT"


class PaiementProductionTests(ImportProdMixin, SimpleTestCase):
    """Liste fermée des fournisseurs, clés GeniusPay et mode staging."""

    def verifier_refus(self, cas, message):
        super().verifier_refus(cas, message)
        for libelle, variables in cas.items():
            with self.subTest(f"{libelle} : aucun secret dans l'erreur"):
                stderr = self.importer_prod(**variables).stderr
                for secret in (SECRET_WEBHOOK_FACTICE, CLE_SECRETE_LIVE, CLE_SECRETE_SANDBOX):
                    self.assertNotIn(secret, stderr)

    def test_configurations_valides(self):
        cas = {
            "cinetpay seul": ({}, {"fournisseur": "cinetpay"}),
            "geniuspay live, variable staging absente": (
                GENIUSPAY_LIVE, {"fournisseur": "geniuspay", "environnement": "live"}),
            "geniuspay live, variable staging à false": (
                {**GENIUSPAY_LIVE, STAGING: "false"}, {"fournisseur": "geniuspay", "environnement": "live"}),
            "geniuspay sandbox en staging": (
                GENIUSPAY_SANDBOX, {"fournisseur": "geniuspay", "environnement": "sandbox"}),
            # La variable ne concerne que geniuspay.
            "cinetpay avec la variable staging à true": ({STAGING: "true"}, {"fournisseur": "cinetpay"}),
            "cinetpay avec la variable staging à false": ({STAGING: "false"}, {"fournisseur": "cinetpay"}),
        }
        for libelle, (variables, attendu) in cas.items():
            with self.subTest(libelle):
                resultat = self.importer_prod(script=LECTURE_PAIEMENT, **variables)
                self.assertEqual(resultat.returncode, 0, resultat.stderr[-500:])
                lu = json.loads(resultat.stdout.strip().splitlines()[-1])
                self.assertEqual({cle: lu[cle] for cle in attendu}, attendu)

    def test_refus_fournisseur_hors_liste(self):
        self.verifier_refus({
            "simulé": {"PAIEMENT_FOURNISSEUR": "simule"},
            "inconnu": {"PAIEMENT_FOURNISSEUR": "stripe"},
            "absent (simulé par défaut)": {"PAIEMENT_FOURNISSEUR": None},
            "casse différente": {**GENIUSPAY_LIVE, "PAIEMENT_FOURNISSEUR": "GeniusPay"},
        }, "est interdit en production")

    def test_refus_cles_geniuspay_absentes(self):
        cas = {}
        for nom in ("GENIUSPAY_API_KEY", "GENIUSPAY_API_SECRET", "GENIUSPAY_WEBHOOK_SECRET"):
            cas[f"{nom} absente"] = {**GENIUSPAY_LIVE, nom: None}
            cas[f"{nom} vide"] = {**GENIUSPAY_LIVE, nom: ""}
            cas[f"{nom} en espaces"] = {**GENIUSPAY_LIVE, nom: "  "}
        self.verifier_refus(cas, "doivent être définis en production")

    def test_refus_environnement_geniuspay_invalide(self):
        self.verifier_refus({
            "absent": {**GENIUSPAY_LIVE, "GENIUSPAY_ENVIRONNEMENT": None},
            "vide": {**GENIUSPAY_LIVE, "GENIUSPAY_ENVIRONNEMENT": ""},
            "inconnu": {**GENIUSPAY_LIVE, "GENIUSPAY_ENVIRONNEMENT": "production"},
            "casse différente": {**GENIUSPAY_LIVE, "GENIUSPAY_ENVIRONNEMENT": "LIVE"},
        }, "GENIUSPAY_ENVIRONNEMENT doit valoir")

    def test_refus_cles_de_sandbox_en_production(self):
        # Environnement live, variable staging absente : toute autre clé est refusée.
        self.verifier_refus({
            "clé publique de sandbox": {**GENIUSPAY_LIVE, "GENIUSPAY_API_KEY": CLE_PUBLIQUE_SANDBOX},
            "clé secrète de sandbox": {**GENIUSPAY_LIVE, "GENIUSPAY_API_SECRET": CLE_SECRETE_SANDBOX},
            "préfixe inconnu (clé publique)": {**GENIUSPAY_LIVE, "GENIUSPAY_API_KEY": "sk_test_factice"},
            "préfixe inconnu (clé secrète)": {**GENIUSPAY_LIVE, "GENIUSPAY_API_SECRET": "factice"},
            "clés inversées": {**GENIUSPAY_LIVE, "GENIUSPAY_API_KEY": CLE_SECRETE_LIVE,
                               "GENIUSPAY_API_SECRET": CLE_PUBLIQUE_LIVE},
        }, "doit commencer par")

    def test_refus_prefixes_de_la_documentation(self):
        # La documentation GeniusPay annonce pk_ (clé publique) et sk_ (clé
        # secrète) ; le tableau de bord délivre sk_ et ss_.
        self.verifier_refus({
            "live, clé publique pk_live_": {**GENIUSPAY_LIVE, "GENIUSPAY_API_KEY": "pk_live_factice"},
            "live, clé secrète sk_live_": {**GENIUSPAY_LIVE, "GENIUSPAY_API_SECRET": "sk_live_autre"},
            "live, paire pk_live_ / sk_live_": {**GENIUSPAY_LIVE, "GENIUSPAY_API_KEY": "pk_live_factice",
                                               "GENIUSPAY_API_SECRET": "sk_live_autre"},
            "sandbox, clé publique pk_sandbox_": {**GENIUSPAY_SANDBOX, "GENIUSPAY_API_KEY": "pk_sandbox_factice"},
            "sandbox, clé secrète sk_sandbox_": {**GENIUSPAY_SANDBOX, "GENIUSPAY_API_SECRET": "sk_sandbox_autre"},
            "sandbox, paire pk_sandbox_ / sk_sandbox_": {
                **GENIUSPAY_SANDBOX, "GENIUSPAY_API_KEY": "pk_sandbox_factice",
                "GENIUSPAY_API_SECRET": "sk_sandbox_autre"},
        }, "doit commencer par")

    def test_refus_environnement_sandbox_sans_variable_staging(self):
        sandbox = {**GENIUSPAY_SANDBOX, STAGING: None}
        self.verifier_refus({
            "variable absente": sandbox,
            "variable à false": {**sandbox, STAGING: "false"},
            "clés live, environnement sandbox": {**GENIUSPAY_LIVE, "GENIUSPAY_ENVIRONNEMENT": "sandbox"},
        }, "réservé au staging")

    def test_refus_variable_staging_avec_environnement_live(self):
        self.verifier_refus({
            "clés et environnement live": {**GENIUSPAY_LIVE, STAGING: "true"},
            "environnement live, clés de sandbox": {**GENIUSPAY_SANDBOX, "GENIUSPAY_ENVIRONNEMENT": "live"},
        }, "incompatible avec l'environnement live")

    def test_refus_variable_staging_avec_cles_live(self):
        # Environnement sandbox mais une clé live : jamais de clé live en staging.
        self.verifier_refus({
            "clé publique live": {**GENIUSPAY_SANDBOX, "GENIUSPAY_API_KEY": CLE_PUBLIQUE_LIVE},
            "clé secrète live": {**GENIUSPAY_SANDBOX, "GENIUSPAY_API_SECRET": CLE_SECRETE_LIVE},
            "deux clés live": {**GENIUSPAY_SANDBOX, "GENIUSPAY_API_KEY": CLE_PUBLIQUE_LIVE,
                               "GENIUSPAY_API_SECRET": CLE_SECRETE_LIVE},
        }, "doit commencer par")

    def test_refus_valeurs_non_exactes_de_la_variable_staging(self):
        valeurs = ("True", "TRUE", "1", "yes", "on", "", " ", "true ", " true", "False", "0", "oui")
        for fournisseur, base in (("geniuspay", GENIUSPAY_SANDBOX), ("cinetpay", {})):
            self.verifier_refus({
                f"{fournisseur} : {valeur!r}": {**base, STAGING: valeur} for valeur in valeurs
            }, f"{STAGING} doit valoir exactement")

    def test_cinetpay_garde_son_refus_des_cles_de_sandbox(self):
        # La variable staging n'ouvre rien pour CinetPay.
        self.verifier_refus({
            "variable absente": {"CINETPAY_API_KEY": "sk_test_cle"},
            "variable à true": {"CINETPAY_API_KEY": "sk_test_cle", STAGING: "true"},
        }, "Clé CinetPay de sandbox")
