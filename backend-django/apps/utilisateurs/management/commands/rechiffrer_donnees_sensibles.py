"""Re-chiffre toutes les données chiffrées au repos avec la clé active.

Étape de la rotation de clé (docs/MODULE_UTILISATEURS.md, « Gestion de la
clé ») : une fois la nouvelle clé placée EN TÊTE de FIELD_ENCRYPTION_KEYS
(l'ancienne toujours présente derrière), cette commande re-chiffre chaque
valeur avec la nouvelle clé. Ensuite seulement, l'ancienne clé peut être
retirée.

Parcourt tous les champs EncryptedCharField du projet, en lisant et en
écrivant les jetons bruts (SQL direct) : aucune valeur ne passe en clair
par l'ORM. Une valeur qu'aucune clé ne déchiffre est laissée intacte et
comptée : ne jamais retirer une ancienne clé tant que ce compte n'est pas nul.
"""

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.core.fields import EncryptedCharField, chiffreur


class Command(BaseCommand):
    help = "Re-chiffre les champs EncryptedCharField avec la clé active (rotation de clé)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--simulation', action='store_true',
            help="Compte ce qui serait re-chiffré, sans rien écrire.",
        )

    def handle(self, *args, simulation=False, **options):
        fernet = chiffreur()
        total_rechiffres = total_illisibles = 0

        for modele in apps.get_models():
            champs = [champ for champ in modele._meta.concrete_fields if isinstance(champ, EncryptedCharField)]
            if not champs:
                continue
            table = connection.ops.quote_name(modele._meta.db_table)
            cle_primaire = connection.ops.quote_name(modele._meta.pk.column)
            for champ in champs:
                colonne = connection.ops.quote_name(champ.column)
                rechiffres = illisibles = 0
                with transaction.atomic(), connection.cursor() as curseur:
                    curseur.execute(
                        f"SELECT {cle_primaire}, {colonne} FROM {table} "
                        f"WHERE {colonne} IS NOT NULL AND {colonne} <> '' FOR UPDATE"
                    )
                    for identifiant, jeton in curseur.fetchall():
                        try:
                            nouveau_jeton = fernet.rotate(jeton.encode()).decode()
                        except InvalidToken:
                            illisibles += 1
                            continue
                        rechiffres += 1
                        if not simulation:
                            curseur.execute(
                                f"UPDATE {table} SET {colonne} = %s WHERE {cle_primaire} = %s",
                                [nouveau_jeton, identifiant],
                            )
                self.stdout.write(
                    f"{modele._meta.label}.{champ.name} : {rechiffres} re-chiffrée(s), {illisibles} illisible(s)"
                )
                total_rechiffres += rechiffres
                total_illisibles += illisibles

        mode = " (simulation : rien n'a été écrit)" if simulation else ""
        self.stdout.write(f"Total : {total_rechiffres} re-chiffrée(s), {total_illisibles} illisible(s){mode}.")
        if total_illisibles:
            self.stderr.write(
                "ATTENTION : des valeurs ne se déchiffrent avec aucune clé de FIELD_ENCRYPTION_KEYS. "
                "Ne retirez aucune ancienne clé avant d'avoir compris pourquoi."
            )
