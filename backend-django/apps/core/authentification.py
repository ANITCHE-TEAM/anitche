from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from drf_spectacular.drainage import set_override
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class JWTAuthentificationOptionnelle(JWTAuthentication):
    """JWT des routes publiques : un jeton valide identifie l'utilisateur,
    un jeton refusé laisse passer la requête en visiteur anonyme.

    Une page publique ne doit pas répondre 401 parce que le client garde un
    jeton expiré, révoqué (CHECK_REVOKE_TOKEN) ou malformé. La signature est
    toujours vérifiée : un jeton refusé n'authentifie jamais personne, il est
    seulement ignoré. Un jeton valide garde l'identification par compte, donc
    la limite ScopedRateThrottle par utilisateur plutôt que par IP (CGNAT).

    Réservée aux vues dont la réponse ne dépend pas de l'identité. Les routes
    protégées et le panier (dont le contenu dépend du compte) gardent
    JWTAuthentication : leur 401 déclenche le rafraîchissement côté client.
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None


class JWTAuthentificationOptionnelleScheme(SimpleJWTScheme):
    """Même schéma de sécurité OpenAPI (jwtAuth) que JWTAuthentication :
    drf-spectacular n'associe pas une sous-classe à l'extension de SimpleJWT."""

    target_class = 'apps.core.authentification.JWTAuthentificationOptionnelle'


# Les deux classes déclarent volontairement le même composant jwtAuth, avec
# la même définition : sans ce réglage, drf-spectacular signale deux
# composants de même nom (avertissement bloquant avec --fail-on-warn).
set_override(JWTAuthentificationOptionnelle, 'suppress_collision_warning', True)
