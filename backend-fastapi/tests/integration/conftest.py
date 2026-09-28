"""Fixtures des tests d'intégration : vrais PostgreSQL et Redis.

Variables (absentes : tests sautés ; REQUIRE_INTEGRATION=1 : échec) :
- FASTAPI_TEST_DATABASE_URL : rôle anitche_fastapi_ro (lecture seule) ;
- FASTAPI_TEST_ADMIN_DATABASE_URL : compte administrateur de la MÊME base,
  pour insérer et supprimer les données de test ;
- FASTAPI_TEST_REDIS_URL : base Redis 15 uniquement.

Schéma : celui des migrations Django (jamais une copie). Rôle : le vrai
script infra/postgres/fastapi_readonly.sql. Jamais la base de dev
« anitche » : refusé ici. Django reste simulé (vérification du jeton).

Limite voulue : les insertions listent toutes les colonnes NOT NULL des
tables Django. Une nouvelle colonne obligatoire casse ces tests : le
contrat entre les deux backends a bougé.
"""
import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

from app.core import resources as resources_module
from app.main import create_app
from tests.fakes import FakeDjango, make_settings

FORBIDDEN_DATABASES = {"anitche"}


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} absente : test d'intégration sauté")
    return value


def _database_url(name: str) -> str:
    url = _required_env(name)
    database = urlsplit(url).path.lstrip("/")
    if database in FORBIDDEN_DATABASES or not database:
        pytest.fail(f"{name} pointe vers « {database} » : jamais la base de dev (utiliser anitche_fastapi_test)")
    return url


@pytest.fixture
def ro_database_url() -> str:
    return _database_url("FASTAPI_TEST_DATABASE_URL")


@pytest.fixture
def admin_database_url(ro_database_url) -> str:
    url = _database_url("FASTAPI_TEST_ADMIN_DATABASE_URL")
    if urlsplit(url).path != urlsplit(ro_database_url).path:
        pytest.fail("FASTAPI_TEST_ADMIN_DATABASE_URL et FASTAPI_TEST_DATABASE_URL doivent viser la même base")
    return url


@pytest.fixture
def redis_url() -> str:
    url = _required_env("FASTAPI_TEST_REDIS_URL")
    if urlsplit(url).path != "/15":
        pytest.fail("FASTAPI_TEST_REDIS_URL doit viser la base Redis 15 (jamais la base 2 de FastAPI)")
    return url


def run(coroutine):
    return asyncio.run(coroutine)


async def _delete_tracking_keys(url: str) -> None:
    # Clés du suivi et des limites, base 15 seulement ; jamais de FLUSHDB.
    redis = Redis.from_url(url, decode_responses=True)
    try:
        keys = [key async for key in redis.scan_iter(match="fastapi:*")]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


@pytest.fixture
def clean_redis(redis_url):
    run(_delete_tracking_keys(redis_url))
    yield redis_url
    run(_delete_tracking_keys(redis_url))


# ------------------------------------------------------------ données


@dataclass
class Seed:
    """Comptes, boutique, groupe, commande et livraison insérés par le
    compte administrateur, supprimés après le test."""

    admin_url: str
    users: dict[str, int] = field(default_factory=dict)
    shop_id: int | None = None
    group_ids: list[uuid.UUID] = field(default_factory=list)
    order_ids: list[uuid.UUID] = field(default_factory=list)
    delivery_ids: list[uuid.UUID] = field(default_factory=list)

    async def _execute(self, query: str, *args):
        connection = await asyncpg.connect(self.admin_url)
        try:
            return await connection.fetchval(query, *args)
        finally:
            await connection.close()

    def user(self, name: str, role: str, *, is_active: bool = True) -> int:
        user_id = run(self._execute(
            """
            INSERT INTO utilisateurs_utilisateur
                (password, is_superuser, email, nom, prenom, role, statut_kyc,
                 email_verifie, telephone_verifie, is_active, is_staff,
                 date_creation, date_mise_a_jour)
            VALUES ('!', false, $1, 'Integration', $2, $3, 'non_soumis',
                    true, false, $4, false, now(), now())
            RETURNING id
            """,
            f"it-{uuid.uuid4().hex}@integration.test", name, role, is_active,
        ))
        self.users[name] = user_id
        return user_id

    def shop(self, owner: str) -> int:
        suffix = uuid.uuid4().hex
        self.shop_id = run(self._execute(
            """
            INSERT INTO vendeurs_boutique
                (nom, slug, description, telephone_contact, email_contact, adresse,
                 ville, est_active, date_creation, date_mise_a_jour, proprietaire_id,
                 est_suspendue, nom_normalise, livraison_offerte)
            VALUES ($1, $1, '', '', '', '', 'Abidjan', true, now(), now(), $2,
                    false, $1, false)
            RETURNING id
            """,
            f"it-{suffix}", self.users[owner],
        ))
        return self.shop_id

    def delivery(self, *, client: str, courier: str | None, status: str, point: tuple | None) -> uuid.UUID:
        group_id, order_id, delivery_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        run(self._insert_delivery(group_id, order_id, delivery_id, client, courier, status, point))
        self.group_ids.append(group_id)
        self.order_ids.append(order_id)
        self.delivery_ids.append(delivery_id)
        return delivery_id

    async def _insert_delivery(self, group_id, order_id, delivery_id, client, courier, status, point):
        latitude, longitude = point or (None, None)
        connection = await asyncpg.connect(self.admin_url)
        try:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO commandes_groupecommande
                        (id, created_at, client_id, livraison_commune, livraison_point_de_repere,
                         livraison_quartier, livraison_telephone, livraison_zone,
                         livraison_latitude, livraison_longitude)
                    VALUES ($1, now(), $2, 'Cocody', 'Test', 'Angré', '+2250700000000', 'abidjan',
                            $3, $4)
                    """,
                    group_id, self.users[client], latitude, longitude,
                )
                await connection.execute(
                    """
                    INSERT INTO commandes_commande
                        (id, numero_commande, status, montant_total, created_at, update_at,
                         boutique_id, client_id, coupon_code, montant_remise, motif_annulation,
                         frais_livraison, frais_livraison_vendeur, livraison_offerte, groupe_id)
                    VALUES ($1, $2, 'confirmee', 1000, now(), now(), $3, $4, '', 0, '',
                            0, 0, false, $5)
                    """,
                    order_id, f"IT-{order_id.hex[:12].upper()}", self.shop_id, self.users[client], group_id,
                )
                await connection.execute(
                    """
                    INSERT INTO livraison_livraison
                        (id, status, adresse_livraison, created_at, updated_at, commande_id,
                         code_chiffre, code_essais, code_hash, tentatives, livreur_id)
                    VALUES ($1, $2, 'Adresse de test', now(), now(), $3, '', 0, '', 0, $4)
                    """,
                    delivery_id, status, order_id, self.users[courier] if courier else None,
                )
        finally:
            await connection.close()

    def set_status(self, delivery_id: uuid.UUID, status: str) -> None:
        run(self._execute("UPDATE livraison_livraison SET status = $2 WHERE id = $1", delivery_id, status))

    async def _cleanup(self):
        connection = await asyncpg.connect(self.admin_url)
        try:
            async with connection.transaction():
                await connection.execute("DELETE FROM livraison_livraison WHERE id = ANY($1::uuid[])", self.delivery_ids)
                await connection.execute("DELETE FROM commandes_commande WHERE id = ANY($1::uuid[])", self.order_ids)
                await connection.execute("DELETE FROM commandes_groupecommande WHERE id = ANY($1::uuid[])", self.group_ids)
                if self.shop_id is not None:
                    await connection.execute("DELETE FROM vendeurs_boutique WHERE id = $1", self.shop_id)
                await connection.execute(
                    "DELETE FROM utilisateurs_utilisateur WHERE id = ANY($1::bigint[])", list(self.users.values())
                )
        finally:
            await connection.close()


@pytest.fixture
def seed(admin_database_url):
    data = Seed(admin_database_url)
    for name, role in [
        ("admin", "admin"),
        ("livreur", "livreur"),
        ("autre_livreur", "livreur"),
        ("client", "client"),
        ("tiers", "client"),
        ("vendeur", "vendeur"),
    ]:
        data.user(name, role)
    data.shop("vendeur")
    yield data
    run(data._cleanup())


# ------------------------------------------------------------ applications


@pytest.fixture
def django_users(seed) -> FakeDjango:
    """Django simulé : un jeton par compte inséré (jeton.<nom>.it)."""
    django = FakeDjango()
    for name, user_id in seed.users.items():
        role = {"admin": "admin", "livreur": "livreur", "autre_livreur": "livreur", "vendeur": "vendeur"}.get(name, "client")
        django.users_by_token[f"jeton.{name}.it"] = {"id": user_id, "role": role}
    return django


def _app_factory(monkeypatch, django: FakeDjango, database_url: str, redis_url: str):
    """Applications FastAPI avec les VRAIES ressources (pool asyncpg du rôle
    en lecture seule, client Redis), ouvertes par le lifespan dans la boucle
    de chaque application ; seul Django est simulé."""
    real_open_resources = resources_module.open_resources

    async def open_with_fake_django(settings):
        opened = await real_open_resources(settings)
        await opened.http.aclose()
        opened.http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(django))
        return opened

    monkeypatch.setattr(resources_module, "open_resources", open_with_fake_django)

    def factory(**overrides):
        return create_app(make_settings(database_url=database_url, redis_url=redis_url, **overrides))

    return factory


@pytest.fixture
def make_app(monkeypatch, django_users, ro_database_url, clean_redis):
    return _app_factory(monkeypatch, django_users, ro_database_url, clean_redis)


@pytest.fixture
def make_search_app(monkeypatch, ro_database_url, clean_redis):
    """Recherche (routes publiques) : aucun compte nécessaire."""
    return _app_factory(monkeypatch, FakeDjango(), ro_database_url, clean_redis)


# ------------------------------------------------------------ catalogue


@dataclass
class CatalogueSeed:
    """Catalogue inséré par le compte administrateur dans les VRAIES tables
    Django, lu par FastAPI à travers les vues de la migration catalogue
    0004 ; supprimé après le test. `marker` : mot unique à mettre dans les
    noms pour isoler une recherche des autres données de la base."""

    admin_url: str
    marker: str = field(default_factory=lambda: "zq" + uuid.uuid4().hex[:10])
    user_ids: list[int] = field(default_factory=list)
    shop_ids: list[int] = field(default_factory=list)
    category_ids: list[int] = field(default_factory=list)
    product_ids: list[int] = field(default_factory=list)

    async def _fetchval(self, query: str, *args):
        connection = await asyncpg.connect(self.admin_url)
        try:
            return await connection.fetchval(query, *args)
        finally:
            await connection.close()

    def execute(self, query: str, *args):
        return run(self._fetchval(query, *args))

    def vendor(self, *, role: str = "vendeur", kyc: str = "valide", active: bool = True) -> int:
        user_id = self.execute(
            """
            INSERT INTO utilisateurs_utilisateur
                (password, is_superuser, email, nom, prenom, role, statut_kyc,
                 email_verifie, telephone_verifie, is_active, is_staff,
                 date_creation, date_mise_a_jour)
            VALUES ('!', false, $1, 'Integration', 'Vendeur', $2, $3, true, false, $4, false, now(), now())
            RETURNING id
            """,
            f"it-{uuid.uuid4().hex}@integration.test", role, kyc, active,
        )
        self.user_ids.append(user_id)
        return user_id

    def shop(self, name: str, *, owner: int | None = None, active: bool = True, suspended: bool = False) -> dict:
        suffix = uuid.uuid4().hex
        owner = owner if owner is not None else self.vendor()
        slug = f"it-{suffix}"
        shop_id = self.execute(
            """
            INSERT INTO vendeurs_boutique
                (nom, slug, description, telephone_contact, email_contact, adresse, ville,
                 est_active, date_creation, date_mise_a_jour, proprietaire_id,
                 est_suspendue, nom_normalise, livraison_offerte)
            VALUES ($1, $2, '', '', '', '', 'Abidjan', $3, now(), now(), $4, $5, $2, false)
            RETURNING id
            """,
            name, slug, active, owner, suspended,
        )
        self.shop_ids.append(shop_id)
        return {"id": shop_id, "slug": slug, "nom": name}

    def category(self, name: str, *, parent: dict | None = None, active: bool = True) -> dict:
        slug = f"it-{uuid.uuid4().hex}"
        category_id = self.execute(
            """
            INSERT INTO catalogue_categorie
                (nom, slug, description, est_active, ordre, date_creation, date_mise_a_jour, parent_id)
            VALUES ($1, $2, '', $3, 0, now(), now(), $4)
            RETURNING id
            """,
            name, slug, active, parent["id"] if parent else None,
        )
        self.category_ids.append(category_id)
        return {"id": category_id, "slug": slug, "nom": name}

    def product(
        self,
        name: str,
        *,
        shop: dict,
        description: str = "",
        category: dict | None = None,
        active: bool = True,
        base_price: int = 10000,
        variants=((10000, None, True, 5),),
        images=(),
        created: datetime | None = None,
    ) -> int:
        """`variants` : (prix, prix promo, active, stock ou None sans ligne de
        stock) ; `images` : (chemin, principale, ordre)."""
        product_id = run(self._insert_product(
            name, shop, description, category, active, base_price, variants, images, created,
        ))
        self.product_ids.append(product_id)
        return product_id

    async def _insert_product(self, name, shop, description, category, active, base_price, variants, images, created):
        connection = await asyncpg.connect(self.admin_url)
        try:
            async with connection.transaction():
                product_id = await connection.fetchval(
                    """
                    INSERT INTO catalogue_produit
                        (nom, slug, description, prix_base, est_actif, date_creation, date_mise_a_jour,
                         boutique_id, categorie_id, desactive_par)
                    VALUES ($1, $2, $3, $4, $5, coalesce($6, now()), now(), $7, $8, $9)
                    RETURNING id
                    """,
                    name, f"it-{uuid.uuid4().hex}", description, Decimal(base_price), active, created,
                    shop["id"], category["id"] if category else None, "" if active else "vendeur",
                )
                for price, promo, variant_active, stock in variants:
                    variant_id = await connection.fetchval(
                        """
                        INSERT INTO catalogue_varianteproduit
                            (sku, nom, prix, prix_promo, est_active, date_creation, date_mise_a_jour, produit_id)
                        VALUES ($1, 'Standard', $2, $3, $4, now(), now(), $5)
                        RETURNING id
                        """,
                        f"IT-{uuid.uuid4().hex}", Decimal(price), None if promo is None else Decimal(promo),
                        variant_active, product_id,
                    )
                    if stock is not None:
                        await connection.execute(
                            """
                            INSERT INTO catalogue_stock (quantite_disponible, seuil_alerte, date_mise_a_jour, variante_id)
                            VALUES ($1, 3, now(), $2)
                            """,
                            stock, variant_id,
                        )
                for path, principal, order in images:
                    await connection.execute(
                        """
                        INSERT INTO catalogue_imageproduit (image, est_principale, ordre, date_creation, produit_id)
                        VALUES ($1, $2, $3, now(), $4)
                        """,
                        path, principal, order, product_id,
                    )
                return product_id
        finally:
            await connection.close()

    async def _cleanup(self):
        connection = await asyncpg.connect(self.admin_url)
        try:
            async with connection.transaction():
                products = self.product_ids
                await connection.execute("DELETE FROM catalogue_imageproduit WHERE produit_id = ANY($1::bigint[])", products)
                await connection.execute(
                    "DELETE FROM catalogue_stock WHERE variante_id IN "
                    "(SELECT id FROM catalogue_varianteproduit WHERE produit_id = ANY($1::bigint[]))",
                    products,
                )
                await connection.execute("DELETE FROM catalogue_varianteproduit WHERE produit_id = ANY($1::bigint[])", products)
                await connection.execute("DELETE FROM catalogue_produit WHERE id = ANY($1::bigint[])", products)
                # Sous-catégories d'abord (clé étrangère vers le parent).
                await connection.execute(
                    "DELETE FROM catalogue_categorie WHERE id = ANY($1::bigint[]) AND parent_id IS NOT NULL", self.category_ids
                )
                await connection.execute("DELETE FROM catalogue_categorie WHERE id = ANY($1::bigint[])", self.category_ids)
                await connection.execute("DELETE FROM vendeurs_boutique WHERE id = ANY($1::bigint[])", self.shop_ids)
                await connection.execute("DELETE FROM utilisateurs_utilisateur WHERE id = ANY($1::bigint[])", self.user_ids)
        finally:
            await connection.close()


@pytest.fixture
def catalogue(admin_database_url):
    data = CatalogueSeed(admin_database_url)
    yield data
    run(data._cleanup())
