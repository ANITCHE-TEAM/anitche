from datetime import timedelta

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import AccessToken

from apps.utilisateurs.models import Utilisateur

from .authentification import JWTAuthentificationOptionnelle
from .reseau import adresse_ip_client


def avec_proxys(nombre):
    return override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, 'NUM_PROXIES': nombre})


class AdresseIPClientTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def requete(self, remote_addr='10.0.0.7', xff=None):
        meta = {'REMOTE_ADDR': remote_addr}
        if xff is not None:
            meta['HTTP_X_FORWARDED_FOR'] = xff
        return self.factory.get('/', **meta)

    def test_sans_proxy_x_forwarded_for_est_ignore(self):
        self.assertEqual(adresse_ip_client(self.requete(xff='6.6.6.6')), '10.0.0.7')

    def test_limite_de_debit_non_contournable_en_changeant_x_forwarded_for(self):
        throttle = AnonRateThrottle()
        identifiants = {
            throttle.get_ident(self.requete(remote_addr='9.9.9.9', xff=f'1.1.1.{i}'))
            for i in range(5)
        }
        self.assertEqual(identifiants, {'9.9.9.9'})

    def test_derriere_un_proxy_seule_la_derniere_entree_compte(self):
        with avec_proxys(1):
            self.assertEqual(adresse_ip_client(self.requete(xff='41.66.1.2')), '41.66.1.2')
            # Entrée forgée par le client devant celle ajoutée par le proxy.
            self.assertEqual(adresse_ip_client(self.requete(xff='6.6.6.6, 41.66.1.2')), '41.66.1.2')

    def test_valeur_invalide_donne_none(self):
        self.assertIsNone(adresse_ip_client(self.requete(remote_addr='pas-une-ip')))
        with avec_proxys(1):
            self.assertIsNone(adresse_ip_client(self.requete(xff='pas-une-ip')))

    def test_ipv6_acceptee(self):
        self.assertEqual(adresse_ip_client(self.requete(remote_addr='2001:db8::1')), '2001:db8::1')


class TypesDeFichiersTests(SimpleTestCase):
    def test_webp_connu_de_mimetypes(self):
        """FileResponse déduit le type de l'extension : .webp doit donner
        image/webp, quel que soit le Python ou le système (apps/core/apps.py)."""
        import mimetypes

        self.assertEqual(mimetypes.guess_type('photo.webp'), ('image/webp', None))


def taux_de_production():
    """Taux de base.py tels qu'en production, lus dans une copie fraîche du
    module : pendant les tests, config.settings.test remplace en place le
    dictionnaire importé de base.py (100000/day partout), donc
    `config.settings.base.REST_FRAMEWORK` n'a plus les vraies valeurs."""
    import importlib.util
    from pathlib import Path

    chemin = Path(__file__).resolve().parents[2] / 'config' / 'settings' / 'base.py'
    spec = importlib.util.spec_from_file_location('reglages_base_production', chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']


class TauxDeLimiteParEnvironnementTests(SimpleTestCase):
    """Les limites relevées pour les Runs Postman n'existent qu'en dev."""

    TAUX_DE_PRODUCTION = {
        'login': '10/hour',
        'otp_envoi': '5/hour',
        'otp_verification': '10/hour',
        'inscription': '10/hour',
    }

    def test_base_garde_les_vraies_valeurs_et_dev_ne_la_modifie_pas(self):
        import importlib

        from config.settings import base

        dev = importlib.import_module('config.settings.dev')
        taux_base = taux_de_production()
        taux_dev = dev.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
        # dev.py construit un nouveau dictionnaire : il ne modifie jamais celui de base.py.
        self.assertIsNot(dev.REST_FRAMEWORK, base.REST_FRAMEWORK)
        self.assertIsNot(taux_dev, base.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'])
        for scope, valeur in self.TAUX_DE_PRODUCTION.items():
            with self.subTest(scope=scope):
                self.assertEqual(taux_base[scope], valeur)
                self.assertEqual(taux_dev[scope], '1000/hour')

    def test_prod_et_ci_ne_redefinissent_aucun_taux(self):
        # prod.py ne s'importe pas sans ses secrets : on vérifie sa source.
        from pathlib import Path

        dossier = Path(__file__).resolve().parents[2] / 'config' / 'settings'
        for nom in ('prod.py', 'ci.py'):
            with self.subTest(fichier=nom):
                self.assertNotIn('THROTTLE_RATES', (dossier / nom).read_text(encoding='utf-8'))


def jeton_expire(utilisateur):
    jeton = AccessToken.for_user(utilisateur)
    jeton.set_exp(lifetime=timedelta(seconds=-1))
    return str(jeton)


class JWTAuthentificationOptionnelleTests(TestCase):
    """Un jeton valide authentifie ; tout jeton refusé laisse la requête
    anonyme, sans exception (donc sans 401)."""

    def setUp(self):
        self.utilisateur = Utilisateur.objects.create_user(
            email='optionnel@anitche.ci', password='MotDePasseSolide123!', nom='O', prenom='P',
        )
        self.factory = RequestFactory()

    def authentifier(self, en_tete=None):
        extra = {'HTTP_AUTHORIZATION': en_tete} if en_tete is not None else {}
        return JWTAuthentificationOptionnelle().authenticate(self.factory.get('/', **extra))

    def test_jeton_valide_authentifie_l_utilisateur(self):
        utilisateur, jeton = self.authentifier(f'Bearer {AccessToken.for_user(self.utilisateur)}')
        self.assertEqual(utilisateur, self.utilisateur)
        self.assertEqual(jeton['user_id'], str(self.utilisateur.pk))

    def test_sans_en_tete_anonyme(self):
        self.assertIsNone(self.authentifier())

    def test_jetons_refuses_anonymes(self):
        entete_et_charge = str(AccessToken.for_user(self.utilisateur)).rsplit('.', 1)[0]
        cas = {
            'expire': f'Bearer {jeton_expire(self.utilisateur)}',
            'malforme': 'Bearer jeton-invalide',
            'signature_forgee': f'Bearer {entete_et_charge}.{"A" * 43}',
            'en_tete_a_trois_parties': 'Bearer a b',
        }
        for nom, en_tete in cas.items():
            with self.subTest(cas=nom):
                self.assertIsNone(self.authentifier(en_tete))

    def test_jeton_revoque_par_changement_de_mot_de_passe_anonyme(self):
        en_tete = f'Bearer {AccessToken.for_user(self.utilisateur)}'
        self.utilisateur.set_password('NouveauMotDePasse456!')
        self.utilisateur.save(update_fields=['password'])
        self.assertIsNone(self.authentifier(en_tete))

    def test_compte_desactive_anonyme(self):
        en_tete = f'Bearer {AccessToken.for_user(self.utilisateur)}'
        self.utilisateur.is_active = False
        self.utilisateur.save(update_fields=['is_active'])
        self.assertIsNone(self.authentifier(en_tete))


class RoutesStrictesJetonExpireTests(TestCase):
    """Le panier (contenu propre au compte) et les routes protégées gardent
    JWTAuthentication : un jeton expiré y donne 401, ce qui déclenche le
    rafraîchissement côté client."""

    def test_panier_et_profil_restent_en_401(self):
        utilisateur = Utilisateur.objects.create_user(
            email='strict@anitche.ci', password='MotDePasseSolide123!', nom='S', prenom='T',
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {jeton_expire(utilisateur)}')
        for url in ('/api/panier/panier/', '/api/utilisateurs/profil/'):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
                self.assertEqual(response.data['errors'].get('code'), ['token_not_valid'])
