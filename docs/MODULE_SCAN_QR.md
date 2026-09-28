# Module scan QR — contrat et règles

> Périmètre : backend FastAPI, `backend-fastapi/app/` (routeur `routeurs/scan_qr.py`, décodeur `services/qr_decode.py`, modèles `modeles/scan_qr.py`).
> État : refonte de septembre 2026 (module 3 de la refonte FastAPI). **FastAPI décode, Django certifie.** Les passeports, la certification, le comptage et le journal des scans sont ceux de Django ([`MODULE_PASSEPORT_QR.md`](./MODULE_PASSEPORT_QR.md)) : FastAPI ne lit aucune base, n'appelle pas Django et ne garde aucun état.

## 1. Rôle

Le frontend envoie le **contenu brut** lu par la caméra (l'URL imprimée dans le QR), ou le code saisi à la main. FastAPI répond :

- **lequel** : le code du passeport, sous sa forme canonique `PAS-AAAA-XXXXXXXX` ;
- **où l'ouvrir** : l'URL de la page de vérification du frontend (`url_verification_publique`, même valeur que Django) ;
- ou **pourquoi pas** : un refus avec code machine (autre site, URL piégée, page ANITCHE qui n'est pas un passeport, texte qui n'est pas un code).

Un 200 **n'atteste pas** que le passeport existe. C'est la page de vérification qui appelle Django (`GET /api/passeports/verifier/<code>/`), et Django qui certifie, compte le scan et le journalise.

Pourquoi côté serveur plutôt que dans le frontend : un décodage sûr n'est pas une ligne. Le diagnostic a trouvé 27 contournements à refuser (hôte piégé, identifiants, antislash lu différemment par Python et par les navigateurs, homographes, `ſ` et `ı`, séparateurs). Une seule implémentation testée sert le site et une future application mobile, avec l'origine lue dans la même variable que Django.

## 2. Ce sur quoi le module s'appuie

| Élément | Où | Rôle |
|---|---|---|
| `FRONTEND_BASE_URL` | `app/core/settings.py` ; même variable et même valeur que Django (`docker-compose.prod.yml` : `${FRONTEND_BASE_URL:-https://anitche.com}` pour les deux services ; dev : `http://localhost:5173`) | Seule origine acceptée au décodage ; base de `url_verification_publique` |
| Chemin `/qr/verifier/{code}` | `CHEMIN_VERIFICATION_PUBLIQUE` (`backend-django/apps/passeport_qr/models.py`) | Chemin de la page imprimée dans les QR (test de parité côté FastAPI) |
| Format du code | `generer_code_passeport` (même fichier Django) | `PAS-<année>-<8 hexadécimaux majuscules>` (test de parité) |
| Limite de débit `qr_scan` | `app/core/rate_limit.py` (socle), Redis | 600/heure par IP, comme `passeport_verification` côté Django |

Aucune base de données, aucun appel HTTP, aucun cache. Redis ne sert qu'à la limite de débit.

## 3. Architecture

```text
routeurs/scan_qr.py     POST /qr/scan : validation du corps, appel du décodeur, 400 avec code machine
services/qr_decode.py   fonctions pures : decode(), normalize_typed_code(), verification_url()
modeles/scan_qr.py      DemandeScanQR (qr_data, 1 à 512 caractères), ReponseScanQR (2 champs)
```

L'ancienne maquette (`services/qr_service.py` : 3 passeports écrits en dur, compteur de scans en mémoire, domaine `anitche.ci` en dur) est **supprimée**, ainsi que la route `GET /qr/passeport/{code}`.

## 4. Contrat HTTP

Base : `http://localhost:8001` en dev, `https://anitche.com/fast` en prod ([`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 12). Route **publique** (aucun jeton), limitée à **600 appels par heure et par IP** (scope `qr_scan`).

### `POST /qr/scan`

Corps :

```json
{"qr_data": "https://anitche.com/qr/verifier/PAS-2026-1A2B3C4D"}
```

`qr_data` : chaîne obligatoire, **1 à 512 caractères** après retrait des espaces de début et de fin. L'URL de prod fait 49 caractères ; 512 laissent la place à des paramètres de suivi. Champ inconnu : ignoré.

Réponse **200** :

```json
{
  "code_passeport": "PAS-2026-1A2B3C4D",
  "url_verification_publique": "https://anitche.com/qr/verifier/PAS-2026-1A2B3C4D"
}
```

- `code_passeport` : forme canonique (majuscules, tirets ASCII).
- `url_verification_publique` : `FRONTEND_BASE_URL` sans « / » final + `/qr/verifier/` + code. **Même nom et même construction que Django** : c'est la valeur imprimée dans le QR. Jamais construite à partir de la saisie.
- Aucun champ de certificat (produit, boutique, statut, nombre de scans) : ils viennent de Django.

Erreurs (format commun, `errors.code[0]` pour les refus) :

| Statut | `errors` | `detail` | Cas |
|---|---|---|---|
| 400 | `{"code": ["code_passeport_invalide"]}` | « Code passeport invalide. Format attendu : PAS-AAAA-XXXXXXXX. » | Texte qui n'est ni un code ni une URL |
| 400 | `{"code": ["qr_non_anitche"]}` | « Ce QR code ne provient pas d'ANITCHE. » | Autre domaine, autre schéma (`javascript:`, `data:`, `mailto:`…), URL piégée (§ 5, règle 3) |
| 400 | `{"code": ["lien_non_passeport"]}` | « Ce lien ANITCHE n'est pas un passeport produit. » | Bonne origine mais autre page (fiche produit…), ou code mal formé dans l'URL |
| 400 | `{"qr_data": ["…"]}` | `qr_data: …` | Champ absent, pas une chaîne, vide, plus de 512 caractères |
| 429 | `{}` | « Requête ralentie… » + en-tête `Retry-After` | Plus de 600 décodages par heure et par IP (un refus compte aussi) |
| 503 | `{}` | « Service temporairement indisponible. » | Redis indisponible (limite de débit) |

**La saisie n'est jamais recopiée**, ni dans la réponse (succès comme refus : seule la forme canonique revient), ni dans les journaux (journal d'accès : méthode, chemin, statut, durée).

## 5. Règles de décodage

Dans cet ordre, après retrait des espaces de début et de fin :

1. **Code saisi** : espaces (dont insécables et fines insécables, tabulations, sauts de ligne) et tirets (`-` et `‐ ‑ ‒ – — −`) retirés ; le reste doit être **ASCII**, puis, en majuscules, exactement `PAS` + 4 chiffres + 8 hexadécimaux → `PAS-AAAA-XXXXXXXX`. ASCII vérifié **avant** la mise en majuscules : `"ſ".upper()` vaut `"S"` et `"ı".upper()` vaut `"I"` en Python.
2. Sinon, si le contenu ne commence pas par un schéma (`lettres:`) → `code_passeport_invalide`.
3. URL → `qr_non_anitche` si :
   - un espace, un caractère de contrôle ou invisible (catégories Unicode C : espace sans chasse, inversion du sens d'écriture…) ou un **antislash** apparaît n'importe où ;
   - la partie hôte contient `@` (identifiants) ou un caractère non ASCII (homographes, pleine chasse) ;
   - le port n'est pas un nombre de 0 à 65535, ou l'URL est mal formée ;
   - l'**origine** (schéma, hôte en minuscules, port effectif) diffère de celle de `FRONTEND_BASE_URL`. Comparaison champ par champ après analyse, **jamais par préfixe**. Le port par défaut explicite (`https://anitche.com:443/…`) est la même origine.
4. Bonne origine : le chemin doit être le chemin de base de `FRONTEND_BASE_URL` + `/qr/verifier/` (casse ignorée, ASCII), puis le code (casse ignorée), avec ou sans « / » final. Paramètres (`?utm_source=…`) et fragment ignorés. Sinon → `lien_non_passeport`.

Pourquoi ces précautions (mesuré au diagnostic) :

- un contrôle par préfixe (`startswith("https://anitche.com")`) laisse passer `https://anitche.com.evil.example/…` et `https://anitche.com@evil.example/…` (hôte réel : `evil.example`) ;
- `urlsplit("https://evil.example\@anitche.com/…")` lit l'hôte `anitche.com`, alors qu'un navigateur lit `\` comme `/` et ouvre `evil.example` ; les navigateurs retirent aussi tabulations et sauts de ligne des URL : ces caractères sont refusés **avant** l'analyse ;
- casse ignorée dans l'URL : certains générateurs mettent toute l'URL en majuscules pour utiliser le mode alphanumérique compact des QR.

Exemples (`FRONTEND_BASE_URL = https://anitche.com`) :

| Contenu | Résultat |
|---|---|
| `PAS-2026-1A2B3C4D`, `pas 2026 1a2b3c4d`, `PAS20261A2B3C4D`, `PAS–2026–1A2B3C4D` | 200 `PAS-2026-1A2B3C4D` |
| `https://anitche.com/qr/verifier/PAS-2026-1A2B3C4D` (et en majuscules, avec « / » final, `?utm_source=…`, `#…`) | 200 |
| `https://evil.example/…`, `https://anitche.com.evil.example/…`, `https://anitche.com@evil.example/…`, « а » cyrillique dans l'hôte, `javascript:…`, `data:…`, `mailto:…`, antislash, `http://` au lieu de `https://`, autre port, port non numérique, `anitche.com.` (point final), `https://anitche.ci/…`, `https://www.anitche.com/…` | 400 `qr_non_anitche` |
| `https://anitche.com/produits/…`, `…/PAS-2026-1A2B3C4D/suite`, `…/PAS-2026-TIASSALE01`, `%2F` dans le chemin, `https://anitche.com/?code=PAS-…` | 400 `lien_non_passeport` |
| `n'importe quoi`, `PAS-2026-TIASSALE01`, `PAſ-2026-1A2B3C4D`, chiffres pleine chasse, `voir PAS-2026-1A2B3C4D svp`, `anitche.com/qr/verifier/PAS-…` (sans schéma) | 400 `code_passeport_invalide` |

Cas limite documenté : un texte de la forme `mot:suite` a la syntaxe d'un schéma (`Code: PAS-…`, ou `localhost:5173/qr/…` sans `http://`) : il est traité comme une URL et refusé en `qr_non_anitche`. Refusé dans tous les cas ; seul le code machine diffère.

## 6. Parcours complet

```text
QR imprimé sur l'étiquette = url_verification_publique (espace vendeur Django)
                             = FRONTEND_BASE_URL/qr/verifier/PAS-AAAA-XXXXXXXX

A. Appareil photo du téléphone (hors application)
   QR ─► navigateur ─► page /qr/verifier/:code (frontend) ─► GET /api/passeports/verifier/{code}/ (Django)
                                                             certifie, compte, journalise

B. Scanner intégré ou saisie manuelle (application)
   caméra ─► POST /fast/qr/scan {qr_data} (FastAPI) ─► 200 : ouvrir url_verification_publique
                                                       ─► même page, même appel Django (un seul comptage)
                                                   ─► 400 qr_non_anitche : avertir, NE JAMAIS ouvrir l'URL
```

- Un seul écran de certification, un seul comptage par scan, dans les deux parcours.
- Le parcours A ne dépend pas de FastAPI : si FastAPI répond 503, proposer de scanner avec l'appareil photo du téléphone.
- Consulter un passeport à partir de son code (ancienne route FastAPI `GET /qr/passeport/{code}`) : c'est `GET /api/passeports/verifier/{code}/` de Django.

## 7. Sécurité

| Point | Règle |
|---|---|
| Hameçonnage par QR recopié ou collé sur une étiquette | Seule l'origine de `FRONTEND_BASE_URL` est acceptée ; le frontend n'ouvre **jamais** une URL refusée |
| Énumération des codes | Aucun oracle ajouté : un code bien formé se décode de la même façon qu'il existe ou non ; seule la vérification Django (limitée à 600/h par IP) répond sur l'existence |
| Données | Aucune : ni droit PostgreSQL, ni appel à Django, ni cache |
| Recopie de la saisie | Jamais (réponse, journaux) ; messages d'erreur fixes |
| Taille | 512 caractères au plus (1 Mo renvoyait 3 Mo avec l'ancienne maquette) |
| État | Aucun (fonctions pures, constantes immuables) : plusieurs workers et redémarrages sans effet |
| Débit | `qr_scan` 600/h par IP ; un refus compte aussi |

## 8. Réglages

| Variable | Dev (compose) | Prod |
|---|---|---|
| `FRONTEND_BASE_URL` | `http://localhost:5173` | `${FRONTEND_BASE_URL:-https://anitche.com}`, **même valeur que Django** ; `https://` obligatoire et pas localhost (le service refuse de démarrer sinon) |
| `RATE_LIMITS` (`qr_scan`) | 600/hour | 600/hour |

`FRONTEND_BASE_URL` : ni paramètres, ni fragment, ni identifiants, port numérique (refus au démarrage), comme `PUBLIC_BASE_URL` et `MEDIA_BASE_URL`.

## 9. Changements de contrat (module 3)

Aucun consommateur dans le dépôt (frontend vide) : changements assumés.

| Avant | Après |
|---|---|
| `POST /qr/scan` renvoyait un « certificat » (12 champs : `valide`, `produit_nom`, `nb_scans`…) tiré de 3 passeports écrits en dur | 2 champs : `code_passeport`, `url_verification_publique` ; le certificat est celui de Django |
| `GET /qr/passeport/{code}` | **Supprimée** (404) : utiliser Django `GET /api/passeports/verifier/{code}/` |
| Contenu non reconnu : 200 `valide: false` | 400 avec code machine |
| Code cherché n'importe où, tout domaine accepté | Règles du § 5 |
| URL de vérification `https://anitche.ci/qr/verifier/…` en dur | Construite depuis `FRONTEND_BASE_URL`, comme Django |
| `qr_data` sans taille, recopié dans la réponse | 512 caractères au plus, jamais recopié |

## 10. Failles corrigées (diagnostic de septembre 2026)

| # | Avant | Après |
|---|---|---|
| 1 | 3 passeports inventés, format différent de Django ; aucun vrai code reconnu | Aucune donnée ; format de Django (test de parité) |
| 2 | Compteur de scans en mémoire, par processus, jamais écrit, incrémenté aussi par une simple consultation | Supprimé : Django compte et journalise |
| 3 | Tout contenu contenant un code « certifié », quel que soit le domaine (7 URL piégées certifiées) | Origine de `FRONTEND_BASE_URL` seule, comparée champ par champ |
| 4 | 200 `valide: false` | 400 avec code machine |
| 5 | Aucune taille maximale, saisie recopiée jusqu'à 3 fois | 512 caractères, aucune recopie |
| 6 | Extraction par `re.search` + `IGNORECASE` (`ſ`, `ı`, chiffres Unicode, texte autour, premier code gagnant) | Règles strictes, ASCII |
| 6 bis | Saisies légitimes refusées (espaces, sans tirets, tirets typographiques) | Acceptées |
| 7 | Champs différents de la réponse publique de Django | Plus de certificat côté FastAPI |
| A | Routes synchrones (pool de threads) | `async` |

## 11. Tests

`tests/test_scan_qr.py` (157 tests, sans base ni Django) :

- les **38 cas du prototype** du rapport de diagnostic (10 saisies acceptées, 27 contournements refusés avec le bon code, origine de dev) ;
- autres cas du décodeur : chaque tiret et espace typographique, port par défaut explicite, 16 codes invalides, 26 URL étrangères ou piégées (espaces, contrôles, caractères invisibles, identifiants, ports invalides, pleine chasse, punycode, `%2E`, schéma sans `//`, crochets, NFKC), 15 liens ANITCHE qui ne sont pas des passeports ; origine seule (schéma, hôte, port) ; chemin de base de `FRONTEND_BASE_URL` ;
- validation (absent, `null`, nombre, liste, vide, espaces seuls, 512 acceptés, 513 refusés, corps de 1 Mo) ;
- **aucune recopie** de la saisie dans la réponse ni dans les journaux (tous niveaux) ;
- réponse : 2 champs, URL construite comme Django pour 4 valeurs de `FRONTEND_BASE_URL` ; aucun oracle d'existence ;
- isolation : 0 requête SQL, 0 appel à Django, seules les clés de limite dans Redis ; réponses identiques d'un appel à l'autre ; aucun objet modifiable au niveau des modules ; ancien service supprimé ; route `async` ;
- routes : `GET /qr/passeport/…` → 404, `GET /qr/scan` → 405, OpenAPI (400 documenté avec les 3 codes, 2 champs, `maxLength` 512) ;
- limite `qr_scan` : 429 au format commun avec `Retry-After`, par IP, un refus compte ;
- parité avec Django : chemin `CHEMIN_VERIFICATION_PUBLIQUE`, format de `generer_code_passeport`, même `FRONTEND_BASE_URL` dans les composes.

Test des tests (mutations temporaires du décodeur, toutes détectées) : contrôle par préfixe au lieu de l'origine, antislash accepté, `re.IGNORECASE` sur le code, `@` accepté, chemin comparé en majuscules sans contrôle ASCII, espaces et contrôles acceptés ; recopie de la saisie dans la réponse ou le journal, taille maximale retirée.

Adaptés (même intention, nouveau contrat) : 4 tests de `tests/test_api.py` ; la liste des routes de `tests/test_core_rate_limit.py` (une seule route QR) ; `tests/test_core_settings.py` (`FRONTEND_BASE_URL` malformée refusée). Aucun test d'intégration : ni base ni vue.

## 12. Collection Postman

`postman_scan_qr.json` (hors dépôt, dossier `postman/collections`) : préparation (compte administrateur de démo, lecture d'un vrai passeport et du contenu de son QR), codes valides et variantes, 27 contournements refusés, taille et erreurs, **enchaînement avec la vérification Django** (le scan est compté par Django : `nb_scans` + 1). Chaque exécution ajoute un scan au premier passeport de la liste. Variables : `fast_url`, `django_url`, `frontend_base_url` (= `FRONTEND_BASE_URL`), `admin_email`, `demo_password`.

## 13. Points ouverts

- **À fixer AVANT toute impression de QR** : le domaine (`anitche.com` ou `anitche.ci` : le dépôt mentionne les deux, [`MODULE_PASSEPORT_QR.md`](./MODULE_PASSEPORT_QR.md) § 9) et la page frontend `/qr/verifier/:code` (elle n'existe pas encore). L'URL est figée dès l'impression, et le décodeur n'accepte que l'origine de `FRONTEND_BASE_URL`.
- **Si le domaine change après impression** : ajouter un réglage `QR_ACCEPTED_ORIGINS` (origines `https://` en prod, validées au démarrage) pour que les QR déjà imprimés restent lisibles. Pas avant : aucun besoin aujourd'hui.
- Côté Django (hors périmètre de ce module) : le 404 de la vérification publique recopie la saisie en majuscules et sans limite de longueur dans `detail` et dans le journal ; `.strip().upper()` y transforme aussi `ſ` et `ı` (sans effet sur les vrais codes, hexadécimaux) ; `FRONTEND_BASE_URL` n'y est pas validée en prod. À traiter à la reprise du module Django.
- nginx accepte des corps de 20 Mo sur `/fast/` ; FastAPI lit le JSON entier avant la validation (20 Mo refusés en 0,04 s, mesuré). Un `client_max_body_size` plus bas pour `/fast/` relève de l'étape hébergement.
