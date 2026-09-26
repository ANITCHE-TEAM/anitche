from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from .models import Utilisateur, CodeOTP, Role


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

    def test_nouveau_compte_est_non_verifie_par_defaut(self):
        """
        F-04 : la simple création du compte ne doit jamais suffire à le
        marquer vérifié — sans quoi n'importe qui pourrait s'approprier
        l'email d'un tiers en s'inscrivant simplement avec.
        """

        self.client.post(self.url, {
            'email': 'nonverifie@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'Kouassi',
            'prenom': 'Awa',
        })

        utilisateur = Utilisateur.objects.get(email='nonverifie@anitche.ci')
        self.assertFalse(utilisateur.email_verifie)

    def test_inscription_genere_un_otp_de_type_inscription(self):
        """
        F-04 : une inscription doit toujours déclencher la génération
        d'un CodeOTP de type 'inscription', pour que l'email fourni
        puisse être prouvé ensuite via /verification-otp/.
        """

        self.client.post(self.url, {
            'email': 'averifier@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'Kouassi',
            'prenom': 'Awa',
        })

        utilisateur = Utilisateur.objects.get(email='averifier@anitche.ci')
        self.assertTrue(
            CodeOTP.objects.filter(
                utilisateur=utilisateur,
                type_usage='inscription',
                utilise=False,
            ).exists()
        )

    @patch('apps.utilisateurs.views.envoyer_code_otp_email.delay')
    def test_inscription_envoie_le_code_a_ladresse_fournie(self, mock_envoi):
        """
        F-04 : le code doit partir sur l'adresse indiquée à l'inscription
        — c'est justement cette adresse que l'on cherche à prouver.
        """

        response = self.client.post(self.url, {
            'email': 'averifier@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'Kouassi',
            'prenom': 'Awa',
        })

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_envoi.assert_called_once()
        destinataire, _code, type_usage = mock_envoi.call_args[0]
        self.assertEqual(destinataire, 'averifier@anitche.ci')
        self.assertEqual(type_usage, 'inscription')

    def test_inscription_echouee_ne_genere_aucun_otp(self):
        """
        Non-régression : un échec de validation (email déjà pris, mot de
        passe faible...) ne doit créer ni utilisateur ni OTP orphelin.
        """

        Utilisateur.objects.create_user(
            email='existe@anitche.ci', password='xxx', nom='A', prenom='B',
        )

        self.client.post(self.url, {
            'email': 'existe@anitche.ci',
            'password': 'MotDePasseSolide123!',
            'nom': 'C',
            'prenom': 'D',
        })

        self.assertEqual(CodeOTP.objects.count(), 0)


class VerificationOTPInscriptionTests(TestCase):
    """
    F-04 : vérifie que la confirmation d'un OTP d'inscription via
    /verification-otp/ marque bien le compte comme vérifié, et que
    rien d'autre ne peut le faire à sa place.
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/verification-otp/'

        self.utilisateur = Utilisateur.objects.create_user(
            email='nouveau@anitche.ci',
            password='MotDePasseSolide123!',
            nom='A',
            prenom='B',
        )
        self.client.force_authenticate(user=self.utilisateur)

    def test_otp_inscription_valide_marque_email_verifie(self):
        _, code = CodeOTP.generer(self.utilisateur, 'inscription')

        response = self.client.post(self.url, {
            'code': code,
            'type_usage': 'inscription',
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.utilisateur.refresh_from_db()
        self.assertTrue(self.utilisateur.email_verifie)

    def test_otp_inscription_incorrect_ne_verifie_pas(self):
        CodeOTP.generer(self.utilisateur, 'inscription')

        response = self.client.post(self.url, {
            'code': '000000',
            'type_usage': 'inscription',
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.utilisateur.refresh_from_db()
        self.assertFalse(self.utilisateur.email_verifie)

    def test_otp_inscription_dun_autre_compte_est_ignore(self):
        """
        Le filtre `utilisateur=request.user` doit empêcher qu'un compte
        se vérifie avec le code OTP généré pour un AUTRE compte.
        """

        autre = Utilisateur.objects.create_user(
            email='autre@anitche.ci', password='xxx', nom='C', prenom='D',
        )
        _, code = CodeOTP.generer(autre, 'inscription')

        response = self.client.post(self.url, {
            'code': code,
            'type_usage': 'inscription',
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.utilisateur.refresh_from_db()
        autre.refresh_from_db()
        self.assertFalse(self.utilisateur.email_verifie)
        self.assertFalse(autre.email_verifie)  # jamais consommé par un tiers


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

    @patch('apps.utilisateurs.views.envoyer_notification_connexion.delay')
    def test_connexion_reussie_envoie_une_notification(self, mock_envoi):
        """F-04 : une connexion réussie doit notifier le titulaire du
        compte (IP + user-agent), pour qu'il repère une connexion qu'il
        n'a pas initiée lui-même."""

        response = self.client.post(self.url, {
            'email': self.email,
            'password': self.password,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_envoi.assert_called_once()
        args, _ = mock_envoi.call_args
        self.assertEqual(args[0], self.email)

    @patch('apps.utilisateurs.views.envoyer_notification_connexion.delay')
    def test_connexion_echouee_nenvoie_aucune_notification(self, mock_envoi):
        """F-04 : un échec d'authentification ne doit jamais déclencher
        cette notification (ni alerter à tort sur une faute de frappe, ni
        servir à énumérer les emails existants)."""

        response = self.client.post(self.url, {
            'email': self.email,
            'password': 'MauvaisMotDePasse',
        })

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        mock_envoi.assert_not_called()

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

    def test_profil_expose_id(self):
        """
        F-11 : le microservice FastAPI (app/core/securite.py) a besoin de
        l'id utilisateur pour vérifier qu'un livreur publie bien sa propre
        position GPS. Ce champ doit rester présent et correct.
        """

        self.client.force_authenticate(user=self.utilisateur)

        response = self.client.get('/api/utilisateurs/profil/')

        self.assertEqual(response.data['id'], self.utilisateur.id)

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
            email_verifie=True,
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

    def test_confirmation_email_inconnu_meme_message_que_sans_otp_en_attente(self):
        """Sécurité (énumération de comptes) : un email inconnu et un email
        connu sans OTP en attente doivent renvoyer EXACTEMENT le même
        message — sinon on peut deviner quels emails sont inscrits en
        soumettant directement l'étape de confirmation."""

        reponse_email_inconnu = self.client.post(self.confirmation_url, {
            'email': 'jamais-inscrit@anitche.ci',
            'code': '000000',
            'nouveau_password': 'NouveauMotDePasse456!',
        })

        reponse_email_connu_sans_otp = self.client.post(self.confirmation_url, {
            'email': self.email,
            'code': '000000',
            'nouveau_password': 'NouveauMotDePasse456!',
        })

        self.assertEqual(reponse_email_inconnu.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reponse_email_connu_sans_otp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reponse_email_inconnu.data, reponse_email_connu_sans_otp.data)


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
        Un compte actif ET déjà vérifié doit recevoir un couple de
        tokens JWT valides après vérification Google.

        email_verifie=True est nécessaire ici : un compte avec un
        mot de passe réel mais non vérifié est désormais refusé par
        le garde-fou anti pré-hijacking (voir F-01 / test dédié
        ci-dessous), ce n'est pas le cas que ce test veut couvrir.
        """

        mock_verify.return_value = self.infos_google

        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )
        utilisateur.email_verifie = True
        utilisateur.save(update_fields=['email_verifie'])

        response = self.client.post(self.url, {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    @patch('apps.utilisateurs.views.envoyer_notification_connexion.delay')
    @patch('apps.utilisateurs.views.google_id_token.verify_oauth2_token')
    def test_connexion_google_reussie_envoie_une_notification(self, mock_verify, mock_envoi):
        """F-04 : la connexion Google réussie doit envoyer la même
        notification que la connexion classique."""

        mock_verify.return_value = self.infos_google

        utilisateur = Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='xxx',
            nom='A',
            prenom='B',
        )
        utilisateur.email_verifie = True
        utilisateur.save(update_fields=['email_verifie'])

        response = self.client.post(self.url, {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_envoi.assert_called_once()
        args, _ = mock_envoi.call_args
        self.assertEqual(args[0], 'test@anitche.ci')

    @patch('apps.utilisateurs.views.google_id_token.verify_oauth2_token')
    def test_compte_non_verifie_avec_mot_de_passe_refuse_liaison(self, mock_verify):
        """
        F-01 (pré-hijacking) : un compte pré-créé par un tiers avec
        un mot de passe réel, jamais vérifié, ne doit jamais être
        auto-lié à une connexion Google portant le même email.
        """

        mock_verify.return_value = self.infos_google

        Utilisateur.objects.create_user(
            email='test@anitche.ci',
            password='mot-de-passe-attaquant',
            nom='A',
            prenom='B',
        )  # email_verifie=False par défaut : non touché ici

        response = self.client.post(self.url, {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)

    @patch('apps.utilisateurs.views.google_id_token.verify_oauth2_token')
    def test_compte_inexistant_est_cree_normalement(self, mock_verify):
        """
        Aucun compte préexistant : la création via Google doit
        continuer de fonctionner sans blocage (pas de régression).
        """

        mock_verify.return_value = self.infos_google

        response = self.client.post(self.url, {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

        utilisateur = Utilisateur.objects.get(email='test@anitche.ci')
        self.assertTrue(utilisateur.email_verifie)
        self.assertFalse(utilisateur.has_usable_password())


# =====================================================
# TESTS DE RÉVOCATION DE TOKENS (RESET PASSWORD + LOGOUT)
# =====================================================

class RevocationTokensTests(TestCase):
    """
    Vérifie que le token_blacklist installé est réellement exploité :
    - un reset de mot de passe révoque les refresh tokens déjà émis ;
    - l'endpoint de déconnexion révoque le refresh token fourni.
    """

    def setUp(self):
        self.client = APIClient()
        self.confirmation_url = (
            '/api/utilisateurs/mot-de-passe-oublie/confirmer/'
        )
        self.deconnexion_url = '/api/utilisateurs/deconnexion/'

        self.email = 'test@anitche.ci'
        self.utilisateur = Utilisateur.objects.create_user(
            email=self.email,
            password='AncienMotDePasse123!',
            nom='A',
            prenom='B',
        )

    def _obtenir_tokens(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(self.utilisateur)
        return str(refresh), str(refresh.access_token)

    def test_reset_mot_de_passe_invalide_lancien_access_token(self):
        """
        CHECK_REVOKE_TOKEN : un access token émis avant le reset ne doit
        plus être accepté après, même s'il n'est pas encore expiré —
        son claim hash_password ne correspond plus au mot de passe
        courant.
        """

        _, access_token = self._obtenir_tokens()

        _, code = CodeOTP.generer(self.utilisateur, 'mdp_oublie')
        self.client.post(self.confirmation_url, {
            'email': self.email,
            'code': code,
            'nouveau_password': 'NouveauMotDePasse456!',
        })

        client_avec_ancien_token = APIClient()
        client_avec_ancien_token.credentials(
            HTTP_AUTHORIZATION=f'Bearer {access_token}'
        )
        response = client_avec_ancien_token.get('/api/utilisateurs/profil/')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_reset_mot_de_passe_revoque_les_refresh_tokens_existants(self):
        """
        Un refresh token émis avant le reset ne doit plus être
        utilisable pour rafraîchir un access token après le reset.
        """

        refresh_token, _ = self._obtenir_tokens()

        _, code = CodeOTP.generer(self.utilisateur, 'mdp_oublie')

        response = self.client.post(self.confirmation_url, {
            'email': self.email,
            'code': code,
            'nouveau_password': 'NouveauMotDePasse456!',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        rafraichissement = self.client.post(
            '/api/utilisateurs/connexion/rafraichir/',
            {'refresh': refresh_token},
        )
        self.assertEqual(
            rafraichissement.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_logout_revoque_le_refresh_token_fourni(self):
        """
        Après déconnexion, le refresh token fourni ne doit plus
        permettre de rafraîchir un access token. Aucune authentification
        n'est nécessaire : posséder le refresh token est la seule
        preuve exigée (même modèle que TokenBlacklistView).
        """

        refresh_token, _ = self._obtenir_tokens()

        response = self.client.post(self.deconnexion_url, {
            'refresh': refresh_token,
        })
        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)

        rafraichissement = self.client.post(
            '/api/utilisateurs/connexion/rafraichir/',
            {'refresh': refresh_token},
        )
        self.assertEqual(
            rafraichissement.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_logout_fonctionne_meme_avec_un_access_token_expire_fourni(self):
        """
        Cas réel visé : le frontend envoie systématiquement le header
        Authorization, y compris sur l'appel de déconnexion. Un access
        token expiré ou invalide dans ce header ne doit jamais empêcher
        la révocation du refresh token — authentication_classes = []
        garantit que ce header n'est même pas examiné par cette vue.
        """

        refresh_token, _ = self._obtenir_tokens()

        self.client.credentials(HTTP_AUTHORIZATION='Bearer token-invalide-ou-expire')
        response = self.client.post(self.deconnexion_url, {
            'refresh': refresh_token,
        })

        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)

    def test_logout_sans_refresh_token_refuse(self):
        """Le corps de la requête doit fournir un refresh token."""

        response = self.client.post(self.deconnexion_url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_logout_est_bien_throttle(self):
        """
        LogoutView est en AllowAny : sans limite dédiée, ce point
        d'entrée public serait un vecteur de spam à coût nul. Même
        technique que apps.fidelite.tests.FideliteThrottleTestCase :
        ScopedRateThrottle.THROTTLE_RATES est un attribut de classe figé
        à l'import, donc patché directement plutôt que via
        override_settings (qui n'aurait aucun effet ici).
        """
        from unittest.mock import patch
        from django.core.cache import cache
        from rest_framework.throttling import ScopedRateThrottle

        cache.clear()
        refresh_token, _ = self._obtenir_tokens()

        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'logout': '1/min'}):
            premiere = self.client.post(self.deconnexion_url, {
                'refresh': refresh_token,
            })
            self.assertEqual(premiere.status_code, status.HTTP_205_RESET_CONTENT)

            deuxieme = self.client.post(self.deconnexion_url, {
                'refresh': refresh_token,
            })
            self.assertEqual(
                deuxieme.status_code,
                status.HTTP_429_TOO_MANY_REQUESTS,
            )


class UtilisateurAdminTestCase(TestCase):
    """F-16 : la validation KYC/vendeur doit passer par
    apps.vendeurs.admin.DemandeVendeurAdmin, pas par une édition libre
    de statut_kyc depuis l'admin général des utilisateurs."""

    def test_statut_kyc_readonly_dans_admin(self):
        from apps.utilisateurs.admin import UtilisateurAdmin
        from django.contrib.admin.sites import AdminSite

        admin_instance = UtilisateurAdmin(Utilisateur, AdminSite())
        self.assertIn('statut_kyc', admin_instance.readonly_fields)


class DocumentKYCAdminTestCase(TestCase):
    """Broken Access Control : piece_identite_recto/verso et selfie ne
    doivent jamais apparaître comme des champs de formulaire admin
    classiques — un FileField, même en readonly_fields, reste un lien
    direct vers sa MEDIA_URL brute dans le rendu Django admin, ce qui
    recrée la fuite que TelechargerDocumentKYCView existe pour fermer
    côté API."""

    def test_piece_identite_et_selfie_exclus_du_formulaire_admin(self):
        from apps.utilisateurs.admin import DocumentKYCAdmin
        from apps.utilisateurs.models import DocumentKYC
        from django.contrib.admin.sites import AdminSite

        admin_instance = DocumentKYCAdmin(DocumentKYC, AdminSite())
        self.assertIn('piece_identite_recto', admin_instance.exclude)
        self.assertIn('piece_identite_verso', admin_instance.exclude)
        self.assertIn('selfie', admin_instance.exclude)

    def test_liens_documents_pointent_vers_la_vue_controlee(self):
        from apps.utilisateurs.admin import DocumentKYCAdmin
        from apps.utilisateurs.models import DocumentKYC, TypePieceIdentite
        from django.contrib.admin.sites import AdminSite
        from django.core.files.uploadedfile import SimpleUploadedFile

        utilisateur = Utilisateur.objects.create_user(
            email="kyc-admin-test@anitche.ci", password="TestPassword123!",
            nom="Test", prenom="KYC",
        )
        dossier = DocumentKYC.objects.create(
            utilisateur=utilisateur,
            type_piece=TypePieceIdentite.CNI,
            piece_identite_recto=SimpleUploadedFile("recto.jpg", b"contenu-factice"),
            piece_identite_verso=SimpleUploadedFile("verso.jpg", b"contenu-factice"),
            selfie=SimpleUploadedFile("selfie.jpg", b"contenu-factice"),
            numero_mobile_money="0700000000",
            adresse="Abidjan",
        )

        admin_instance = DocumentKYCAdmin(DocumentKYC, AdminSite())
        lien_recto = admin_instance.lien_piece_identite_recto(dossier)
        lien_verso = admin_instance.lien_piece_identite_verso(dossier)

        self.assertIn(f"/api/utilisateurs/kyc/{utilisateur.id}/piece_identite_recto/", lien_recto)
        self.assertIn(f"/api/utilisateurs/kyc/{utilisateur.id}/piece_identite_verso/", lien_verso)
        # Ne doit jamais exposer le chemin MEDIA_URL brut du fichier.
        self.assertNotIn(dossier.piece_identite_recto.url, lien_recto)
        self.assertNotIn(dossier.piece_identite_verso.url, lien_verso)

    def test_lien_verso_absent_affiche_un_tiret(self):
        """Le verso étant optionnel au niveau modèle, l'admin doit
        afficher un tiret plutôt qu'un lien cassé quand il est absent."""
        from apps.utilisateurs.admin import DocumentKYCAdmin
        from apps.utilisateurs.models import DocumentKYC, TypePieceIdentite
        from django.contrib.admin.sites import AdminSite
        from django.core.files.uploadedfile import SimpleUploadedFile

        utilisateur = Utilisateur.objects.create_user(
            email="kyc-admin-sans-verso@anitche.ci", password="TestPassword123!",
            nom="Test", prenom="KYC",
        )
        dossier = DocumentKYC.objects.create(
            utilisateur=utilisateur,
            type_piece=TypePieceIdentite.PASSEPORT,
            piece_identite_recto=SimpleUploadedFile("recto.jpg", b"contenu-factice"),
            selfie=SimpleUploadedFile("selfie.jpg", b"contenu-factice"),
            numero_mobile_money="0700000000",
            adresse="Abidjan",
        )

        admin_instance = DocumentKYCAdmin(DocumentKYC, AdminSite())
        self.assertEqual(admin_instance.lien_piece_identite_verso(dossier), "—")


# =====================================================
# TESTS UPLOAD KYC — RÈGLES RECTO/VERSO SELON LE TYPE DE PIÈCE
# =====================================================

def _fichier_pdf_valide(nom):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(nom, b"%PDF-1.4\n%fake-content", content_type="application/pdf")


def _fichier_selfie_valide():
    # ImageField (Django) décode réellement l'image via Pillow, pas
    # seulement sa signature binaire : un PNG minimal mais valide est
    # nécessaire, sans quoi toute requête échoue en 400 sur le champ
    # selfie avant même d'atteindre la règle métier testée.
    import io
    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (1, 1)).save(buffer, format='PNG')
    return SimpleUploadedFile(
        "selfie.png", buffer.getvalue(), content_type="image/png"
    )


class UploadKYCTests(TestCase):
    """
    Vérifie que DocumentKYCSerializer applique correctement la règle
    métier : verso obligatoire pour les pièces recto/verso (CNI, permis,
    carte consulaire, carte résident), refusé pour les pièces à page
    unique (passeport, attestation d'identité).
    """

    def setUp(self):
        self.client = APIClient()
        self.url = '/api/utilisateurs/upload-kyc/'
        self.utilisateur = Utilisateur.objects.create_user(
            email='kyc-upload@anitche.ci', password='TestPassword123!',
            nom='Test', prenom='KYC', email_verifie=True,
        )
        self.client.force_authenticate(user=self.utilisateur)

    def _payload(self, type_piece, avec_verso):
        data = {
            'type_piece': type_piece,
            'piece_identite_recto': _fichier_pdf_valide('recto.pdf'),
            'selfie': _fichier_selfie_valide(),
            'numero_mobile_money': '0700000000',
            'adresse': 'Cocody, Abidjan',
        }
        if avec_verso:
            data['piece_identite_verso'] = _fichier_pdf_valide('verso.pdf')
        return data

    def test_cni_sans_verso_refuse(self):
        response = self.client.post(
            self.url, self._payload('cni', avec_verso=False), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cni_avec_verso_accepte(self):
        response = self.client.post(
            self.url, self._payload('cni', avec_verso=True), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_permis_sans_verso_refuse(self):
        """La règle doit s'appliquer à toutes les pièces recto/verso, pas
        uniquement la CNI."""
        response = self.client.post(
            self.url, self._payload('permis', avec_verso=False), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_passeport_avec_verso_refuse(self):
        response = self.client.post(
            self.url, self._payload('passeport', avec_verso=True), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_passeport_sans_verso_accepte(self):
        response = self.client.post(
            self.url, self._payload('passeport', avec_verso=False), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_attestation_identite_sans_verso_accepte(self):
        response = self.client.post(
            self.url, self._payload('attestation_identite', avec_verso=False), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_premiere_soumission_passe_directement_en_attente(self):
        """
        Harmonisation : la toute première soumission KYC suffit
        désormais à elle seule à passer statut_kyc à en_attente, comme
        la resoumission après refus — la vue /demande-vendeur/ séparée
        qui aurait autrement été nécessaire a été supprimée.
        """
        from apps.utilisateurs.models import StatutKYC

        response = self.client.post(
            self.url, self._payload('passeport', avec_verso=False), format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.statut_kyc, StatutKYC.EN_ATTENTE)

    def test_compte_bancaire_vide_stocke_en_none(self):
        """
        Un formulaire multipart envoie un champ laissé vide comme ''
        plutôt que de l'omettre — DocumentKYCSerializer.validate_compte_bancaire
        doit normaliser cette chaîne vide en None, seule représentation
        voulue de « pas de compte bancaire » en base.
        """
        from apps.utilisateurs.models import DocumentKYC

        payload = self._payload('passeport', avec_verso=False)
        payload['compte_bancaire'] = ''

        response = self.client.post(self.url, payload, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        dossier = DocumentKYC.objects.get(utilisateur=self.utilisateur)
        self.assertIsNone(dossier.compte_bancaire)


# =====================================================
# TESTS RESOUMISSION KYC APRÈS REFUS + CONCURRENCE
# =====================================================

class ResoumissionKYCTests(TestCase):
    """
    Un dossier KYC refusé doit pouvoir être resoumis (nouveaux fichiers,
    éventuellement nouveau type_piece) sans création d'un second dossier
    — et cette resoumission suffit à elle seule à repasser le compte en
    file d'attente (aucune vue séparée à appeler). Un dossier en_attente
    ou valide, lui, reste bloqué.
    """

    def setUp(self):
        from apps.utilisateurs.models import DocumentKYC, TypePieceIdentite, StatutKYC
        self.DocumentKYC = DocumentKYC
        self.TypePieceIdentite = TypePieceIdentite
        self.StatutKYC = StatutKYC

        self.client = APIClient()
        self.url = '/api/utilisateurs/upload-kyc/'
        self.utilisateur = Utilisateur.objects.create_user(
            email='kyc-resoumission@anitche.ci', password='TestPassword123!',
            nom='Test', prenom='KYC', email_verifie=True,
        )
        self.client.force_authenticate(user=self.utilisateur)

    def _creer_dossier_existant(self, statut):
        dossier = self.DocumentKYC.objects.create(
            utilisateur=self.utilisateur,
            type_piece=self.TypePieceIdentite.CNI,
            piece_identite_recto=_fichier_pdf_valide('ancien-recto.pdf'),
            piece_identite_verso=_fichier_pdf_valide('ancien-verso.pdf'),
            selfie=_fichier_selfie_valide(),
            numero_mobile_money='0700000000',
            adresse='Cocody, Abidjan',
        )
        if statut == self.StatutKYC.REFUSE:
            dossier.commentaire_admin = 'Motif du refus'
            dossier.date_traitement = timezone.now()
            dossier.save(update_fields=['commentaire_admin', 'date_traitement'])
        self.utilisateur.statut_kyc = statut
        self.utilisateur.save(update_fields=['statut_kyc'])
        return dossier

    def _payload(self, type_piece='passeport'):
        return {
            'type_piece': type_piece,
            'piece_identite_recto': _fichier_pdf_valide('nouveau-recto.pdf'),
            'selfie': _fichier_selfie_valide(),
            'numero_mobile_money': '0700000000',
            'adresse': 'Nouvelle adresse, Abidjan',
        }

    def test_resoumission_apres_refus_ok(self):
        from django.core.files.storage import default_storage

        dossier = self._creer_dossier_existant(self.StatutKYC.REFUSE)
        ancien_chemin_recto = dossier.piece_identite_recto.name
        ancien_chemin_verso = dossier.piece_identite_verso.name
        self.assertTrue(default_storage.exists(ancien_chemin_recto))

        # TestCase ne commite jamais réellement sa transaction (rollback
        # en fin de test) : sans ce helper, les callbacks transaction.
        # on_commit() enregistrés par create() ne s'exécuteraient jamais
        # et la suppression des anciens fichiers ne serait pas observable
        # ici, alors qu'elle a bien lieu en conditions réelles.
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self._payload('passeport'), format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.statut_kyc, self.StatutKYC.EN_ATTENTE)

        dossier.refresh_from_db()
        self.assertEqual(dossier.type_piece, 'passeport')
        self.assertIsNone(dossier.commentaire_admin)
        self.assertIsNone(dossier.date_traitement)
        # Le verso n'est plus pertinent pour un passeport et n'a pas été
        # redonné : il doit rester vide, pas hériter de l'ancien fichier.
        self.assertFalse(dossier.piece_identite_verso)

        # Anciens fichiers réellement supprimés du stockage, pas
        # seulement détachés en base.
        self.assertFalse(default_storage.exists(ancien_chemin_recto))
        self.assertFalse(default_storage.exists(ancien_chemin_verso))

        # Un seul dossier existe toujours pour ce compte.
        self.assertEqual(
            self.DocumentKYC.objects.filter(utilisateur=self.utilisateur).count(), 1
        )

    def test_echec_apres_remplacement_conserve_les_anciens_fichiers(self):
        """
        Si une erreur survient APRÈS le remplacement des fichiers en
        mémoire mais AVANT le commit (ici : soumettre_demande_vendeur()
        simulée en échec), la transaction doit intégralement annuler ses
        effets base ET ne jamais avoir touché au stockage — les anciens
        fichiers doivent toujours exister, puisque leur suppression est
        différée à transaction.on_commit().
        """
        from django.core.files.storage import default_storage

        dossier = self._creer_dossier_existant(self.StatutKYC.REFUSE)
        ancien_chemin_recto = dossier.piece_identite_recto.name
        ancien_chemin_verso = dossier.piece_identite_verso.name
        ancien_commentaire = dossier.commentaire_admin
        self.assertTrue(default_storage.exists(ancien_chemin_recto))
        self.assertTrue(default_storage.exists(ancien_chemin_verso))

        payload = self._payload('cni')
        payload['piece_identite_verso'] = _fichier_pdf_valide('nouveau-verso.pdf')

        # config.exceptions.custom_exception_handler convertit toute
        # exception non-DRF en réponse 500 propre plutôt que de la
        # laisser remonter comme exception Python brute — d'où l'assert
        # sur le code de statut plutôt que sur l'exception elle-même.
        # Le rollback de la transaction a bien eu lieu avant ça (il se
        # produit à la sortie du bloc `with transaction.atomic():`,
        # indépendamment de l'endroit où l'exception finit par être
        # interceptée dans la pile).
        with patch(
            'apps.utilisateurs.models.Utilisateur.soumettre_demande_vendeur',
            side_effect=RuntimeError('panne simulée après remplacement des fichiers'),
        ):
            response = self.client.post(self.url, payload, format='multipart')
            self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Rollback base : le dossier n'a jamais réellement changé.
        dossier.refresh_from_db()
        self.assertEqual(dossier.type_piece, 'cni')
        self.assertEqual(dossier.commentaire_admin, ancien_commentaire)
        self.assertEqual(dossier.piece_identite_recto.name, ancien_chemin_recto)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.statut_kyc, self.StatutKYC.REFUSE)

        # Stockage : les anciens fichiers n'ont jamais été supprimés,
        # puisque le on_commit() qui les efface n'a jamais été exécuté
        # (pas de commit réel — la transaction a été annulée).
        self.assertTrue(default_storage.exists(ancien_chemin_recto))
        self.assertTrue(default_storage.exists(ancien_chemin_verso))

    def test_resoumission_en_attente_refusee(self):
        self._creer_dossier_existant(self.StatutKYC.EN_ATTENTE)

        response = self.client.post(self.url, self._payload(), format='multipart')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.statut_kyc, self.StatutKYC.EN_ATTENTE)

    def test_resoumission_valide_refusee(self):
        self._creer_dossier_existant(self.StatutKYC.VALIDE)

        response = self.client.post(self.url, self._payload(), format='multipart')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.statut_kyc, self.StatutKYC.VALIDE)

    def test_upload_concurrent_pas_de_500(self):
        """
        Simule l'état observé par une seconde requête concurrente une
        fois le verrou de la première relâché : le dossier existe déjà
        (statut non_soumis, ni en_attente ni valide ni refuse) au moment
        où la seconde requête l'examine. Elle doit recevoir un refus
        propre (400), jamais une IntegrityError (500).
        """
        self.DocumentKYC.objects.create(
            utilisateur=self.utilisateur,
            type_piece=self.TypePieceIdentite.CNI,
            piece_identite_recto=_fichier_pdf_valide('recto.pdf'),
            piece_identite_verso=_fichier_pdf_valide('verso.pdf'),
            selfie=_fichier_selfie_valide(),
            numero_mobile_money='0700000000',
            adresse='Cocody, Abidjan',
        )

        response = self.client.post(self.url, self._payload('cni'), format='multipart')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nom_fichier_stocke_est_un_uuid(self):
        import re

        response = self.client.post(self.url, self._payload('passeport'), format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        dossier = self.DocumentKYC.objects.get(utilisateur=self.utilisateur)
        motif = re.compile(
            r'^kyc/pieces_identite/recto/'
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.pdf$'
        )
        self.assertRegex(dossier.piece_identite_recto.name, motif)
        # Le nom d'origine ne doit apparaître nulle part dans le chemin stocké.
        self.assertNotIn('nouveau-recto', dossier.piece_identite_recto.name)


class ResoumissionKYCConcurrenceTestCase(TransactionTestCase):
    """A04:2025 : deux tentatives d'upload KYC simultanées pour un compte
    sans dossier ne doivent jamais produire de 500 (IntegrityError sur la
    contrainte OneToOne)."""

    def setUp(self):
        self.utilisateur = Utilisateur.objects.create_user(
            email='kyc-concurrence@anitche.ci', password='TestPassword123!',
            nom='Test', prenom='KYC', email_verifie=True,
        )

    @skipUnless(
        connection.vendor == 'postgresql',
        "DIAGNOSTIC : select_for_update() est un no-op sur SQLite (pas de "
        "verrouillage de ligne) ; deux transactions d'écriture concurrentes "
        "s'y soldent par un comportement propre à SQLite, pas par le "
        "verrouillage réel visé par ce test — même limite déjà documentée "
        "sur apps.retours.tests.RetoursConcurrenceTestCase.",
    )
    def test_deux_uploads_simultanes_un_seul_accepte(self):
        from concurrent.futures import ThreadPoolExecutor
        import django.db

        url = '/api/utilisateurs/upload-kyc/'

        def appel():
            django.db.close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(user=self.utilisateur)
                data = {
                    'type_piece': 'passeport',
                    'piece_identite_recto': _fichier_pdf_valide('recto.pdf'),
                    'selfie': _fichier_selfie_valide(),
                    'numero_mobile_money': '0700000000',
                    'adresse': 'Cocody, Abidjan',
                }
                return client.post(url, data, format='multipart').status_code
            finally:
                django.db.connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuts = list(executor.map(lambda _: appel(), range(2)))

        self.assertNotIn(500, statuts)
        self.assertEqual(statuts.count(status.HTTP_201_CREATED), 1)
        self.assertEqual(statuts.count(status.HTTP_400_BAD_REQUEST), 1)


# =====================================================
# TESTS TÉLÉCHARGEMENT KYC — RECTO/VERSO
# =====================================================

class TelechargerDocumentKYCTests(TestCase):
    """
    Vérifie le contrôle d'accès de TelechargerDocumentKYCView sur les
    deux faces du document (CHAMPS_AUTORISES doit inclure recto et
    verso) : accès pour le propriétaire, refus pour un tiers.
    """

    def setUp(self):
        from apps.utilisateurs.models import DocumentKYC, TypePieceIdentite
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client = APIClient()
        self.proprietaire = Utilisateur.objects.create_user(
            email='kyc-proprietaire@anitche.ci', password='TestPassword123!',
            nom='Test', prenom='KYC',
        )
        self.autre_utilisateur = Utilisateur.objects.create_user(
            email='kyc-tiers@anitche.ci', password='TestPassword123!',
            nom='Tiers', prenom='Intrus',
        )
        self.utilisateur_sans_dossier = Utilisateur.objects.create_user(
            email='kyc-sans-dossier@anitche.ci', password='TestPassword123!',
            nom='Sans', prenom='Dossier',
        )
        DocumentKYC.objects.create(
            utilisateur=self.proprietaire,
            type_piece=TypePieceIdentite.CNI,
            piece_identite_recto=SimpleUploadedFile("recto.pdf", b"%PDF-1.4\nfake"),
            piece_identite_verso=SimpleUploadedFile("verso.pdf", b"%PDF-1.4\nfake"),
            selfie=SimpleUploadedFile("selfie.png", b"\x89PNG\r\n\x1a\nfake"),
            numero_mobile_money='0700000000',
            adresse='Cocody, Abidjan',
        )

    def _url(self, champ):
        return f'/api/utilisateurs/kyc/{self.proprietaire.id}/{champ}/'

    def test_proprietaire_telecharge_le_recto(self):
        self.client.force_authenticate(user=self.proprietaire)
        response = self.client.get(self._url('piece_identite_recto'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_proprietaire_telecharge_le_verso(self):
        self.client.force_authenticate(user=self.proprietaire)
        response = self.client.get(self._url('piece_identite_verso'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_autre_utilisateur_refuse_sur_le_recto(self):
        self.client.force_authenticate(user=self.autre_utilisateur)
        response = self.client.get(self._url('piece_identite_recto'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_autre_utilisateur_refuse_sur_le_verso(self):
        self.client.force_authenticate(user=self.autre_utilisateur)
        response = self.client.get(self._url('piece_identite_verso'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_autre_utilisateur_sur_cible_sans_dossier_recoit_403_pas_404(self):
        """
        Broken Access Control : l'autorisation est vérifiée AVANT toute
        recherche du dossier. Un tiers non autorisé doit recevoir le même
        403 que la cible ait ou non soumis un KYC — sinon un 404 ici et
        un 403 là (test_autre_utilisateur_refuse_sur_le_recto) servirait
        d'oracle pour deviner qui a soumis un dossier KYC.
        """
        self.client.force_authenticate(user=self.autre_utilisateur)
        url = f'/api/utilisateurs/kyc/{self.utilisateur_sans_dossier.id}/piece_identite_recto/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UtilisateurManagerTestCase(TestCase):
    """create_superuser() doit produire un compte qui a réellement les
    droits admin métier (role=super_admin), pas seulement is_staff/
    is_superuser côté Django admin. Sans quoi un superuser créé via
    `manage.py createsuperuser` ne peut administrer aucune route métier
    (paiements, livraison, support, vendeurs, retours...) qui vérifie
    user.role plutôt que is_staff."""

    def test_create_superuser_donne_le_role_super_admin(self):
        admin = Utilisateur.objects.create_superuser(
            email="superadmin@anitche.ci",
            password="TestPassword123!",
            nom="Admin",
            prenom="Anitche",
        )
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertEqual(admin.role, Role.SUPER_ADMIN)

    def test_create_superuser_respecte_un_role_explicite(self):
        """Si un rôle est explicitement fourni, on ne l'écrase pas."""
        admin = Utilisateur.objects.create_superuser(
            email="admin-explicite@anitche.ci",
            password="TestPassword123!",
            nom="Admin",
            prenom="Explicite",
            role=Role.ADMIN,
        )
        self.assertEqual(admin.role, Role.ADMIN)

    def test_create_user_classique_garde_le_role_client_par_defaut(self):
        """Non-régression : create_user (inscription normale) ne doit pas
        être affecté par ce correctif et garder le rôle par défaut du modèle."""
        client = Utilisateur.objects.create_user(
            email="client-normal@anitche.ci",
            password="TestPassword123!",
            nom="Client",
            prenom="Normal",
        )
        self.assertEqual(client.role, Role.CLIENT)
        self.assertFalse(client.is_staff)
        self.assertFalse(client.is_superuser)

# =====================================================
# PASSE 2 (septembre 2026) : chiffrement, IP, OTP, email vérifié, limites
# =====================================================

import importlib
import io as _io
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from rest_framework.throttling import SimpleRateThrottle

from apps.core.fields import MESSAGE_VALEUR_ILLISIBLE, ValeurIllisible, chiffreur
from .models import DocumentKYC, StatutKYC
from .permissions import CODE_EMAIL_NON_VERIFIE

CLE_A = Fernet.generate_key().decode()
CLE_B = Fernet.generate_key().decode()


def avec_proxys(nombre):
    return override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, 'NUM_PROXIES': nombre})


def colonnes_brutes(dossier):
    with connection.cursor() as curseur:
        curseur.execute(
            "SELECT numero_mobile_money, compte_bancaire FROM utilisateurs_documentkyc WHERE id = %s",
            [dossier.pk],
        )
        return curseur.fetchone()


def ecrire_jeton_brut(dossier, colonne, jeton):
    """Écrit un jeton tel quel (sans passer par le champ, qui le chiffrerait)."""
    with connection.cursor() as curseur:
        curseur.execute(f"UPDATE utilisateurs_documentkyc SET {colonne} = %s WHERE id = %s", [jeton, dossier.pk])


def creer_dossier(utilisateur, **champs):
    valeurs = {
        'type_piece': 'passeport', 'piece_identite_recto': 'kyc/recto.pdf', 'selfie': 'kyc/selfie.png',
        'numero_mobile_money': '0707070707', 'adresse': 'Cocody', 'compte_bancaire': 'CI93 CI0080 1112',
        **champs,
    }
    return DocumentKYC.objects.create(utilisateur=utilisateur, **valeurs)


class ChiffrementDonneesKYCTests(TestCase):
    """1.1 : numero_mobile_money et compte_bancaire chiffrés au repos (Fernet)."""

    def setUp(self):
        self.utilisateur = Utilisateur.objects.create_user(
            email='kyc-chiffrement@anitche.ci', password='TestPassword123!', nom='T', prenom='K', email_verifie=True,
        )

    def test_depot_chiffre_en_base_et_lisible_par_lapplication(self):
        # Avant : les deux valeurs étaient stockées en clair.
        client = APIClient()
        client.force_authenticate(self.utilisateur)
        response = client.post('/api/utilisateurs/upload-kyc/', {
            'type_piece': 'passeport', 'piece_identite_recto': _fichier_pdf_valide('r.pdf'),
            'selfie': _fichier_selfie_valide(), 'numero_mobile_money': '0707070707',
            'adresse': 'Cocody', 'compte_bancaire': 'CI93 CI0080 1112',
        }, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        dossier = DocumentKYC.objects.get(utilisateur=self.utilisateur)
        mobile_brut, bancaire_brut = colonnes_brutes(dossier)
        self.assertNotIn('0707070707', mobile_brut)
        self.assertNotIn('CI93', bancaire_brut)
        self.assertEqual(chiffreur().decrypt(mobile_brut.encode()).decode(), '0707070707')
        self.assertEqual((dossier.numero_mobile_money, dossier.compte_bancaire), ('0707070707', 'CI93 CI0080 1112'))

    def test_longueurs_metier_toujours_validees(self):
        client = APIClient()
        client.force_authenticate(self.utilisateur)
        response = client.post('/api/utilisateurs/upload-kyc/', {
            'type_piece': 'passeport', 'piece_identite_recto': _fichier_pdf_valide('r.pdf'),
            'selfie': _fichier_selfie_valide(), 'numero_mobile_money': '0' * 21,
            'adresse': 'Cocody', 'compte_bancaire': 'X' * 51,
        }, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('numero_mobile_money', response.data['errors'])
        self.assertIn('compte_bancaire', response.data['errors'])

    def test_valeur_illisible_jamais_ecrasee(self):
        # Avant : une clé erronée faisait lire « [valeur illisible…] », et
        # la sauvegarde suivante chiffrait ce message à la place de l'IBAN.
        dossier = creer_dossier(self.utilisateur)
        jeton_etranger = Fernet(Fernet.generate_key()).encrypt(b'CI93 AUTRE CLE').decode()
        ecrire_jeton_brut(dossier, 'compte_bancaire', jeton_etranger)

        dossier = DocumentKYC.objects.get(pk=dossier.pk)
        self.assertIsInstance(dossier.compte_bancaire, ValeurIllisible)
        self.assertEqual(dossier.compte_bancaire, MESSAGE_VALEUR_ILLISIBLE)
        dossier.adresse = 'Plateau'
        dossier.save()

        self.assertEqual(colonnes_brutes(dossier)[1], jeton_etranger)
        self.assertEqual(DocumentKYC.objects.get(pk=dossier.pk).numero_mobile_money, '0707070707')

    def test_rotation_de_cle(self):
        with override_settings(FIELD_ENCRYPTION_KEYS=[CLE_A]):
            dossier = creer_dossier(self.utilisateur)
        # Nouvelle clé en tête, ancienne derrière : tout reste lisible.
        with override_settings(FIELD_ENCRYPTION_KEYS=[CLE_B, CLE_A]):
            self.assertEqual(DocumentKYC.objects.get(pk=dossier.pk).compte_bancaire, 'CI93 CI0080 1112')
            sortie = _io.StringIO()
            call_command('rechiffrer_donnees_sensibles', stdout=sortie)
            self.assertIn('Total : 2 re-chiffrée(s), 0 illisible(s)', sortie.getvalue())
        # Ancienne clé retirée : toujours lisible, car tout a été re-chiffré.
        with override_settings(FIELD_ENCRYPTION_KEYS=[CLE_B]):
            dossier = DocumentKYC.objects.get(pk=dossier.pk)
            self.assertEqual((dossier.numero_mobile_money, dossier.compte_bancaire), ('0707070707', 'CI93 CI0080 1112'))

    def test_simulation_et_valeurs_illisibles_signalees(self):
        dossier = creer_dossier(self.utilisateur)
        jeton_etranger = Fernet(Fernet.generate_key()).encrypt(b'X').decode()
        ecrire_jeton_brut(dossier, 'compte_bancaire', jeton_etranger)
        avant = colonnes_brutes(dossier)

        sortie, erreurs = _io.StringIO(), _io.StringIO()
        call_command('rechiffrer_donnees_sensibles', '--simulation', stdout=sortie, stderr=erreurs)
        self.assertIn('Total : 1 re-chiffrée(s), 1 illisible(s) (simulation', sortie.getvalue())
        self.assertIn('Ne retirez aucune ancienne clé', erreurs.getvalue())
        self.assertEqual(colonnes_brutes(dossier), avant)


class MigrationChiffrementKYCTests(TransactionTestCase):
    """Migration 0008 : les valeurs existantes en clair sont chiffrées ; le
    retour arrière les déchiffre."""

    AVANT = [('utilisateurs', '0007_kyc_upload_to_uuid')]
    APRES = [('utilisateurs', '0008_chiffrement_donnees_financieres_kyc')]

    def tearDown(self):
        executeur = MigrationExecutor(connection)
        executeur.migrate(executeur.loader.graph.leaf_nodes())

    def test_migration_aller_retour(self):
        executeur = MigrationExecutor(connection)
        executeur.migrate(self.AVANT)
        anciennes_apps = executeur.loader.project_state(self.AVANT).apps
        AncienUtilisateur = anciennes_apps.get_model('utilisateurs', 'Utilisateur')
        AncienDossier = anciennes_apps.get_model('utilisateurs', 'DocumentKYC')
        utilisateur = AncienUtilisateur.objects.create(email='migration@anitche.ci', nom='M', prenom='G', password='x')
        dossier = AncienDossier.objects.create(
            utilisateur=utilisateur, type_piece='passeport', piece_identite_recto='kyc/r.pdf', selfie='kyc/s.png',
            numero_mobile_money='0707070707', adresse='Cocody', compte_bancaire='CI93 CI0080 1112',
        )

        executeur = MigrationExecutor(connection)
        executeur.migrate(self.APRES)
        mobile_brut, bancaire_brut = colonnes_brutes(dossier)
        self.assertEqual(chiffreur().decrypt(mobile_brut.encode()).decode(), '0707070707')
        self.assertEqual(chiffreur().decrypt(bancaire_brut.encode()).decode(), 'CI93 CI0080 1112')

        executeur = MigrationExecutor(connection)
        executeur.migrate(self.AVANT)
        self.assertEqual(colonnes_brutes(dossier), ('0707070707', 'CI93 CI0080 1112'))


class NotificationConnexionIPTests(TestCase):
    """1.2 : l'IP de la notification vient de adresse_ip_client (proxy de confiance)."""

    def setUp(self):
        self.client = APIClient()
        Utilisateur.objects.create_user(
            email='notif@anitche.ci', password='TestPassword123!', nom='N', prenom='T', email_verifie=True,
        )

    def test_connexion_par_mot_de_passe(self):
        # Avant : REMOTE_ADDR, c'est-à-dire l'IP du conteneur Nginx en production.
        with avec_proxys(1), patch('apps.utilisateurs.views.envoyer_notification_connexion.delay') as envoi:
            response = self.client.post(
                '/api/utilisateurs/connexion/', {'email': 'notif@anitche.ci', 'password': 'TestPassword123!'},
                REMOTE_ADDR='172.18.0.5', HTTP_X_FORWARDED_FOR='41.66.1.2',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(envoi.call_args.args[1], '41.66.1.2')

    def test_connexion_google(self):
        infos = {'sub': 'g-notif', 'email': 'notif@anitche.ci', 'email_verified': True}
        with avec_proxys(1), \
                patch('apps.utilisateurs.views.google_id_token.verify_oauth2_token', return_value=infos), \
                patch('apps.utilisateurs.views.envoyer_notification_connexion.delay') as envoi:
            response = self.client.post(
                '/api/utilisateurs/connexion-google/', {'id_token': 'x'},
                REMOTE_ADDR='172.18.0.5', HTTP_X_FORWARDED_FOR='41.66.1.2',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(envoi.call_args.args[1], '41.66.1.2')

    def test_sans_proxy_en_tete_ignore(self):
        with patch('apps.utilisateurs.views.envoyer_notification_connexion.delay') as envoi:
            self.client.post(
                '/api/utilisateurs/connexion/', {'email': 'notif@anitche.ci', 'password': 'TestPassword123!'},
                REMOTE_ADDR='10.0.0.7', HTTP_X_FORWARDED_FOR='6.6.6.6',
            )
        self.assertEqual(envoi.call_args.args[1], '10.0.0.7')


class MessageOTPDureeTests(TestCase):
    def test_duree_affichee_egale_a_la_duree_reelle(self):
        # Avant : « 10 minutes » en dur (réglage OTP_DUREE_VALIDITE_MINUTES inexistant).
        Utilisateur.objects.create_user(email='otp-duree@anitche.ci', password='TestPassword123!', nom='O', prenom='D')
        with patch.object(CodeOTP, 'DUREE_VALIDITE_MINUTES', 3):
            APIClient().post('/api/utilisateurs/mot-de-passe-oublie/', {'email': 'otp-duree@anitche.ci'})
        self.assertIn('Ce code expire dans 3 minutes.', mail.outbox[-1].body)


class DocumentKYCAbsentDuStockageTests(TestCase):
    def test_piece_perdue_404_et_non_500(self):
        utilisateur = Utilisateur.objects.create_user(email='kyc-perdu@anitche.ci', password='x', nom='K', prenom='P')
        creer_dossier(utilisateur, piece_identite_recto='kyc/fichier-disparu.pdf')
        client = APIClient()
        client.force_authenticate(utilisateur)
        with self.assertLogs('securite', level='ERROR'):
            response = client.get(f'/api/utilisateurs/kyc/{utilisateur.pk}/piece_identite_recto/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class EmailVerifieTests(TestCase):
    """Q1 : connexion libre, actions sensibles réservées aux emails vérifiés."""

    def setUp(self):
        self.client = APIClient()
        self.utilisateur = Utilisateur.objects.create_user(
            email='non-verifie@anitche.ci', password='TestPassword123!', nom='N', prenom='V',
        )

    def test_connexion_toujours_possible(self):
        response = self.client.post(
            '/api/utilisateurs/connexion/', {'email': 'non-verifie@anitche.ci', 'password': 'TestPassword123!'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_actions_sensibles_refusees_avec_code_machine(self):
        self.client.force_authenticate(self.utilisateur)
        actions = {
            'dépôt KYC': ('/api/utilisateurs/upload-kyc/', {}),
            'changement de contact': ('/api/utilisateurs/changement-contact/', {'nouvel_email': 'x@anitche.ci'}),
            'validation du panier': ('/api/commandes/valider-panier/', {}),
        }
        for libelle, (url, corps) in actions.items():
            with self.subTest(libelle):
                response = self.client.post(url, corps)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertEqual(response.data['errors']['code'], CODE_EMAIL_NON_VERIFIE)
        self.assertFalse(DocumentKYC.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_renvoi_du_code_puis_verification(self):
        self.client.force_authenticate(self.utilisateur)
        response = self.client.post('/api/utilisateurs/renvoyer-code-inscription/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mail.outbox[-1].to, ['non-verifie@anitche.ci'])
        code = mail.outbox[-1].body.split('est : ')[1][:6]

        response = self.client.post('/api/utilisateurs/verification-otp/', {'code': code, 'type_usage': 'inscription'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.utilisateur.refresh_from_db()
        self.assertTrue(self.utilisateur.email_verifie)

        response = self.client.post('/api/utilisateurs/renvoyer-code-inscription/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_renvoi_reserve_aux_comptes_connectes(self):
        response = self.client.post('/api/utilisateurs/renvoyer-code-inscription/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class InscriptionPasse2Tests(TestCase):
    """Q2 : téléphone retiré de l'inscription ; limite dédiée par IP."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.corps = {'password': 'TestPassword123!', 'nom': 'I', 'prenom': 'N'}

    def test_telephone_refuse_explicitement(self):
        response = self.client.post('/api/utilisateurs/inscription/', {
            **self.corps, 'email': 'tel@anitche.ci', 'telephone': '0101010101',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('telephone', response.data['errors'])
        self.assertFalse(Utilisateur.objects.filter(email='tel@anitche.ci').exists())

    def test_inscription_sans_telephone(self):
        response = self.client.post('/api/utilisateurs/inscription/', {**self.corps, 'email': 'neuf@anitche.ci'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn('telephone', response.data)

    def test_limite_dediee_par_ip_non_contournable(self):
        with patch.object(SimpleRateThrottle, 'THROTTLE_RATES', {'inscription': '2/hour'}):
            codes = [
                self.client.post('/api/utilisateurs/inscription/', {**self.corps, 'email': f'i{i}@anitche.ci'},
                                 REMOTE_ADDR='9.9.9.9', HTTP_X_FORWARDED_FOR=f'1.1.1.{i}').status_code
                for i in range(3)
            ]
        self.assertEqual(codes, [201, 201, 429])


class LimitesOTPSepareesTests(TestCase):
    """Q5 : envoi et vérification des codes comptés séparément."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.utilisateur = Utilisateur.objects.create_user(
            email='otp-scopes@anitche.ci', password='TestPassword123!', nom='O', prenom='S', email_verifie=True,
        )

    def test_verification_possible_apres_plusieurs_envois(self):
        # Avant : un seul compteur 'otp' ; quelques demandes de code bloquaient la vérification.
        taux = {'otp_envoi': '2/hour', 'otp_verification': '10/hour'}
        self.client.force_authenticate(self.utilisateur)
        with patch.object(SimpleRateThrottle, 'THROTTLE_RATES', taux):
            envois = [
                self.client.post('/api/utilisateurs/changement-contact/', {'nouvel_email': f'n{i}@anitche.ci'}).status_code
                for i in range(3)
            ]
            verification = self.client.post(
                '/api/utilisateurs/verification-otp/', {'code': '000000', 'type_usage': 'changement_email'},
            ).status_code
        self.assertEqual(envois, [200, 200, 429])
        self.assertEqual(verification, status.HTTP_400_BAD_REQUEST)  # code faux, mais pas bloqué

    def test_mot_de_passe_oublie_confirmation_apres_plusieurs_demandes(self):
        taux = {'otp_envoi': '2/hour', 'otp_verification': '10/hour'}
        with patch.object(SimpleRateThrottle, 'THROTTLE_RATES', taux):
            for _ in range(2):
                self.client.post('/api/utilisateurs/mot-de-passe-oublie/', {'email': 'otp-scopes@anitche.ci'})
            confirmation = self.client.post('/api/utilisateurs/mot-de-passe-oublie/confirmer/', {
                'email': 'otp-scopes@anitche.ci', 'code': '000000', 'nouveau_password': 'NouveauMotDePasse123!',
            }).status_code
        self.assertEqual(confirmation, status.HTTP_400_BAD_REQUEST)


class RafraichissementLimiteTests(TestCase):
    """Q6 : le rafraîchissement ne partage plus le compteur 'anon' de l'API."""

    def test_limite_dediee(self):
        cache.clear()
        client = APIClient()
        Utilisateur.objects.create_user(email='refresh@anitche.ci', password='TestPassword123!', nom='R', prenom='F')
        refresh = client.post(
            '/api/utilisateurs/connexion/', {'email': 'refresh@anitche.ci', 'password': 'TestPassword123!'},
        ).data['refresh']
        taux = {'anon': '1/hour', 'rafraichissement': '2/hour', 'inscription': '100/hour'}
        with patch.object(SimpleRateThrottle, 'THROTTLE_RATES', taux):
            client.get('/api/vendeurs/boutiques/')  # épuise 'anon'
            premier = client.post('/api/utilisateurs/connexion/rafraichir/', {'refresh': refresh})
            second = client.post('/api/utilisateurs/connexion/rafraichir/', {'refresh': premier.data['refresh']})
            troisieme = client.post('/api/utilisateurs/connexion/rafraichir/', {'refresh': second.data['refresh']})
        self.assertEqual((premier.status_code, second.status_code, troisieme.status_code), (200, 200, 429))


class TelephoneNonVerifieSansSMSTests(TestCase):
    def test_changement_de_telephone_ne_le_marque_pas_verifie(self):
        # Décision d'équipe (Q7) : le code part sur l'email, pas au numéro.
        utilisateur = Utilisateur.objects.create_user(
            email='tel-sms@anitche.ci', password='TestPassword123!', nom='T', prenom='S', email_verifie=True,
        )
        client = APIClient()
        client.force_authenticate(utilisateur)
        client.post('/api/utilisateurs/changement-contact/', {'nouveau_telephone': '0505050505'})
        self.assertEqual(mail.outbox[-1].to, ['tel-sms@anitche.ci'])
        code = mail.outbox[-1].body.split('est : ')[1][:6]
        response = client.post('/api/utilisateurs/verification-otp/', {'code': code, 'type_usage': 'changement_telephone'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        utilisateur.refresh_from_db()
        self.assertEqual((utilisateur.telephone, utilisateur.telephone_verifie), ('0505050505', False))


class MediaDeTestIsoleTests(TestCase):
    def test_les_tests_necrivent_pas_dans_le_media_du_projet(self):
        self.assertNotEqual(Path(settings.MEDIA_ROOT).resolve(), (Path(settings.BASE_DIR) / 'media').resolve())


class LimitesDeProductionTests(TestCase):
    """Les limites testées avec les VRAIES valeurs de base.py (test.py les
    relâche pour le reste de la suite ; dev.py les relève pour Postman)."""

    def setUp(self):
        from apps.core.tests import taux_de_production

        cache.clear()
        self.taux = taux_de_production()
        self.client = APIClient()
        Utilisateur.objects.create_user(
            email='limites@anitche.ci', password='TestPassword123!', nom='L', prenom='P', email_verifie=True,
        )

    def seuil(self, scope):
        return int(self.taux[scope].split('/')[0])

    def codes(self, nombre, appel):
        with patch.object(SimpleRateThrottle, 'THROTTLE_RATES', self.taux):
            return [appel(i) for i in range(nombre)]

    def test_connexion(self):
        n = self.seuil('login')
        codes = self.codes(n + 1, lambda i: self.client.post(
            '/api/utilisateurs/connexion/', {'email': 'limites@anitche.ci', 'password': 'TestPassword123!'},
        ).status_code)
        self.assertEqual(codes, [200] * n + [429])

    def test_inscription(self):
        n = self.seuil('inscription')
        codes = self.codes(n + 1, lambda i: self.client.post('/api/utilisateurs/inscription/', {
            'email': f'insc{i}@anitche.ci', 'password': 'TestPassword123!', 'nom': 'I', 'prenom': 'N',
        }).status_code)
        self.assertEqual(codes, [201] * n + [429])

    def test_envoi_et_verification_de_codes(self):
        n_envoi, n_verif = self.seuil('otp_envoi'), self.seuil('otp_verification')
        envois = self.codes(n_envoi + 1, lambda i: self.client.post(
            '/api/utilisateurs/mot-de-passe-oublie/', {'email': 'limites@anitche.ci'},
        ).status_code)
        self.assertEqual(envois, [200] * n_envoi + [429])
        verifications = self.codes(n_verif + 1, lambda i: self.client.post(
            '/api/utilisateurs/mot-de-passe-oublie/confirmer/',
            {'email': 'limites@anitche.ci', 'code': '000000', 'nouveau_password': 'NouveauMotDePasse123!'},
        ).status_code)
        self.assertEqual(verifications, [400] * n_verif + [429])
