"""Emails en production : prod.py refuse de démarrer sans vrai serveur SMTP.

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


class ConfigurationEmailProductionTests(SimpleTestCase):
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
