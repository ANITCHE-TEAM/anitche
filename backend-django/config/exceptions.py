import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger("anitche.exceptions")


def custom_exception_handler(exc, context):
    """Gestionnaire d'exceptions global pour Django REST Framework.
    
    Standardise le format de réponse en cas d'erreur :
    {
        "success": false,
        "status_code": 400,
        "detail": "Message principal",
        "errors": { ... }
    }
    """
    # Appel du gestionnaire par défaut de DRF
    response = exception_handler(exc, context)

    view_name = context.get("view", None)
    view_name_str = view_name.__class__.__name__ if view_name else "Inconnue"

    if response is not None:
        # Erreur standard DRF (400, 401, 403, 404, 405, 429, etc.)
        custom_data = {
            "success": False,
            "status_code": response.status_code,
            "detail": "Une erreur est survenue lors du traitement de la requête.",
            "errors": response.data,
        }

        # Si response.data contient déjà un champ 'detail' clair
        if isinstance(response.data, dict) and "detail" in response.data:
            custom_data["detail"] = response.data["detail"]
        elif isinstance(response.data, dict):
            # Premier message d'erreur si disponible
            for cle, val in response.data.items():
                if isinstance(val, list) and val:
                    custom_data["detail"] = f"{cle}: {val[0]}"
                    break

        response.data = custom_data
        logger.warning(
            f"Erreur HTTP {response.status_code} dans la vue {view_name_str}: {custom_data['detail']}"
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
                "detail": "Une erreur interne du serveur est survenue. L'incident a été enregistré.",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response
