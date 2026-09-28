"""Intégration PostgreSQL : rôle anitche_fastapi_ro et requête d'accès.

Schéma créé par les migrations Django, rôle créé par
infra/postgres/fastapi_readonly.sql (rapport module 1 §b et §g).
"""
import asyncio
import itertools
import uuid

import asyncpg
import pytest

from app.services.delivery_access import Refusal, can_listen, fetch_access

pytestmark = pytest.mark.integration

# Exactement la liste du script SQL, ni plus ni moins.
EXPECTED_COLUMN_PRIVILEGES = {
    ("livraison_livraison", "id"),
    ("livraison_livraison", "commande_id"),
    ("livraison_livraison", "livreur_id"),
    ("livraison_livraison", "status"),
    ("commandes_commande", "id"),
    ("commandes_commande", "client_id"),
    ("commandes_commande", "groupe_id"),
    ("commandes_groupecommande", "id"),
    ("commandes_groupecommande", "livraison_latitude"),
    ("commandes_groupecommande", "livraison_longitude"),
    ("utilisateurs_utilisateur", "id"),
    ("utilisateurs_utilisateur", "role"),
    ("utilisateurs_utilisateur", "is_active"),
}
STATUSES = ["en_attente", "expediee", "en_cours", "livree", "echouee", "annulee"]
POINT = (5.397340, -3.986620)


async def _fetch(url: str, query: str, *args):
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def test_column_privileges_are_exactly_the_granted_list(admin_database_url):
    rows = asyncio.run(_fetch(
        admin_database_url,
        """
        SELECT table_name, column_name, privilege_type
        FROM information_schema.column_privileges
        WHERE grantee = 'anitche_fastapi_ro'
        """,
    ))
    assert {row["privilege_type"] for row in rows} == {"SELECT"}
    assert {(row["table_name"], row["column_name"]) for row in rows} == EXPECTED_COLUMN_PRIVILEGES
    tables = asyncio.run(_fetch(
        admin_database_url,
        "SELECT table_name FROM information_schema.table_privileges WHERE grantee = 'anitche_fastapi_ro'",
    ))
    assert tables == []  # aucun droit sur une table entière


@pytest.mark.parametrize(
    "query",
    [
        "SELECT code_hash FROM livraison_livraison",
        "SELECT code_chiffre FROM livraison_livraison",
        "SELECT adresse_livraison FROM livraison_livraison",
        "SELECT * FROM livraison_livraison",
        "SELECT email FROM utilisateurs_utilisateur",
        "SELECT password FROM utilisateurs_utilisateur",
        "SELECT telephone FROM utilisateurs_utilisateur",
        "SELECT montant_total FROM commandes_commande",
        "SELECT livraison_telephone FROM commandes_groupecommande",
        "SELECT livraison_commune FROM commandes_groupecommande",
        "SELECT id FROM vendeurs_boutique",
    ],
)
def test_other_columns_and_tables_are_refused(ro_database_url, query):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        asyncio.run(_fetch(ro_database_url, query))


def test_writes_are_refused_even_in_a_read_write_transaction(ro_database_url):
    async def write():
        connection = await asyncpg.connect(ro_database_url)
        try:
            with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                await connection.execute("UPDATE livraison_livraison SET status = 'livree'")
            # La vraie barrière : même en forçant une transaction en écriture,
            # le rôle n'a aucun droit d'écriture.
            async with connection.transaction(readonly=False):
                await connection.execute("SET TRANSACTION READ WRITE")
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute("UPDATE livraison_livraison SET status = 'livree'")
        finally:
            await connection.close()

    asyncio.run(write())


def test_access_query_on_the_django_schema(seed, ro_database_url):
    """Requête d'accès lue par le rôle en lecture seule, pour les quatre
    profils et chaque statut."""
    deliveries = {
        status: seed.delivery(client="client", courier="livreur", status=status, point=POINT)
        for status in STATUSES
    }
    expected_visible = {"admin": True, "livreur": True, "client": True, "tiers": False, "autre_livreur": False, "vendeur": False}

    async def check():
        pool = await asyncpg.create_pool(
            ro_database_url, min_size=1, max_size=2,
            server_settings={"default_transaction_read_only": "on"},
        )
        try:
            for (status, delivery_id), (name, visible) in itertools.product(deliveries.items(), expected_visible.items()):
                user_id = seed.users[name]
                access = await fetch_access(pool, delivery_id, user_id)
                assert access.status == status
                assert access.courier_id == seed.users["livreur"]
                assert access.client_id == seed.users["client"]
                assert access.destination == pytest.approx(POINT)
                refusal = can_listen(access, user_id)
                if not visible:
                    assert refusal is Refusal.DELIVERY_NOT_FOUND, (name, status)
                elif status != "en_cours":
                    assert refusal is Refusal.NOT_IN_PROGRESS, (name, status)
                else:
                    assert refusal is None, (name, status)
            assert await fetch_access(pool, uuid.uuid4(), seed.users["admin"]) is None
        finally:
            await pool.close()

    asyncio.run(check())


def test_delivery_without_client_point_has_no_destination(seed, ro_database_url):
    delivery_id = seed.delivery(client="client", courier="livreur", status="en_cours", point=None)

    async def check():
        pool = await asyncpg.create_pool(ro_database_url, min_size=1, max_size=1)
        try:
            return await fetch_access(pool, delivery_id, seed.users["client"])
        finally:
            await pool.close()

    access = asyncio.run(check())
    assert access.destination is None and access.courier_id == seed.users["livreur"]
