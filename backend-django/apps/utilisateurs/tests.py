from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from .models import Utilisateur, CodeOTP


class InscriptionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/inscription/'

    def test_inscription_reussie(self):
        response = self.client.post(self.url, {
            'email': 'nouveau@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'Kouassi',
            'prenom': 'Awa',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Utilisateur.objects.filter(email='nouveau@anitche.ci').exists())

    def test_email_deja_pris(self):
        Utilisateur.objects.create_user(email='existe@anitche.ci', password='xxx', nom='A', prenom='B')
        response = self.client.post(self.url, {
            'email': 'existe@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'C', 'prenom': 'D',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mot_de_passe_faible_refuse(self):
        response = self.client.post(self.url, {
            'email': 'test@anitche.ci',
            'password': '123',
            'nom': 'A', 'prenom': 'B',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProfilTests(TestCase):
    def setUp(self):
        self.utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci', password='MotDePasseSolide123!', nom='A', prenom='B',
        )
        self.client = APIClient()

    def test_profil_sans_authentification(self):
        response = self.client.get('/api/utilisateurs/profil/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_profil_authentifie(self):
        self.client.force_authenticate(user=self.utilisateur)
        response = self.client.get('/api/utilisateurs/profil/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CodeOTPTests(TestCase):
    def test_generation_et_verification(self):
        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci', password='xxx', nom='A', prenom='B',
        )
        otp, code = CodeOTP.generer(utilisateur, 'inscription')
        valide, _ = otp.verifier(code)
        self.assertTrue(valide)

    def test_code_incorrect(self):
        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci', password='xxx', nom='A', prenom='B',
        )
        otp, _ = CodeOTP.generer(utilisateur, 'inscription')
        valide, _ = otp.verifier('000000')
        self.assertFalse(valide)
