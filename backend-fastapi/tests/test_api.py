from decimal import Decimal

from app.core.auth import CurrentUser, get_current_user
from tests.fakes import AUTH_HEADERS, product_row

# Aucune application globale : chaque test reçoit la fixture `client`
# (tests/conftest.py), application neuve créée par create_app, avec
# Redis, Django et PostgreSQL simulés.


def test_health_check(client):
    # /health vérifie PostgreSQL et Redis, sans exposer de version. Les
    # en-têtes de sécurité (nosniff, X-Frame-Options) sont posés par nginx,
    # et la durée des requêtes va dans le journal d'accès, pas dans un
    # en-tête (tests/test_core_*.py).
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
    assert "version" not in data


def test_root(client):
    # L'en-tête nosniff n'est pas posé par FastAPI (nginx s'en charge) :
    # il n'est pas vérifié ici.
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "documentation" in data
    assert "endpoints" in data



# ==========================================
# 1. Tests Moteur de Recherche
# ==========================================

# La recherche lit les vues publiques du catalogue (PostgreSQL) ; ici, le
# pool simulé renvoie les lignes de la vue (tests/fakes.py). Contrat :
# paramètres et réponse de la liste Django (count/next/previous/results) plus les
# facettes. Comportement réel (accents, fautes, visibilité) :
# tests/integration/test_recherche_vues.py ; détail : tests/test_recherche.py.

def test_recherche_produits_catalogue_complet(client, db):
    db.search_rows = [product_row(), product_row(id=42, nom="Savon noir", slug="savon-noir-fbf0ea")]
    response = client.get("/recherche/produits")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert [p["id"] for p in data["results"]] == [41, 42]
    assert data["next"] is None and data["previous"] is None
    assert set(data["facettes"]) == {"categories", "boutiques", "prix"}


def test_recherche_produits_filtre_mot_cle(client, db):
    db.search_rows = [product_row(id=1, nom="Robe Baoulé Traditionnelle")]
    response = client.get("/recherche/produits?recherche=baoule")
    assert response.status_code == 200
    assert [p["nom"] for p in response.json()["results"]] == ["Robe Baoulé Traditionnelle"]
    # Texte transmis en paramètre, normalisé par PostgreSQL (accents).
    (sql, params), = db.search_calls("page")
    assert params[0] == "baoule" and "baoule" not in sql
    assert "catalogue_normaliser($1::text)" in sql


def test_recherche_produits_filtre_prix_et_tri(client, db):
    response = client.get("/recherche/produits?prix_max=20000&tri=prix_asc")
    assert response.status_code == 200
    (sql, params), = db.search_calls("page")
    assert "p.prix_min <= $1" in sql
    assert "ORDER BY p.prix_min ASC, p.date_creation DESC, p.id DESC" in sql
    assert params == (20000, 0)


def test_recherche_suggestions_autocompletion(client, db):
    db.suggestion_rows = [{"type": "produit", "texte": "Chemise en wax", "id": 32, "slug": "chemise-en-wax-a9808e"}]
    response = client.get("/recherche/suggestions?recherche=wax")
    assert response.status_code == 200
    data = response.json()
    assert data["requete"] == "wax"
    assert data["suggestions"] == [{"type": "produit", "texte": "Chemise en wax", "id": 32, "slug": "chemise-en-wax-a9808e"}]


# ==========================================
# 2. Tests Conseiller Shopping IA
# ==========================================

# Routes authentifiées, produits du vrai catalogue (lus par la recherche ;
# ici le pool simulé). Contrat complet : tests/test_conseiller_ia.py.

def test_ia_conseil_mariage_ceremonie(client, db):
    db.search_rows = [
        product_row(id=33, nom="Robe en bazin brodé", categorie_nom="Mode", prix_min=Decimal("22000.00")),
        product_row(id=41),
    ]
    payload = {
        "messages": [
            {"role": "user", "contenu": "Je cherche une tenue d'apparat pour un mariage traditionnel à Yamoussoukro."}
        ],
        "occasion": "mariage traditionnel",
        "budget_max": 50000,
    }
    response = client.post("/ia/conseil", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert [p["id"] for p in data["produits_suggeres"]] == [33]  # le karité n'est pas proposé
    assert len(data["conseils_style"]) > 0
    assert "bazin" in data["produits_suggeres"][0]["justification"].lower()
    assert data["source"] == "regles"


def test_ia_recommandations_personnalisees(client, db):
    db.search_rows = [product_row(id=41), product_row(id=42, nom="Savon noir", prix_min=Decimal("1500.00"))]
    payload = {"categories": ["beaute"], "budget_max": 40000}
    response = client.post("/ia/recommandations", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert [r["id"] for r in data["recommandations"]] == [41, 42]
    ((query, args),) = db.search_calls("page")
    assert "beaute" in args and 40000 in args  # catégorie et budget filtrés par le SQL


# ==========================================
# 3. Tests Scan QR (décodage)
# ==========================================

# FastAPI décode seulement : code normalisé et URL de la page de
# vérification (FRONTEND_BASE_URL, http://localhost:5173 en test), sans
# certificat ni base de passeports ; Django certifie, compte et journalise.
# Contenu refusé : 400 avec code machine (jamais 200 `valide: false`).
# Détail :
# tests/test_scan_qr.py.

def test_scan_qr_code_direct_valide(client):
    payload = {"qr_data": "PAS-2026-1A2B3C4D"}
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "code_passeport": "PAS-2026-1A2B3C4D",
        "url_verification_publique": "http://localhost:5173/qr/verifier/PAS-2026-1A2B3C4D",
    }


def test_scan_qr_url_complete(client):
    # Seule l'origine de FRONTEND_BASE_URL est acceptée (ici celle de dev) :
    # un autre domaine, même anitche.ci, est refusé.
    payload = {"qr_data": "http://localhost:5173/qr/verifier/PAS-2026-0A1B2C3D"}
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 200
    assert response.json()["code_passeport"] == "PAS-2026-0A1B2C3D"

    other_domain = client.post("/qr/scan", json={"qr_data": "https://anitche.ci/qr/verifier/PAS-2026-0A1B2C3D"})
    assert other_domain.status_code == 400
    assert other_domain.json()["errors"] == {"code": ["qr_non_anitche"]}


def test_scan_qr_code_invalide(client):
    payload = {"qr_data": "QR-INVALIDE-RANDOM"}
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert data["errors"] == {"code": ["code_passeport_invalide"]}
    assert "QR-INVALIDE-RANDOM" not in response.text


def test_consulter_passeport_get(client):
    # Route absente de FastAPI : la consultation d'un passeport par son
    # code est celle de Django, GET /api/passeports/verifier/{code}/.
    response = client.get("/qr/passeport/PAS-2026-1A2B3C4D")
    assert response.status_code == 404


# ==========================================
# 4. Tests Suivi GPS & Télémétrie Livreur
# ==========================================

def test_mise_a_jour_position_gps_et_consultation(client, db):
    # Le livreur est celui du jeton (livreur_id du corps ignoré), l'accès
    # est lu dans PostgreSQL (livraison en cours, livreur assigné). Ni
    # statut ni livreur_id dans la réponse ; distance et temps
    # restants seulement si le client a donné son point GPS (ici, non).
    # Cas refusés : test_suivi_gps_acces_refuse.py et test_suivi_gps.py.
    livraison_id = "550e8400-e29b-41d4-a716-446655440000"
    db.add_delivery(livraison_id, status="en_cours", courier_id=42, client_id=7)
    db.add_user(42, "livreur")
    payload = {
        "livraison_id": livraison_id,
        "livreur_id": 42,
        "latitude": 5.3350,
        "longitude": -4.0020,
        "vitesse_kmh": 32.5,
        "cap_degres": 120.0,
    }

    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=42, role="livreur")

    # 1. Envoi de la position par le livreur
    response_post = client.post("/livraison/position", json=payload)
    assert response_post.status_code == 200
    data_post = response_post.json()
    assert data_post["livraison_id"] == livraison_id
    assert data_post["horodatage"].endswith("Z")
    assert data_post["distance_restante_km"] is None
    assert data_post["temps_estime_minutes"] is None
    assert "livreur_id" not in data_post and "statut" not in data_post

    # 2. Consultation de la position (livreur assigné ; client : test_suivi_gps.py)
    response_get = client.get(f"/livraison/position/{livraison_id}")
    assert response_get.status_code == 200
    data_get = response_get.json()
    assert data_get["latitude"] == 5.3350
    assert data_get["longitude"] == -4.0020
    assert data_get["vitesse_kmh"] == 32.5
    assert "livreur_id" not in data_get
