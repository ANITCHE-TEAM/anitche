"""Schéma OpenAPI (drf-spectacular, config/schema.py) et documentation
interactive : schéma sans avertissement, fidèle aux conventions de l'API,
identique au fichier versionné, et documentation absente en production.
"""

import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import yaml
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

FICHIER_VERSIONNE = Path(settings.BASE_DIR) / "schema.yaml"

UPLOADS = [
    ("/api/utilisateurs/upload-kyc/", "post"),
    ("/api/catalogue/vendeur/produits/{produit_pk}/images/", "post"),
    ("/api/retours/{id}/photos/", "post"),
    ("/api/support/messages/{message_id}/attachments/", "post"),
]


class SchemaOpenAPITests(SimpleTestCase):
    """Génération (une fois pour la classe) avec --validate --fail-on-warn :
    le moindre avertissement de drf-spectacular fait échouer le test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with tempfile.TemporaryDirectory() as dossier:
            chemin = Path(dossier) / "schema.yaml"
            call_command("spectacular", "--validate", "--fail-on-warn", "--file", str(chemin))
            cls.texte = chemin.read_text(encoding="utf-8")
        cls.schema = yaml.safe_load(cls.texte)

    def operation(self, chemin, methode):
        return self.schema["paths"][chemin][methode]

    def schema_de_reponse(self, chemin, methode, code):
        return self.operation(chemin, methode)["responses"][code]["content"]["application/json"]["schema"]

    def test_telechargements_decrits_avec_leur_vrai_type(self):
        """Un FileResponse n'est jamais décrit en application/json."""
        attendus = {
            ("/api/retours/{id}/photos/{photo_id}/", "get"): ["image/jpeg", "image/png", "image/webp"],
            ("/api/support/attachments/{id}/", "get"): ["image/jpeg", "image/png", "image/webp", "application/pdf"],
            ("/api/utilisateurs/kyc/{utilisateur_id}/{champ}/", "get"): [
                "image/jpeg", "image/png", "image/webp", "application/pdf",
            ],
        }
        for (chemin, methode), types in attendus.items():
            with self.subTest(chemin):
                contenu = self.operation(chemin, methode)["responses"]["200"]["content"]
                self.assertEqual(list(contenu), types)
                for schema in contenu.values():
                    self.assertEqual(schema["schema"], {"type": "string", "format": "binary"})

    def test_version_openapi(self):
        self.assertEqual(self.schema["openapi"], "3.1.0")

    def test_fichier_versionne_a_jour(self):
        """backend-django/schema.yaml doit être régénéré à chaque changement
        de contrat : python manage.py spectacular --file schema.yaml"""
        self.assertTrue(FICHIER_VERSIONNE.exists(), "schema.yaml absent : le générer.")
        versionne = FICHIER_VERSIONNE.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(
            versionne, self.texte.replace("\r\n", "\n"),
            "schema.yaml n'est plus à jour : python manage.py spectacular --file schema.yaml",
        )

    def test_webhooks_et_admin_exclus(self):
        for chemin in self.schema["paths"]:
            self.assertNotIn("webhook", chemin)
            self.assertTrue(chemin.startswith("/api/"), chemin)
        for chemin in ("/api/schema/", "/api/docs/", "/api/redoc/"):
            self.assertNotIn(chemin, self.schema["paths"])

    def test_authentification_jwt(self):
        self.assertEqual(
            self.schema["components"]["securitySchemes"]["jwtAuth"],
            {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        )
        self.assertEqual(self.operation("/api/utilisateurs/profil/", "get")["security"], [{"jwtAuth": []}])
        # Connexion : aucune authentification ; réponse access/refresh.
        connexion = self.operation("/api/utilisateurs/connexion/", "post")
        self.assertNotIn("security", connexion)
        self.assertIn("401", connexion["responses"])

    def test_format_erreur_commun(self):
        erreur = self.schema["components"]["schemas"]["Erreur"]
        self.assertEqual(set(erreur["required"]), {"success", "status_code", "detail", "errors"})
        errors = erreur["properties"]["errors"]
        self.assertEqual(errors["type"], "object")
        self.assertEqual(errors["additionalProperties"]["type"], "array")
        self.assertEqual(errors["additionalProperties"]["items"]["type"], "string")

    def test_erreurs_documentees_sur_chaque_operation(self):
        reference = {"$ref": "#/components/schemas/Erreur"}
        for chemin, methodes in self.schema["paths"].items():
            for methode, operation in methodes.items():
                if methode not in ("get", "post", "put", "patch", "delete"):
                    continue
                with self.subTest(f"{methode.upper()} {chemin}"):
                    codes_erreur = [code for code in operation["responses"] if code.startswith(("4", "5"))]
                    self.assertTrue(codes_erreur)
                    for code in codes_erreur:
                        contenu = operation["responses"][code]["content"]["application/json"]["schema"]
                        self.assertEqual(contenu, reference)
                    if operation.get("security") == [{"jwtAuth": []}]:
                        self.assertIn("401", operation["responses"])
                    if "{" in chemin:
                        self.assertIn("404", operation["responses"])

    def test_conflits_documentes(self):
        for chemin, methode in [
            ("/api/commandes/{id}/annuler/", "post"),
            ("/api/paiements/{id}/annuler/", "post"),
            ("/api/livraison/{id}/statut/", "patch"),
            ("/api/utilisateurs/connexion-google/", "post"),
        ]:
            with self.subTest(chemin):
                self.assertIn("409", self.operation(chemin, methode)["responses"])

    def test_pagination(self):
        reponse = self.schema_de_reponse("/api/catalogue/produits/", "get", "200")
        pagine = self.schema["components"]["schemas"][reponse["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(pagine["required"]), {"count", "results"})
        self.assertEqual(set(pagine["properties"]), {"count", "next", "previous", "results"})
        parametres = {p["name"] for p in self.operation("/api/catalogue/produits/", "get")["parameters"]}
        self.assertTrue({"page", "recherche", "categorie", "tri"} <= parametres)
        # Liste volontairement non paginée : un tableau simple.
        self.assertEqual(self.schema_de_reponse("/api/catalogue/categories/", "get", "200")["type"], "array")

    def test_uploads_multipart(self):
        for chemin, methode in UPLOADS:
            with self.subTest(chemin):
                self.assertIn("multipart/form-data", self.operation(chemin, methode)["requestBody"]["content"])

    def test_champs_en_lecture_seule_absents_des_requetes(self):
        requete = self.schema["components"]["schemas"]["ProduitVendeurRequest"]
        for champ in ("id", "slug", "boutique_nom", "images", "variantes"):
            self.assertNotIn(champ, requete["properties"])

    def test_montants_positifs_avec_exemples_realistes(self):
        """Décimaux (chaînes) : plus d'exemples absurdes (« -04 ») générés par
        Swagger à partir d'un motif autorisant le signe moins. Montants FCFA
        entiers et positifs, sauf les deux montants de reversement qui
        peuvent être négatifs."""
        signes = {"montant_ajustements", "montant_net"}
        hors_fcfa = {"poids_kg", "taux_commission", "valeur"}
        vus = 0
        for composant, schema in self.schema["components"]["schemas"].items():
            for nom, propriete in (schema.get("properties") or {}).items():
                if propriete.get("format") != "decimal":
                    continue
                vus += 1
                with self.subTest(composant=composant, champ=nom):
                    exemple = propriete.get("example")
                    self.assertIsInstance(exemple, str)
                    self.assertRegex(exemple, propriete["pattern"])
                    self.assertTrue(exemple.endswith(".00") or nom in hors_fcfa, exemple)
                    if nom not in signes:
                        self.assertFalse(propriete["pattern"].startswith("^-"), propriete["pattern"])
                        self.assertGreaterEqual(Decimal(exemple), 0)
        self.assertGreater(vus, 50)

    def test_enumerations_nommees(self):
        composants = self.schema["components"]["schemas"]
        for nom in ("StatutCommandeEnum", "StatutLivraisonEnum", "StatutPaiementEnum", "StatutTicketEnum"):
            self.assertIn(nom, composants)
        self.assertEqual(
            composants["StatutCommandeEnum"]["enum"],
            ["creee", "confirmee", "preparation", "expediee", "livree", "annulee"],
        )

    def test_tags_par_module_decrits(self):
        tags = {tag["name"]: tag["description"] for tag in self.schema["tags"]}
        self.assertEqual(len(tags), 12)
        for chemin, methodes in self.schema["paths"].items():
            for methode, operation in methodes.items():
                if methode in ("get", "post", "put", "patch", "delete"):
                    self.assertEqual(len(operation["tags"]), 1)
                    self.assertIn(operation["tags"][0], tags, chemin)


class DocumentationInteractiveTests(TestCase):
    def test_routes_de_documentation_en_developpement(self):
        self.assertTrue(settings.DOCUMENTATION_API_ACTIVE)
        for chemin in ("/api/docs/", "/api/redoc/"):
            with self.subTest(chemin):
                self.assertEqual(self.client.get(chemin).status_code, 200)
        reponse = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(json.loads(reponse.content)["openapi"], "3.1.0")

    def test_aucune_documentation_en_production(self):
        """Settings de production, même avec DOCUMENTATION_API_ACTIVE=True
        dans l'environnement : la valeur est figée dans prod.py."""
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SECRET_KEY": "x" * 50,
            "FIELD_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
            "ALLOWED_HOSTS": "api.anitche.com",
            "CORS_ALLOWED_ORIGINS": "https://anitche.com",
            "BACKEND_BASE_URL": "https://api.anitche.com",
            "PAIEMENT_FOURNISSEUR": "cinetpay",
            "CINETPAY_API_KEY": "sk_live_cle",
            "CINETPAY_API_PASSWORD": "mdp",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.exemple.test",
            "EMAIL_HOST_USER": "identifiant-factice",
            "EMAIL_HOST_PASSWORD": "mot-de-passe-factice",
            "DEFAULT_FROM_EMAIL": "ANITCHE <no-reply@anitche.com>",
            "DOCUMENTATION_API_ACTIVE": "True",
        }
        script = (
            "import django, json\n"
            "django.setup()\n"
            "from django.conf import settings\n"
            "from django.urls import Resolver404, resolve\n"
            "montees = []\n"
            "for chemin in ('/api/schema/', '/api/docs/', '/api/redoc/'):\n"
            "    try:\n"
            "        resolve(chemin)\n"
            "        montees.append(chemin)\n"
            "    except Resolver404:\n"
            "        pass\n"
            "print(json.dumps({'active': settings.DOCUMENTATION_API_ACTIVE, 'montees': montees}))\n"
        )
        resultat = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(settings.BASE_DIR), env=env, capture_output=True, text=True,
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr[-800:])
        self.assertEqual(json.loads(resultat.stdout.strip().splitlines()[-1]), {"active": False, "montees": []})
