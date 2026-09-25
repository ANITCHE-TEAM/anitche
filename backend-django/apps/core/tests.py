from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.throttling import AnonRateThrottle

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
