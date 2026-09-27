"""Schéma OpenAPI de l'API (drf-spectacular) : tags par module et réponses
d'erreur au format commun (config/exceptions.py).

Les réponses d'erreur courantes sont ajoutées automatiquement à chaque
opération, d'après la vue :

- 400 : méthode avec corps (POST, PUT, PATCH) ;
- 401 : authentification exigée (toute permission autre que AllowAny) ;
- 403 : permission au-delà de « être connecté » (rôle, propriétaire, email
  vérifié...) ;
- 404 : URL avec un paramètre (objet introuvable ou non visible) ;
- 429 : vue soumise à une limite de débit.

Les autres codes (400 d'un GET, 403 d'un contrôle fait dans la vue, 409,
502, 503) se déclarent vue par vue avec `extend_schema(responses=...)` et
le helper `erreurs(...)`.
"""

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated


class ErreurSerializer(serializers.Serializer):
    """Format commun de toute réponse d'erreur de l'API."""

    success = serializers.BooleanField(help_text="Toujours false.")
    status_code = serializers.IntegerField(help_text="Code HTTP, répété dans le corps.")
    detail = serializers.CharField(help_text="Message principal, affichable tel quel.")
    errors = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text=(
            "Erreurs par champ : clé → liste de messages. Vide si l'erreur ne porte sur aucun champ. "
            "Clés imbriquées à points (« adresse_livraison.commune », « items.0.quantite ») ; "
            "« non_field_errors » pour une erreur de validation sans champ ; "
            "« code » porte parfois un code machine (ex. [\"email_non_verifie\"])."
        ),
    )

    class Meta:
        ref_name = "Erreur"


class MessageSerializer(serializers.Serializer):
    """Réponse de succès réduite à un message affichable."""

    message = serializers.CharField()

    class Meta:
        ref_name = "Message"


class JetonsSerializer(serializers.Serializer):
    """Paire de jetons JWT."""

    access = serializers.CharField(help_text="Jeton d'accès (15 minutes), à envoyer en « Authorization: Bearer ».")
    refresh = serializers.CharField(help_text="Jeton de rafraîchissement (7 jours, à usage unique).")

    class Meta:
        ref_name = "Jetons"


#: Réponse d'un téléchargement de fichier (pièce KYC, photo, pièce jointe).
FICHIER = OpenApiResponse(OpenApiTypes.BINARY, description="Contenu du fichier (Content-Disposition : attachment).")


DESCRIPTIONS_ERREURS = {
    400: "Requête invalide (erreurs par champ dans `errors`) ou refus métier (message dans `detail`).",
    401: "Authentification absente, jeton invalide ou expiré.",
    403: "Action non autorisée pour ce compte.",
    404: "Ressource introuvable ou non visible pour ce compte.",
    409: "Conflit : l'état actuel de la ressource ne permet pas cette action.",
    429: "Limite de débit atteinte : réessayer après le délai indiqué par l'en-tête Retry-After.",
    502: "Fournisseur externe (paiement) momentanément indisponible.",
    503: "Service momentanément indisponible.",
}


def erreur(code, description=None):
    return OpenApiResponse(ErreurSerializer, description=description or DESCRIPTIONS_ERREURS[code])


def erreurs(*codes):
    """{code: réponse d'erreur} à fusionner dans extend_schema(responses=...)."""
    return {code: erreur(code) for code in codes}


# Préfixe d'URL (/api/<préfixe>/) → tag du module.
TAGS_PAR_PREFIXE = {
    "utilisateurs": "Utilisateurs",
    "vendeurs": "Vendeurs",
    "catalogue": "Catalogue",
    "panier": "Panier",
    "commandes": "Commandes",
    "paiements": "Paiements",
    "livraison": "Livraison",
    "retours": "Retours",
    "fidelite": "Fidélité",
    "notifications": "Notifications",
    "support": "Support",
    "passeports": "Passeports QR",
}

METHODES_AVEC_CORPS = {"POST", "PUT", "PATCH"}


class SchemaAnitche(AutoSchema):
    def get_tags(self):
        segments = self.path.strip("/").split("/")
        if len(segments) >= 2 and segments[0] == "api" and segments[1] in TAGS_PAR_PREFIXE:
            return [TAGS_PAR_PREFIXE[segments[1]]]
        return super().get_tags()

    def _codes_erreur_automatiques(self):
        permissions = self.view.get_permissions()
        publique = all(isinstance(permission, AllowAny) for permission in permissions)
        codes = []
        if self.method in METHODES_AVEC_CORPS:
            codes.append(400)
        if not publique:
            codes.append(401)
        if any(not isinstance(permission, (AllowAny, IsAuthenticated)) for permission in permissions):
            codes.append(403)
        if "{" in self.path:
            codes.append(404)
        if self.view.get_throttles():
            codes.append(429)
        return codes

    def _get_response_bodies(self, direction="response"):
        reponses = super()._get_response_bodies(direction)
        if direction != "response":
            return reponses
        for code in self._codes_erreur_automatiques():
            reponses.setdefault(str(code), self._get_response_for_code(erreur(code), str(code), direction=direction))
        return dict(sorted(reponses.items()))
