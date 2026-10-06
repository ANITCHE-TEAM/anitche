"""Recherche publique : règles de visibilité du catalogue traduites en vues SQL.

Lues par FastAPI (module recherche) avec le rôle en lecture seule
`anitche_fastapi_ro`, qui n'a le droit de lire QUE ces vues (droits posés
par infra/postgres/fastapi_readonly.sql, lancé après `migrate`). Aucune
table du catalogue ne lui est ouverte : ni stock exact, ni seuil d'alerte,
ni produits désactivés, ni données du propriétaire de la boutique.

Règles Django traduites ici (toute évolution doit être répercutée, le test
de parité de apps/catalogue/tests.py échoue sinon) :
- catalogue_produit_public : Produit.objects.visibles_publiquement(), prix
  affiché et `en_stock` de ProduitPublicListView, image principale de
  ProduitPublicListSerializer ;
- catalogue_categorie_publique : Categorie.objects.actives() ;
- catalogue_boutique_publique : Boutique.objects.publiques().

PostgreSQL refuse de modifier le type d'une colonne utilisée par une vue, ou
de la supprimer : une telle migration doit supprimer puis recréer les vues
(docs/MODULE_CATALOGUE.md, § 12).

Sous SQLite (config.settings.test, lancements locaux rapides), rien n'est
créé : ni vues ni index, et les tests de parité sont ignorés.
"""
from django.db import migrations

CREATION = [
    # « trusted » depuis PostgreSQL 13 : le propriétaire de la base suffit,
    # pas besoin d'être superutilisateur. Schéma explicite : la fonction
    # ci-dessous appelle public.unaccent.
    "CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public",
    # Minuscules, sans accents. IMMUTABLE (dictionnaire et schéma explicites)
    # pour pouvoir être indexée ; la recherche doit toujours normaliser le
    # texte cherché avec cette même fonction, jamais en Python.
    """
    CREATE FUNCTION catalogue_normaliser(texte text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, texte)) $$
    """,
    # Prix et stock calculés dans un seul LATERAL : PostgreSQL ne le calcule
    # que pour les lignes renvoyées quand le tri n'en dépend pas.
    # `categorie_parent_slug` : le filtre Django `categorie__parent__slug`
    # porte aussi sur un parent inactif. Un produit rangé dans une catégorie
    # inactive reste visible, comme dans Django (dette, MODULE_CATALOGUE.md § 9).
    """
    CREATE VIEW catalogue_produit_public AS
    SELECT p.id, p.nom, p.slug, p.prix_base, p.date_creation,
           p.categorie_id, c.nom AS categorie_nom, c.slug AS categorie_slug,
           c.parent_id AS categorie_parent_id, cp.slug AS categorie_parent_slug,
           p.boutique_id, b.nom AS boutique_nom, b.slug AS boutique_slug,
           offre.prix_min, offre.en_stock,
           (SELECT nullif(i.image, '') FROM catalogue_imageproduit i WHERE i.produit_id = p.id
            ORDER BY i.est_principale DESC, i.ordre, i.id LIMIT 1) AS image_principale,
           catalogue_normaliser(p.nom) AS nom_normalise,
           catalogue_normaliser(p.nom || ' ' || p.description) AS texte_normalise
    FROM catalogue_produit p
    JOIN vendeurs_boutique b ON b.id = p.boutique_id
    JOIN utilisateurs_utilisateur u ON u.id = b.proprietaire_id
    LEFT JOIN catalogue_categorie c ON c.id = p.categorie_id
    LEFT JOIN catalogue_categorie cp ON cp.id = c.parent_id
    LEFT JOIN LATERAL (
        SELECT min(coalesce(v.prix_promo, v.prix)) AS prix_min,
               coalesce(bool_or(s.quantite_disponible > 0), false) AS en_stock
        FROM catalogue_varianteproduit v
        LEFT JOIN catalogue_stock s ON s.variante_id = v.id
        WHERE v.produit_id = p.id AND v.est_active
    ) offre ON true
    WHERE p.est_actif
      AND b.est_active AND NOT b.est_suspendue
      AND u.role = 'vendeur' AND u.statut_kyc = 'valide' AND u.is_active
      AND EXISTS (SELECT 1 FROM catalogue_varianteproduit v WHERE v.produit_id = p.id AND v.est_active)
    """,
    """
    CREATE VIEW catalogue_categorie_publique AS
    SELECT id, nom, slug, parent_id, catalogue_normaliser(nom) AS nom_normalise
    FROM catalogue_categorie
    WHERE est_active
    """,
    # `nom_normalise` de la vue = catalogue_normaliser(nom) (recherche), à ne
    # pas confondre avec la colonne vendeurs_boutique.nom_normalise (forme
    # sans espaces ni ponctuation, anti-usurpation), qui n'est pas exposée.
    """
    CREATE VIEW catalogue_boutique_publique AS
    SELECT b.id, b.nom, b.slug, catalogue_normaliser(b.nom) AS nom_normalise
    FROM vendeurs_boutique b
    JOIN utilisateurs_utilisateur u ON u.id = b.proprietaire_id
    WHERE b.est_active AND NOT b.est_suspendue
      AND u.role = 'vendeur' AND u.statut_kyc = 'valide' AND u.is_active
    """,
    # Mêmes expressions que les colonnes de la vue, sinon PostgreSQL
    # n'utilise pas ces index. CREATE INDEX simple : la table est petite ;
    # CONCURRENTLY (migration non atomique) le jour où elle sera grosse.
    """
    CREATE INDEX catalogue_produit_nom_trgm ON catalogue_produit
    USING gin (catalogue_normaliser(nom) public.gin_trgm_ops)
    """,
    """
    CREATE INDEX catalogue_produit_texte_trgm ON catalogue_produit
    USING gin (catalogue_normaliser(nom || ' ' || description) public.gin_trgm_ops)
    """,
    # Tri par défaut « plus récents d'abord », id en second pour une
    # pagination stable.
    "CREATE INDEX catalogue_produit_date_id ON catalogue_produit (date_creation DESC, id DESC)",
]

# Les extensions restent : elles peuvent servir ailleurs. Supprimer les vues
# retire aussi les droits de FastAPI sur elles : relancer le script de droits
# après un nouveau `migrate`.
SUPPRESSION = [
    "DROP INDEX catalogue_produit_date_id",
    "DROP INDEX catalogue_produit_texte_trgm",
    "DROP INDEX catalogue_produit_nom_trgm",
    "DROP VIEW catalogue_boutique_publique",
    "DROP VIEW catalogue_categorie_publique",
    "DROP VIEW catalogue_produit_public",
    "DROP FUNCTION catalogue_normaliser(text)",
]


def executer(instructions):
    def operation(apps, schema_editor):
        if schema_editor.connection.vendor != 'postgresql':
            return
        for instruction in instructions:
            schema_editor.execute(instruction)
    return operation


class Migration(migrations.Migration):

    dependencies = [
        ('catalogue', '0003_desactivation_et_regles_de_prix'),
        ('vendeurs', '0005_livraison_offerte'),
        ('utilisateurs', '0009_retrait_compte_bancaire'),
    ]

    operations = [
        migrations.RunPython(executer(CREATION), executer(SUPPRESSION)),
    ]
