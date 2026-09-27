"""Exception commune des refus métier exposés par l'API.

Les vues la lèvent au lieu de construire une Response d'erreur à la main :
config/exceptions.py::custom_exception_handler la met alors au format commun
{success, status_code, detail, errors}, comme toutes les autres erreurs.
"""

from rest_framework.exceptions import APIException


class ErreurMetier(APIException):
    """Refus métier sans champ fautif précis (transition impossible, action
    déjà faite, fournisseur indisponible...), avec son code HTTP.

    Une erreur qui porte sur un champ de la requête se lève plutôt avec
    ValidationError({"champ": [message]}), pour que le frontend puisse la
    rattacher au bon champ (errors.<champ>).
    """

    status_code = 400
    default_detail = "La requête ne peut pas être traitée."
    default_code = "erreur_metier"

    def __init__(self, detail=None, status_code=None, code=None):
        super().__init__(detail, code)
        if status_code is not None:
            self.status_code = status_code
