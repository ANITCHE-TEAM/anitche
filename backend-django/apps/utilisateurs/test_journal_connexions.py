from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.db import DatabaseError, IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tests import avec_proxys

from .admin import JournalConnexionAdmin
from .models import JournalConnexion, MethodeConnexion, MotifEchecConnexion, Role, Utilisateur
from .tasks import purger_journal_connexions

URL_CONNEXION = '/api/utilisateurs/connexion/'
URL_GOOGLE = '/api/utilisateurs/connexion-google/'
URL_MES_CONNEXIONS = '/api/utilisateurs/connexions/'
URL_ADMIN_CONNEXIONS = '/api/utilisateurs/administration/connexions/'
MOT_DE_PASSE = 'MotDePasseSolide123!'
VERIFIER_GOOGLE = 'apps.utilisateurs.views.google_id_token.verify_oauth2_token'
ENVOI_NOTIFICATION = 'apps.utilisateurs.views.envoyer_notification_connexion.delay'


def creer(email, **extra):
    return Utilisateur.objects.create_user(
        email=email, password=MOT_DE_PASSE, nom='N', prenom='P', email_verifie=True, **extra,
    )


def ecrire(utilisateur=None, email='x@anitche.ci', reussie=True, il_y_a=None, **extra):
    """Ligne du journal, datée `il_y_a` (timedelta) avant maintenant."""
    champs = {'methode': MethodeConnexion.MOT_DE_PASSE, 'motif_echec': '' if reussie else 'email_inconnu'}
    champs.update(extra)
    ligne = JournalConnexion.objects.create(utilisateur=utilisateur, email_tente=email, reussie=reussie, **champs)
    if il_y_a is not None:
        JournalConnexion.objects.filter(pk=ligne.pk).update(date_creation=timezone.now() - il_y_a)
    return ligne


class EcritureConnexionMotDePasseTests(TestCase):
    """POST connexion/ : chaque tentative laisse une ligne."""

    def setUp(self):
        self.client = APIClient()
        self.utilisateur = creer('awa@anitche.ci')

    def connecter(self, email='awa@anitche.ci', password=MOT_DE_PASSE, **meta):
        with patch(ENVOI_NOTIFICATION):
            return self.client.post(URL_CONNEXION, {'email': email, 'password': password}, **meta)

    def test_reussite(self):
        reponse = self.connecter('AWA@Anitche.CI', HTTP_USER_AGENT='Firefox/130')
        self.assertEqual(reponse.status_code, 200)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, self.utilisateur)
        self.assertEqual(ligne.email_tente, 'awa@anitche.ci')
        self.assertTrue(ligne.reussie)
        self.assertEqual(ligne.motif_echec, '')
        self.assertEqual(ligne.methode, MethodeConnexion.MOT_DE_PASSE)
        self.assertEqual(ligne.adresse_ip, '127.0.0.1')
        self.assertEqual(ligne.user_agent, 'Firefox/130')

    def test_mot_de_passe_incorrect(self):
        reponse = self.connecter(password='MauvaisMotDePasse')
        self.assertEqual(reponse.status_code, 401)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, self.utilisateur)
        self.assertFalse(ligne.reussie)
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.MOT_DE_PASSE_INCORRECT)

    def test_email_inconnu_sans_utilisateur_et_en_minuscules(self):
        reponse = self.connecter(email='Inconnu@Anitche.CI')
        self.assertEqual(reponse.status_code, 401)
        ligne = JournalConnexion.objects.get()
        self.assertIsNone(ligne.utilisateur)
        self.assertEqual(ligne.email_tente, 'inconnu@anitche.ci')
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.EMAIL_INCONNU)

    def test_compte_desactive(self):
        Utilisateur.objects.filter(pk=self.utilisateur.pk).update(is_active=False)
        reponse = self.connecter()
        self.assertEqual(reponse.status_code, 401)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, self.utilisateur)
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.COMPTE_DESACTIVE)

    def test_la_reponse_ne_depend_pas_du_motif(self):
        inconnu = self.connecter(email='inconnu@anitche.ci')
        incorrect = self.connecter(password='MauvaisMotDePasse')
        self.assertEqual(inconnu.status_code, incorrect.status_code)
        self.assertEqual(inconnu.json(), incorrect.json())

    def test_requete_mal_formee_non_journalisee(self):
        reponse = self.client.post(URL_CONNEXION, {'email': 'awa@anitche.ci'})
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(JournalConnexion.objects.count(), 0)

    def test_email_non_texte(self):
        reponse = self.client.post(URL_CONNEXION, {'email': 12345, 'password': 'x'}, format='json')
        self.assertEqual(reponse.status_code, 401)
        self.assertEqual(JournalConnexion.objects.get().motif_echec, MotifEchecConnexion.EMAIL_INCONNU)

    def test_notification_par_email_conservee(self):
        with patch(ENVOI_NOTIFICATION) as envoi:
            self.client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': MOT_DE_PASSE})
        envoi.assert_called_once()
        self.assertEqual(envoi.call_args.args[0], 'awa@anitche.ci')


class EcritureConnexionGoogleTests(TestCase):
    """POST connexion-google/ : réussites et refus journalisés."""

    def setUp(self):
        self.client = APIClient()
        self.infos = {
            'sub': 'google-id-1', 'email': 'Awa@Anitche.ci', 'email_verified': True,
            'family_name': 'K', 'given_name': 'A',
        }

    def connecter(self, infos=None, **meta):
        with patch(VERIFIER_GOOGLE, return_value=self.infos if infos is None else infos), patch(ENVOI_NOTIFICATION):
            return self.client.post(URL_GOOGLE, {'id_token': 'jeton-google'}, **meta)

    def test_reussite_compte_existant(self):
        utilisateur = creer('awa@anitche.ci')
        reponse = self.connecter(HTTP_USER_AGENT='Chrome/130')
        self.assertEqual(reponse.status_code, 200)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, utilisateur)
        self.assertEqual(ligne.email_tente, 'awa@anitche.ci')
        self.assertTrue(ligne.reussie)
        self.assertEqual(ligne.methode, MethodeConnexion.GOOGLE)
        self.assertEqual(ligne.user_agent, 'Chrome/130')

    def test_reussite_compte_cree_par_google(self):
        self.assertEqual(self.connecter().status_code, 200)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, Utilisateur.objects.get(email='awa@anitche.ci'))
        self.assertTrue(ligne.reussie)

    def test_jeton_google_invalide(self):
        with patch(VERIFIER_GOOGLE, side_effect=ValueError('jeton')):
            reponse = self.client.post(URL_GOOGLE, {'id_token': 'jeton-google'})
        self.assertEqual(reponse.status_code, 400)
        ligne = JournalConnexion.objects.get()
        self.assertFalse(ligne.reussie)
        self.assertIsNone(ligne.utilisateur)
        self.assertEqual(ligne.email_tente, '')
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.GOOGLE_JETON_INVALIDE)
        self.assertEqual(ligne.methode, MethodeConnexion.GOOGLE)

    def test_informations_incompletes(self):
        reponse = self.connecter({**self.infos, 'email_verified': False})
        self.assertEqual(reponse.status_code, 400)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.GOOGLE_INFOS_INCOMPLETES)
        self.assertEqual(ligne.email_tente, 'awa@anitche.ci')
        self.assertIsNone(ligne.utilisateur)

    def test_compte_desactive(self):
        utilisateur = creer('awa@anitche.ci', is_active=False)
        reponse = self.connecter()
        self.assertEqual(reponse.status_code, 403)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, utilisateur)
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.COMPTE_DESACTIVE)

    def test_compte_desactive_lie_a_google(self):
        utilisateur = creer('awa@anitche.ci', is_active=False, google_id='google-id-1')
        self.assertEqual(self.connecter().status_code, 403)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, utilisateur)
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.COMPTE_DESACTIVE)

    def test_liaison_refusee(self):
        utilisateur = Utilisateur.objects.create_user(
            email='awa@anitche.ci', password='mot-de-passe-attaquant', nom='N', prenom='P',
        )
        reponse = self.connecter()
        self.assertEqual(reponse.status_code, 409)
        ligne = JournalConnexion.objects.get()
        self.assertEqual(ligne.utilisateur, utilisateur)
        self.assertEqual(ligne.motif_echec, MotifEchecConnexion.GOOGLE_LIAISON_REFUSEE)

    def test_notification_par_email_conservee(self):
        creer('awa@anitche.ci')
        with patch(VERIFIER_GOOGLE, return_value=self.infos), patch(ENVOI_NOTIFICATION) as envoi:
            self.client.post(URL_GOOGLE, {'id_token': 'jeton-google'})
        envoi.assert_called_once()


class JournalConnexionSecretsTests(TestCase):
    """Le journal ne contient ni mot de passe, ni jeton, ni code."""

    def test_aucun_secret_dans_le_journal(self):
        client = APIClient()
        creer('awa@anitche.ci')
        with patch(ENVOI_NOTIFICATION):
            reussie = client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': MOT_DE_PASSE})
            client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': 'MauvaisSecret987!'})
            with patch(VERIFIER_GOOGLE, return_value={'sub': 'g', 'email': 'awa@anitche.ci', 'email_verified': True}):
                google = client.post(URL_GOOGLE, {'id_token': 'jeton-google-secret'})
        secrets = [
            MOT_DE_PASSE, 'MauvaisSecret987!', 'jeton-google-secret',
            reussie.json()['access'], reussie.json()['refresh'], google.json()['access'], google.json()['refresh'],
        ]

        self.assertEqual(JournalConnexion.objects.count(), 3)
        for ligne in JournalConnexion.objects.all().values():
            contenu = ' '.join(str(valeur) for valeur in ligne.values())
            for secret in secrets:
                self.assertNotIn(secret, contenu)

        client.force_authenticate(Utilisateur.objects.get(email='awa@anitche.ci'))
        contenu = client.get(URL_MES_CONNEXIONS).content.decode()
        for secret in secrets:
            self.assertNotIn(secret, contenu)

    def test_champs_exposes_a_l_utilisateur(self):
        utilisateur = creer('awa@anitche.ci')
        ecrire(utilisateur, 'awa@anitche.ci')
        client = APIClient()
        client.force_authenticate(utilisateur)
        ligne = client.get(URL_MES_CONNEXIONS).json()['results'][0]
        self.assertEqual(
            set(ligne), {'id', 'date_creation', 'reussie', 'motif_echec', 'methode', 'adresse_ip', 'user_agent'},
        )


class AdresseIPJournalTests(TestCase):
    """L'IP vient de adresse_ip_client : X-Forwarded-For du client ignoré."""

    def setUp(self):
        self.client = APIClient()
        creer('awa@anitche.ci')

    def connecter(self, **meta):
        with patch(ENVOI_NOTIFICATION):
            return self.client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': MOT_DE_PASSE}, **meta)

    def test_sans_proxy_l_en_tete_du_client_est_ignore(self):
        self.connecter(REMOTE_ADDR='10.0.0.7', HTTP_X_FORWARDED_FOR='6.6.6.6')
        self.assertEqual(JournalConnexion.objects.get().adresse_ip, '10.0.0.7')

    def test_derriere_le_proxy_de_confiance_seule_la_derniere_entree_compte(self):
        # Nginx écrase X-Forwarded-For avec l'IP réelle ; une valeur forgée
        # placée avant elle par le client n'est jamais retenue.
        with avec_proxys(1):
            self.connecter(REMOTE_ADDR='172.18.0.5', HTTP_X_FORWARDED_FOR='6.6.6.6, 41.66.1.2')
        self.assertEqual(JournalConnexion.objects.get().adresse_ip, '41.66.1.2')

    def test_adresse_invalide_n_empeche_pas_la_connexion(self):
        with avec_proxys(1):
            reponse = self.connecter(REMOTE_ADDR='172.18.0.5', HTTP_X_FORWARDED_FOR='pas-une-ip')
        self.assertEqual(reponse.status_code, 200)
        self.assertIsNone(JournalConnexion.objects.get().adresse_ip)

    def test_valeurs_longues_ou_nulles_tronquees(self):
        client = APIClient()
        with patch(ENVOI_NOTIFICATION):
            client.post(
                URL_CONNEXION, {'email': 'a' * 400 + '@anitche.ci', 'password': 'x'}, format='json',
                HTTP_USER_AGENT='U' * 600 + '\x00',
            )
        ligne = JournalConnexion.objects.get()
        self.assertEqual(len(ligne.user_agent), 255)
        self.assertEqual(len(ligne.email_tente), 254)
        self.assertNotIn('\x00', ligne.user_agent + ligne.email_tente)


class PanneDuJournalTests(TestCase):
    """Une panne du journal ne change jamais la réponse faite au client."""

    def setUp(self):
        self.client = APIClient()
        creer('awa@anitche.ci')

    def test_connexion_reussie_malgre_la_panne(self):
        with patch.object(JournalConnexion.objects, 'create', side_effect=DatabaseError('panne')), \
                patch(ENVOI_NOTIFICATION) as envoi, \
                self.assertLogs('securite', level='ERROR'):
            reponse = self.client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': MOT_DE_PASSE})
        self.assertEqual(reponse.status_code, 200)
        self.assertIn('access', reponse.json())
        envoi.assert_called_once()

    def test_refus_inchange_malgre_la_panne(self):
        with patch.object(JournalConnexion.objects, 'create', side_effect=DatabaseError('panne')), \
                self.assertLogs('securite', level='ERROR'):
            reponse = self.client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': 'Mauvais'})
        self.assertEqual(reponse.status_code, 401)

    def test_connexion_google_reussie_malgre_la_panne(self):
        infos = {'sub': 'g', 'email': 'awa@anitche.ci', 'email_verified': True}
        with patch(VERIFIER_GOOGLE, return_value=infos), patch(ENVOI_NOTIFICATION), \
                patch.object(JournalConnexion.objects, 'create', side_effect=DatabaseError('panne')), \
                self.assertLogs('securite', level='ERROR'):
            reponse = self.client.post(URL_GOOGLE, {'id_token': 'x'})
        self.assertEqual(reponse.status_code, 200)

    def test_erreur_sql_reelle_n_abime_pas_la_transaction(self):
        # Une ligne refusée par la base (contrainte) annule seulement son point
        # de sauvegarde : la transaction de l'appelant reste utilisable.
        with patch('apps.utilisateurs.services.adresse_ip_client', return_value='pas-une-ip'), \
                self.assertLogs('securite', level='ERROR'):
            reponse = self.client.post(URL_CONNEXION, {'email': 'awa@anitche.ci', 'password': MOT_DE_PASSE})
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(JournalConnexion.objects.count(), 0)
        self.assertEqual(Utilisateur.objects.count(), 1)


class MesConnexionsTests(TestCase):
    """GET connexions/ : mon historique seulement, paginé, récent d'abord."""

    def setUp(self):
        self.client = APIClient()
        self.awa = creer('awa@anitche.ci')
        self.bintou = creer('bintou@anitche.ci')

    def test_anonyme_refuse(self):
        self.assertEqual(self.client.get(URL_MES_CONNEXIONS).status_code, 401)

    def test_seules_mes_connexions_les_recentes_d_abord(self):
        ancienne = ecrire(self.awa, il_y_a=timedelta(days=2))
        refusee = ecrire(self.awa, reussie=False, il_y_a=timedelta(days=1))
        recente = ecrire(self.awa)
        ecrire(self.bintou)
        ecrire(None, 'awa@anitche.ci', reussie=False)

        self.client.force_authenticate(self.awa)
        reponse = self.client.get(URL_MES_CONNEXIONS)

        self.assertEqual(reponse.status_code, 200)
        self.assertEqual([ligne['id'] for ligne in reponse.json()['results']], [recente.id, refusee.id, ancienne.id])
        self.assertEqual(reponse.json()['count'], 3)

    def test_pagination(self):
        for _ in range(25):
            ecrire(self.awa)
        self.client.force_authenticate(self.awa)
        page1 = self.client.get(URL_MES_CONNEXIONS).json()
        page2 = self.client.get(URL_MES_CONNEXIONS, {'page': 2}).json()
        self.assertEqual((page1['count'], len(page1['results']), len(page2['results'])), (25, 20, 5))

    def test_tous_les_roles_voient_les_leurs(self):
        for role in (Role.VENDEUR, Role.LIVREUR, Role.MODERATEUR, Role.SUPPORT, Role.ADMIN):
            utilisateur = creer(f'{role}@anitche.ci', role=role)
            ecrire(utilisateur)
            self.client.force_authenticate(utilisateur)
            with self.subTest(role=role):
                self.assertEqual(self.client.get(URL_MES_CONNEXIONS).json()['count'], 1)


class JournalAdministrationTests(TestCase):
    """GET administration/connexions/ : administration seulement, filtres."""

    def setUp(self):
        self.client = APIClient()
        self.admin = creer('admin@anitche.ci', role=Role.ADMIN)
        self.awa = creer('awa@anitche.ci')
        self.bintou = creer('bintou@anitche.ci')
        self.l1 = ecrire(self.awa, 'awa@anitche.ci', il_y_a=timedelta(days=10))
        self.l2 = ecrire(self.awa, 'awa@anitche.ci', reussie=False, il_y_a=timedelta(days=5))
        self.l3 = ecrire(self.bintou, 'bintou@anitche.ci', il_y_a=timedelta(days=1))
        self.l4 = ecrire(None, 'inconnu@anitche.ci', reussie=False)
        self.client.force_authenticate(self.admin)

    def ids(self, **params):
        reponse = self.client.get(URL_ADMIN_CONNEXIONS, params)
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return [ligne['id'] for ligne in reponse.json()['results']]

    def test_reserve_a_l_administration(self):
        roles = (Role.CLIENT, Role.VENDEUR, Role.LIVREUR, Role.MODERATEUR, Role.SUPPORT)
        for role in roles:
            utilisateur = creer(f'{role}-x@anitche.ci', role=role, is_staff=True)
            self.client.force_authenticate(utilisateur)
            with self.subTest(role=role):
                self.assertEqual(self.client.get(URL_ADMIN_CONNEXIONS).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(URL_ADMIN_CONNEXIONS).status_code, 401)

    def test_super_admin_autorise(self):
        self.client.force_authenticate(creer('super@anitche.ci', role=Role.SUPER_ADMIN))
        self.assertEqual(self.client.get(URL_ADMIN_CONNEXIONS).status_code, 200)

    def test_tout_le_journal_les_recentes_d_abord(self):
        self.assertEqual(self.ids(), [self.l4.id, self.l3.id, self.l2.id, self.l1.id])

    def test_champs_administration(self):
        ligne = self.client.get(URL_ADMIN_CONNEXIONS).json()['results'][0]
        self.assertEqual(
            set(ligne),
            {'id', 'utilisateur', 'email_tente', 'date_creation', 'reussie', 'motif_echec', 'methode',
             'adresse_ip', 'user_agent'},
        )
        self.assertIsNone(ligne['utilisateur'])

    def test_filtre_utilisateur(self):
        self.assertEqual(self.ids(utilisateur=self.awa.id), [self.l2.id, self.l1.id])

    def test_filtre_email_exact_sans_casse(self):
        self.assertEqual(self.ids(email='AWA@anitche.ci'), [self.l2.id, self.l1.id])
        self.assertEqual(self.ids(email='inconnu@anitche.ci'), [self.l4.id])
        self.assertEqual(self.ids(email='awa'), [])

    def test_filtre_reussie(self):
        self.assertEqual(self.ids(reussie='true'), [self.l3.id, self.l1.id])
        self.assertEqual(self.ids(reussie='false'), [self.l4.id, self.l2.id])

    def test_filtre_periode_jours_inclus(self):
        jour = lambda decalage: (timezone.localdate() - timedelta(days=decalage)).isoformat()  # noqa: E731
        self.assertEqual(self.ids(date_debut=jour(5)), [self.l4.id, self.l3.id, self.l2.id])
        self.assertEqual(self.ids(date_fin=jour(5)), [self.l2.id, self.l1.id])
        self.assertEqual(self.ids(date_debut=jour(5), date_fin=jour(5)), [self.l2.id])
        self.assertEqual(self.ids(date_debut=jour(1), date_fin=jour(1)), [self.l3.id])

    def test_filtres_combines(self):
        self.assertEqual(self.ids(utilisateur=self.awa.id, reussie='false'), [self.l2.id])

    def test_valeurs_invalides_refusees(self):
        for params in (
            {'utilisateur': 'abc'}, {'reussie': 'peut-etre'}, {'date_debut': '2026-13-45'},
            {'date_debut': '2026-10-10', 'date_fin': '2026-10-01'},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(URL_ADMIN_CONNEXIONS, params).status_code, 400)

    def test_pagination(self):
        for _ in range(25):
            ecrire(self.awa)
        reponse = self.client.get(URL_ADMIN_CONNEXIONS).json()
        self.assertEqual((reponse['count'], len(reponse['results'])), (29, 20))


class JournalConnexionModeleTests(TestCase):
    def test_un_echec_exige_un_motif(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalConnexion.objects.create(methode='google', reussie=False, motif_echec='')

    def test_une_reussite_n_a_pas_de_motif(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalConnexion.objects.create(methode='google', reussie=True, motif_echec='compte_desactive')

    def test_suppression_du_compte_conserve_la_ligne(self):
        utilisateur = creer('awa@anitche.ci')
        ligne = ecrire(utilisateur, 'awa@anitche.ci')
        utilisateur.delete()
        ligne.refresh_from_db()
        self.assertIsNone(ligne.utilisateur)
        self.assertEqual(ligne.email_tente, 'awa@anitche.ci')


class JournalConnexionAdminDjangoTests(TestCase):
    """L'admin Django montre le journal sans permettre de le modifier."""

    def setUp(self):
        self.superuser = Utilisateur.objects.create_superuser(
            email='root@anitche.ci', password=MOT_DE_PASSE, nom='R', prenom='O',
        )
        self.ligne = ecrire(None, 'awa@anitche.ci')
        self.modele_admin = JournalConnexionAdmin(JournalConnexion, admin.site)
        self.requete = RequestFactory().get('/')
        self.requete.user = self.superuser

    def test_enregistre_dans_l_admin(self):
        self.assertIsInstance(admin.site._registry[JournalConnexion], JournalConnexionAdmin)

    def test_lecture_seule_meme_pour_un_superutilisateur(self):
        self.assertFalse(self.modele_admin.has_add_permission(self.requete))
        self.assertFalse(self.modele_admin.has_change_permission(self.requete, self.ligne))
        self.assertFalse(self.modele_admin.has_delete_permission(self.requete, self.ligne))
        self.assertTrue(self.modele_admin.has_view_permission(self.requete, self.ligne))

    def test_tous_les_champs_en_lecture_seule(self):
        noms = {champ.name for champ in JournalConnexion._meta.fields}
        self.assertEqual(set(self.modele_admin.get_readonly_fields(self.requete, self.ligne)), noms)

    def test_pages_accessibles(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get('/admin/utilisateurs/journalconnexion/').status_code, 200)
        self.assertEqual(self.client.get(f'/admin/utilisateurs/journalconnexion/{self.ligne.pk}/change/').status_code, 200)
        self.assertEqual(self.client.get('/admin/utilisateurs/journalconnexion/add/').status_code, 403)
        self.assertEqual(
            self.client.post(f'/admin/utilisateurs/journalconnexion/{self.ligne.pk}/delete/', {'post': 'yes'}).status_code,
            403,
        )
        self.assertTrue(JournalConnexion.objects.filter(pk=self.ligne.pk).exists())


class PurgeJournalConnexionsTests(TestCase):
    def test_supprime_les_lignes_de_plus_de_la_duree_de_retention(self):
        vieille = ecrire(None, il_y_a=timedelta(days=181))
        limite = ecrire(None, il_y_a=timedelta(days=179))
        recente = ecrire(None)

        resultat = purger_journal_connexions()

        self.assertEqual(set(JournalConnexion.objects.values_list('pk', flat=True)), {limite.pk, recente.pk})
        self.assertFalse(JournalConnexion.objects.filter(pk=vieille.pk).exists())
        self.assertIn('1 ', resultat)

    @override_settings(JOURNAL_CONNEXIONS_RETENTION_JOURS=30)
    def test_la_duree_est_un_reglage(self):
        ecrire(None, il_y_a=timedelta(days=31))
        gardee = ecrire(None, il_y_a=timedelta(days=29))
        purger_journal_connexions()
        self.assertEqual(list(JournalConnexion.objects.values_list('pk', flat=True)), [gardee.pk])

    def test_duree_par_defaut_de_180_jours(self):
        self.assertEqual(settings.JOURNAL_CONNEXIONS_RETENTION_JOURS, 180)

    def test_planifiee_chaque_jour_dans_beat(self):
        entree = settings.CELERY_BEAT_SCHEDULE['utilisateurs-purger-journal-connexions']
        self.assertEqual(entree['task'], purger_journal_connexions.name)
        self.assertEqual(entree['schedule'].hour, {3})
        self.assertEqual(entree['schedule'].day_of_month, set(range(1, 32)))
