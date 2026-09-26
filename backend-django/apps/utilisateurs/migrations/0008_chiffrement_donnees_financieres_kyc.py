"""Chiffrement au repos de numero_mobile_money et compte_bancaire (DocumentKYC).

Trois étapes, pour ne jamais lire une valeur en clair avec un champ qui
attend du chiffré (elle serait lue comme illisible) :
1. colonnes passées en TEXT, toujours en clair ;
2. valeurs existantes chiffrées directement (Fernet, clé active de
   FIELD_ENCRYPTION_KEYS) — le retour arrière les déchiffre ;
3. passage à EncryptedCharField (changement d'état seul, aucun SQL : la
   colonne est déjà du TEXT).
"""

import apps.core.fields
from django.db import migrations, models

CHAMPS = ('numero_mobile_money', 'compte_bancaire')


def _transformer(apps, operation):
    # Les clés sont une configuration d'exécution : on passe par le même
    # point d'entrée que le champ (apps.core.fields.chiffreur).
    from apps.core.fields import chiffreur

    fernet = chiffreur()
    DocumentKYC = apps.get_model('utilisateurs', 'DocumentKYC')
    for dossier in DocumentKYC.objects.only('pk', *CHAMPS).iterator():
        valeurs = {}
        for champ in CHAMPS:
            valeur = getattr(dossier, champ)
            if valeur:
                valeurs[champ] = operation(fernet, valeur)
        if valeurs:
            DocumentKYC.objects.filter(pk=dossier.pk).update(**valeurs)


def chiffrer(apps, schema_editor):
    _transformer(apps, lambda fernet, valeur: fernet.encrypt(valeur.encode()).decode())


def dechiffrer(apps, schema_editor):
    _transformer(apps, lambda fernet, valeur: fernet.decrypt(valeur.encode()).decode())


class Migration(migrations.Migration):

    dependencies = [
        ('utilisateurs', '0007_kyc_upload_to_uuid'),
    ]

    operations = [
        migrations.AlterField(
            model_name='documentkyc',
            name='numero_mobile_money',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='documentkyc',
            name='compte_bancaire',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(chiffrer, dechiffrer),
        migrations.AlterField(
            model_name='documentkyc',
            name='numero_mobile_money',
            field=apps.core.fields.EncryptedCharField(),
        ),
        migrations.AlterField(
            model_name='documentkyc',
            name='compte_bancaire',
            field=apps.core.fields.EncryptedCharField(blank=True, null=True),
        ),
    ]
