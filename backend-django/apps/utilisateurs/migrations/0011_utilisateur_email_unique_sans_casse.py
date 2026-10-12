import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    """Unicité de l'email insensible à la casse (index unique sur LOWER(email))."""

    dependencies = [
        ('utilisateurs', '0010_emails_en_minuscules'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='utilisateur',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower('email'),
                name='utilisateur_email_unique_sans_casse',
                violation_error_message='Un compte existe déjà avec cet email.',
            ),
        ),
    ]
