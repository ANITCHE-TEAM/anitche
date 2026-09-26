import apps.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Introduit le type de pièce et sépare piece_identite en recto/verso.

    - type_piece : ajouté avec un défaut ponctuel 'cni' (preserve_default
      = False) pour backfiller les dossiers déjà existants sans les
      laisser dans un état invalide ; le modèle ne conserve PAS ce
      default (voir models.py — la valeur doit être fournie
      explicitement par le client à chaque nouvel upload).
    - piece_identite -> piece_identite_recto : renommage (pas de perte
      de données), le fichier déjà soumis devient le recto.
    - piece_identite_verso : nouveau champ, optionnel au niveau modèle
      (la règle métier obligatoire/refusé selon type_piece vit dans
      DocumentKYCSerializer, pas en base).

    Entièrement réversible : RenameField et AddField savent tous deux
    revenir en arrière automatiquement.
    """

    dependencies = [
        ('utilisateurs', '0005_alter_documentkyc_piece_identite_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentkyc',
            name='type_piece',
            field=models.CharField(
                choices=[
                    ('cni', "Carte nationale d'identité"),
                    ('passeport', 'Passeport'),
                    ('permis', 'Permis de conduire'),
                    ('carte_consulaire', 'Carte consulaire'),
                    ('carte_resident', 'Carte de résident'),
                    ('attestation_identite', "Attestation d'identité"),
                ],
                default='cni',
                max_length=30,
            ),
            preserve_default=False,
        ),
        migrations.RenameField(
            model_name='documentkyc',
            old_name='piece_identite',
            new_name='piece_identite_recto',
        ),
        migrations.AddField(
            model_name='documentkyc',
            name='piece_identite_verso',
            field=models.FileField(
                blank=True,
                null=True,
                upload_to='kyc/pieces_identite/',
                validators=[apps.core.validators.ValidateurFichierSecurise(extensions=['.jpg', '.jpeg', '.png', '.pdf'], taille_max_mo=10)],
            ),
        ),
    ]
