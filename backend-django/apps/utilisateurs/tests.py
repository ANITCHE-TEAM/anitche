from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from .models import Utilisateur, CodeOTP


# =====================================================
# TESTS D'INSCRIPTION
# =====================================================

class InscriptionTests(TestCase):
    """
    Vérifie le bon fonctionnement de l'inscription
    des nouveaux utilisateurs.
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/inscription/'

    def test_inscription_reussie(self):
        """
        Un utilisateur valide doit pouvoir créer un compte.
        """

        response = self.client.post(self.url, {
            'email': 'nouveau@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'Kouassi',
            'prenom': 'Awa',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            Utilisateur.objects.filter(
                email='nouveau@anitche.ci'
            ).exists()
        )

    def test_email_deja_pris(self):
        """
        Deux comptes ne peuvent pas partager
        la même adresse email.
        """

        Utilisateur.objects.create_user(
            email='existe@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B'
        )

        response = self.client.post(self.url, {
            'email': 'existe@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'C',
            'prenom': 'D',
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST
        )

    def test_mot_de_passe_faible_refuse(self):
        """
        Les mots de passe ne respectant pas
        la politique de sécurité doivent être refusés.
        """

        response = self.client.post(self.url, {
            'email': 'test@anitche.ci',
            'password': '123',
            'nom': 'A',
            'prenom': 'B',
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST
        )


# =====================================================
# TESTS DU PROFIL
# =====================================================

class ProfilTests(TestCase):
    """
    Vérifie l'accès au profil utilisateur.
    """

    def setUp(self):
        self.utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='MotDePasseSolide123!',
            nom='A',
            prenom='B',
        )

        self.client = APIClient()

    def test_profil_sans_authentification(self):
        """
        Un utilisateur non authentifié
        ne doit pas accéder à son profil.
        """

        response = self.client.get('/api/utilisateurs/profil/')

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )

    def test_profil_authentifie(self):
        """
        Un utilisateur authentifié
        doit pouvoir consulter son profil.
        """

        self.client.force_authenticate(user=self.utilisateur)

        response = self.client.get('/api/utilisateurs/profil/')

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK
        )


# =====================================================
# TESTS DES CODES OTP
# =====================================================

class CodeOTPTests(TestCase):
    """
    Vérifie la génération et la validation
    des codes OTP.
    """

    def test_generation_et_verification(self):
        """
        Un code OTP généré doit être accepté
        lorsqu'il est correctement saisi.
        """

        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )

        otp, code = CodeOTP.generer(
            utilisateur,
            'inscription'
        )

        valide, _ = otp.verifier(code)

        self.assertTrue(valide)

    def test_code_incorrect(self):
        """
        Un code erroné doit être refusé.
        """

        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )

        otp, _ = CodeOTP.generer(
            utilisateur,
            'inscription'
        )

        valide, _ = otp.verifier('000000')

        self.assertFalse(valide)