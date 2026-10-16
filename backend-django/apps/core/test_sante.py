from unittest import mock

from django.db import OperationalError
from django.test import TestCase
from redis.exceptions import ConnectionError as RedisConnectionError


class SanteTests(TestCase):
    def test_200_quand_base_et_redis_repondent(self):
        with mock.patch('apps.core.sante.redis.Redis.from_url') as from_url:
            reponse = self.client.get('/health/')
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json(), {'status': 'ok'})
        from_url.return_value.ping.assert_called_once()
        from_url.return_value.close.assert_called_once()

    def test_503_quand_la_base_manque(self):
        with mock.patch('apps.core.sante.redis.Redis.from_url'), \
                mock.patch('apps.core.sante.connection.cursor', side_effect=OperationalError('secret: db down')):
            reponse = self.client.get('/health/')
        self.assertEqual(reponse.status_code, 503)
        self.assertEqual(reponse.json(), {'status': 'indisponible'})
        self.assertNotIn('secret', reponse.content.decode())

    def test_503_quand_redis_manque(self):
        with mock.patch('apps.core.sante.redis.Redis.from_url') as from_url:
            from_url.return_value.ping.side_effect = RedisConnectionError('redis://:mdp@redis:6379 refused')
            reponse = self.client.get('/health/')
        self.assertEqual(reponse.status_code, 503)
        self.assertEqual(reponse.json(), {'status': 'indisponible'})
        self.assertNotIn('mdp', reponse.content.decode())
        from_url.return_value.close.assert_called_once()

    def test_sans_authentification_et_sans_mise_en_cache(self):
        with mock.patch('apps.core.sante.redis.Redis.from_url'):
            reponse = self.client.get('/health/')
        self.assertEqual(reponse.status_code, 200)
        self.assertIn('no-cache', reponse['Cache-Control'])

    def test_methode_non_sure_refusee(self):
        reponse = self.client.post('/health/')
        self.assertEqual(reponse.status_code, 405)
