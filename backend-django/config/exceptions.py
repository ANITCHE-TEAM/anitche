import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from rest_framework.settings import api_settings

logger = logging.getLogger("anitche.exceptions")

MESSAGE_ERREUR_GENERIQUE = "Une erreur est survenue lors du traitement de la requête."
MESSAGE_ERREUR_INTERNE = "Une erreur interne du serveur est survenue. L'incident a été enregistré."


def _aplatir(valeur, prefixe, erreurs):
    """Remplit `erreurs` (clé → liste de messages) à partir des erreurs DRF.

    Les erreurs imbriquées (serializer dans un serializer, liste d'objets)
    prennent une clé à points : "adresse_livraison.commune", "items.0.quantite".
    """
    if isinstance(valeur, dict):
        for cle, sous_valeur in valeur.items():
            _aplatir(sous_valeur, f"{prefixe}.{cle}" if prefixe else str(cle), erreurs)
    elif isinstance(valeur, (list, tuple)):
        messages = [str(element) for element in valeur if not isinstance(element, (dict, list, tuple))]
        if messages:
            erreurs.setdefault(prefixe, []).extend(messages)
        for index, element in enumerate(valeur):
            if isinstance(element, (dict, list, tuple)):
                _aplatir(element, f"{prefixe}.{index}", erreurs)
    elif valeur is not None:
        erreurs.setdefault(prefixe, []).append(str(valeur))


def normaliser_erreurs(donnees):
    """(detail, errors) du format commun à partir de `response.data` de DRF.

    - detail : toujours une chaîne (le message principal) ;
    - errors : toujours un objet, clé → liste de messages. Vide quand
      l'erreur ne porte sur aucun champ (401, 404, refus métier...). Une
      erreur de validation sans champ va sous "non_field_errors".
    """
    cle_generale = api_settings.NON_FIELD_ERRORS_KEY
    detail = None
    if isinstance(donnees, dict):
        donnees = dict(donnees)
        if "detail" in donnees:
            valeur = donnees.pop("detail")
            if isinstance(valeur, (list, tuple)):
                valeur = valeur[0] if valeur else None
            detail = str(valeur) if valeur is not None else None
    elif isinstance(donnees, (list, tuple)):
        donnees = {cle_generale: donnees}
    else:
        detail = str(donnees) if donnees is not None else None
        donnees = {}

    erreurs = {}
    _aplatir(donnees, "", erreurs)

    if detail is None:
        detail = MESSAGE_ERREUR_GENERIQUE
        for cle, messages in erreurs.items():
            if messages:
                detail = messages[0] if cle == cle_generale else f"{cle}: {messages[0]}"
                break
    return detail, erreurs


def custom_exception_handler(exc, context):
    """Gestionnaire d'exceptions global pour Django REST Framework.

    Toute réponse d'erreur de l'API a le même format :
    {
        "success": false,
        "status_code": 400,
        "detail": "Message principal",
        "errors": {"champ": ["message", ...]}
    }
    Les vues ne construisent jamais une Response d'erreur à la main : elles
    lèvent une exception DRF (ValidationError, PermissionDenied, NotFound...)
    ou apps.core.exceptions.ErreurMetier. Seules exceptions : les
    notifications des fournisseurs de paiement (apps.paiements.views),
    qui répondent au format attendu par le fournisseur.
    """
    # Appel du gestionnaire par défaut de DRF
    response = exception_handler(exc, context)

    view_name = context.get("view", None)
    view_name_str = view_name.__class__.__name__ if view_name else "Inconnue"

    if response is not None:
        # Erreur standard DRF (400, 401, 403, 404, 405, 409, 429, etc.)
        detail, erreurs = normaliser_erreurs(response.data)
        response.data = {
            "success": False,
            "status_code": response.status_code,
            "detail": detail,
            "errors": erreurs,
        }
        logger.warning(
            f"Erreur HTTP {response.status_code} dans la vue {view_name_str}: {detail}"
        )
    else:
        # Exception 500 non gérée
        logger.error(
            f"Exception non interceptée dans la vue {view_name_str}: {str(exc)}",
            exc_info=True,
        )

        response = Response(
            {
                "success": False,
                "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "detail": MESSAGE_ERREUR_INTERNE,
                "errors": {},
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response
