"""
Génération de chemins d'upload anonymisés (UUID) pour les fichiers
sensibles (pièces d'identité, selfies KYC).

Le nom de fichier d'origine n'est jamais conservé : il peut contenir des
informations sensibles (nom complet, numéro de document dans le nom du
fichier) et, réutilisé tel quel, peut provoquer une collision ou un nom
de fichier ambigu sur le stockage. Seule l'extension est conservée, pour
que le fichier reste ouvrable normalement.
"""

import os
import uuid

from django.utils.deconstruct import deconstructible


@deconstructible
class CheminUploadUUID:
    """
    Callable `upload_to` : place le fichier dans `sous_dossier` sous un
    nom `<uuid4>.<extension>`.

    Classe (et non simple fonction) décorée `@deconstructible` : Django
    doit pouvoir sérialiser cette valeur dans les migrations (un
    `functools.partial` ne le serait pas), et une instance paramétrée
    par sous-dossier évite de dupliquer une fonction quasi identique par
    champ (recto/verso/selfie).
    """

    def __init__(self, sous_dossier):
        self.sous_dossier = sous_dossier

    def __call__(self, instance, nom_fichier):
        extension = os.path.splitext(nom_fichier)[1].lower()
        return f"{self.sous_dossier}/{uuid.uuid4()}{extension}"

    def __eq__(self, autre):
        return (
            isinstance(autre, CheminUploadUUID)
            and self.sous_dossier == autre.sous_dossier
        )
