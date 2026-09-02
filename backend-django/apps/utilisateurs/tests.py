from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
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
# TESTS DE CONNEXION (JWT)
# =====================================================

class ConnexionTests(TestCase):
    """
    Vérifie l'obtention des jetons JWT via /connexion/
    (rest_framework_simplejwt.TokenObtainPairView).
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/connexion/'
        self.refresh_url = '/api/utilisateurs/connexion/rafraichir/'
        self.email = 'test@anitche.ci'
        self.password = 'MotDePasseSolide123!'

        self.utilisateur = Utilisateur.objects.create_user(
            email=self.email,
            password=self.password,
            nom='A',
            prenom='B',
        )

    def test_connexion_reussie(self):
        """
        Des identifiants valides doivent renvoyer
        un couple access/refresh token.
        """

        response = self.client.post(self.url, {
            'email': self.email,
            'password': self.password,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    def test_connexion_mauvais_mot_de_passe(self):
        """
        Un mauvais mot de passe doit être refusé.
        """

        response = self.client.post(self.url, {
            'email': self.email,
            'password': 'MauvaisMotDePasse',
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )

    def test_connexion_email_inexistant(self):
        """
        Un email non enregistré doit être refusé.
        """

        response = self.client.post(self.url, {
            'email': 'inconnu@anitche.ci',
            'password': self.password,
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )

    def test_connexion_compte_inactif_refusee(self):
        """
        Un compte désactivé (is_active=False)
        ne doit pas pouvoir se connecter.
        """

        self.utilisateur.is_active = False
        self.utilisateur.save(update_fields=['is_active'])

        response = self.client.post(self.url, {
            'email': self.email,
            'password': self.password,
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )

    def test_rafraichissement_token(self):
        """
        Un refresh token valide doit permettre
        d'obtenir un nouvel access token.
        """

        connexion = self.client.post(self.url, {
            'email': self.email,
            'password': self.password,
        })
        refresh = connexion.data['refresh']

        response = self.client.post(self.refresh_url, {
            'refresh': refresh,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)


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

    def test_champs_role_et_kyc_en_lecture_seule(self):
        """
        Une tentative de modification de role/statut_kyc
        via le profil ne doit avoir aucun effet
        (champs read_only dans ProfilSerializer).
        """

        self.client.force_authenticate(user=self.utilisateur)

        response = self.client.patch('/api/utilisateurs/profil/', {
            'role': 'admin',
            'statut_kyc': 'valide',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.role, 'client')
        self.assertEqual(self.utilisateur.statut_kyc, 'non_soumis')


# =====================================================
# TESTS DES CODES OTP
# =====================================================

class CodeOTPTests(TestCase):
    """
    Vérifie la génération et la validation
    des codes OTP.
    """

    def setUp(self):
        self.utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )

    def test_generation_et_verification(self):
        """
        Un code OTP généré doit être accepté
        lorsqu'il est correctement saisi.
        """

        otp, code = CodeOTP.generer(
            self.utilisateur,
            'inscription'
        )

        valide, _ = otp.verifier(code)

        self.assertTrue(valide)

    def test_code_incorrect(self):
        """
        Un code erroné doit être refusé.
        """

        otp, _ = CodeOTP.generer(
            self.utilisateur,
            'inscription'
        )

        valide, _ = otp.verifier('000000')

        self.assertFalse(valide)

    def test_code_expire_refuse(self):
        """
        Un code dont la date d'expiration
        est dépassée doit être refusé.
        """

        otp, code = CodeOTP.generer(
            self.utilisateur,
            'inscription'
        )

        otp.date_expiration = timezone.now() - timedelta(minutes=1)
        otp.save(update_fields=['date_expiration'])

        valide, message = otp.verifier(code)

        self.assertFalse(valide)
        self.assertEqual(message, "Ce code a expiré.")

    def test_code_deja_utilise_refuse(self):
        """
        Un code déjà validé une première fois
        ne doit pas pouvoir être réutilisé.
        """

        otp, code = CodeOTP.generer(
            self.utilisateur,
            'inscription'
        )

        premiere_verification, _ = otp.verifier(code)
        deuxieme_verification, message = otp.verifier(code)

        self.assertTrue(premiere_verification)
        self.assertFalse(deuxieme_verification)
        self.assertEqual(message, "Ce code a déjà été utilisé.")

    def test_nombre_tentatives_max_atteint(self):
        """
        Au-delà du nombre maximal de tentatives,
        même le bon code doit être refusé.
        """

        otp, code = CodeOTP.generer(
            self.utilisateur,
            'inscription'
        )

        for _ in range(CodeOTP.NOMBRE_TENTATIVES_MAX):
            otp.verifier('000000')

        valide, message = otp.verifier(code)

        self.assertFalse(valide)
        self.assertEqual(
            message,
            "Nombre maximal de tentatives atteint."
        )


class DemandeChangementContactTests(TestCase):
    """
    Vérifie la sécurité de la demande de changement
    d'email / téléphone : unicité et bonne destination du code OTP.
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/changement-contact/'

        self.utilisateur = Utilisateur.objects.create_user(
            email='moi@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )
        self.autre_utilisateur = Utilisateur.objects.create_user(
            email='dejapris@anitche.ci',
            password='xxx',
            nom='C',
            prenom='D',
        )
        self.client.force_authenticate(user=self.utilisateur)

    def test_email_deja_utilise_refuse(self):
        """
        Impossible de demander un email déjà utilisé
        par un autre compte.
        """

        response = self.client.post(self.url, {
            'nouvel_email': 'dejapris@anitche.ci',
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            CodeOTP.objects.filter(utilisateur=self.utilisateur).exists()
        )

    @patch('apps.utilisateurs.views.envoyer_code_otp_email.delay')
    def test_otp_changement_email_envoye_a_la_nouvelle_adresse(self, mock_envoi):
        """
        Le code OTP doit partir sur la NOUVELLE adresse email,
        jamais sur l'ancienne : c'est elle qu'on cherche à prouver.
        """

        response = self.client.post(self.url, {
            'nouvel_email': 'nouvelle@anitche.ci',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_envoi.assert_called_once()
        destinataire = mock_envoi.call_args[0][0]
        self.assertEqual(destinataire, 'nouvelle@anitche.ci')


class VerificationOTPViewTests(TestCase):
    """
    Vérifie le endpoint /verification-otp/ pour
    le changement d'email et de téléphone.
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/verification-otp/'

        self.utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )
        self.client.force_authenticate(user=self.utilisateur)

    def test_changement_email_applique_apres_otp_valide(self):
        """
        Un code OTP valide doit déclencher
        la mise à jour de l'email et son marquage vérifié.
        """

        _, code = CodeOTP.generer(
            self.utilisateur,
            'changement_email',
            'nouvel-email@anitche.ci'
        )

        response = self.client.post(self.url, {
            'code': code,
            'type_usage': 'changement_email',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.email, 'nouvel-email@anitche.ci')
        self.assertTrue(self.utilisateur.email_verifie)

    def test_aucun_otp_en_attente(self):
        """
        Sans OTP généré au préalable,
        la vérification doit échouer proprement.
        """

        response = self.client.post(self.url, {
            'code': '123456',
            'type_usage': 'changement_email',
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST
        )


# =====================================================
# TESTS MOT DE PASSE OUBLIÉ
# =====================================================

class MotDePasseOublieTests(TestCase):
    """
    Vérifie le flux complet de réinitialisation
    du mot de passe (demande + confirmation).
    """

    def setUp(self):
        self.client = APIClient()
        self.demande_url = '/api/utilisateurs/mot-de-passe-oublie/'
        self.confirmation_url = (
            '/api/utilisateurs/mot-de-passe-oublie/confirmer/'
        )

        self.email = 'test@anitche.ci'
        self.utilisateur = Utilisateur.objects.create_user(
            email=self.email,
            password='AncienMotDePasse123!',
            nom='A',
            prenom='B',
        )

    def test_demande_email_existant(self):
        """
        Une demande pour un email existant
        doit répondre 200 et créer un OTP.
        """

        response = self.client.post(self.demande_url, {
            'email': self.email,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            CodeOTP.objects.filter(
                utilisateur=self.utilisateur,
                type_usage='mdp_oublie',
            ).exists()
        )

    def test_demande_email_inexistant_meme_reponse(self):
        """
        Une demande pour un email inconnu doit renvoyer
        la même réponse 200, sans révéler l'absence du compte.
        """

        response = self.client.post(self.demande_url, {
            'email': 'inconnu@anitche.ci',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_confirmation_reussie_change_le_mot_de_passe(self):
        """
        Un code OTP valide doit permettre
        de définir un nouveau mot de passe utilisable.
        """

        _, code = CodeOTP.generer(self.utilisateur, 'mdp_oublie')

        response = self.client.post(self.confirmation_url, {
            'email': self.email,
            'code': code,
            'nouveau_password': 'NouveauMotDePasse456!',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.utilisateur.refresh_from_db()
        self.assertTrue(
            self.utilisateur.check_password('NouveauMotDePasse456!')
        )

    def test_confirmation_code_incorrect_refusee(self):
        """
        Un code incorrect doit empêcher
        le changement de mot de passe.
        """

        CodeOTP.generer(self.utilisateur, 'mdp_oublie')

        response = self.client.post(self.confirmation_url, {
            'email': self.email,
            'code': '000000',
            'nouveau_password': 'NouveauMotDePasse456!',
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST
        )
        self.utilisateur.refresh_from_db()
        self.assertTrue(
            self.utilisateur.check_password('AncienMotDePasse123!')
        )

    def test_confirmation_nouveau_mot_de_passe_faible_refuse(self):
        """
        Le nouveau mot de passe doit lui aussi respecter
        la politique de sécurité définie par Django.
        """

        _, code = CodeOTP.generer(self.utilisateur, 'mdp_oublie')

        response = self.client.post(self.confirmation_url, {
            'email': self.email,
            'code': code,
            'nouveau_password': '123',
        })

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST
        )


# =====================================================
# TESTS DEMANDE VENDEUR / KYC
# =====================================================

class DemandeVendeurTests(TestCase):
    """
    Vérifie les règles d'accès au statut vendeur.
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/demande-vendeur/'

        self.utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )

    def test_sans_authentification_refuse(self):
        """
        Un utilisateur non authentifié ne peut pas
        demander le statut vendeur.
        """

        response = self.client.post(self.url)

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED
        )

    def test_sans_dossier_kyc_refuse(self):
        """
        La demande doit être refusée si aucun
        dossier KYC n'a été soumis au préalable.
        """

        self.client.force_authenticate(user=self.utilisateur)

        response = self.client.post(self.url)

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST
        )

# =====================================================
# TESTS DE CONNEXION GOOGLE
# =====================================================

class ConnexionGoogleTests(TestCase):
    """
    Vérifie que la connexion via Google respecte
    les mêmes règles de contrôle d'accès que la
    connexion classique (notamment is_active).
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/connexion-google/'

        self.infos_google = {
            'sub': 'google-id-123',
            'email': 'test@anitche.ci',
            'email_verified': True,
            'family_name': 'A',
            'given_name': 'B',
        }

    @patch('apps.utilisateurs.views.google_id_token.verify_oauth2_token')
    def test_compte_desactive_refuse_meme_avec_token_google_valide(self, mock_verify):
        """
        Un compte désactivé (banni) ne doit recevoir aucun
        token, même si le token Google fourni est valide.
        """

        mock_verify.return_value = self.infos_google

        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )
        utilisateur.is_active = False
        utilisateur.save(update_fields=['is_active'])

        response = self.client.post(self.url, {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)

    @patch('apps.utilisateurs.views.google_id_token.verify_oauth2_token')
    def test_compte_actif_recoit_des_tokens(self, mock_verify):
        """
        Un compte actif doit recevoir un couple de tokens
        JWT valides après vérification Google.
        """

        mock_verify.return_value = self.infos_google

        Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )

        response = self.client.post(self.url, {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)