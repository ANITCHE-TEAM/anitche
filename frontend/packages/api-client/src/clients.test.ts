import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { creerClientNu, creerClients, ok, resoudreBase, sansJeton } from "./clients";
import { ErreurApi } from "./erreurs";
import type { SchemaDjango } from "./types";
import { erreur } from "./testing";
import { serveur } from "./testing/serveur";

const ORIGINE = "http://api.test";
const PROFIL = `${ORIGINE}/api/utilisateurs/profil/`;
const CONNEXION = `${ORIGINE}/api/utilisateurs/connexion/`;
const VALIDER = `${ORIGINE}/api/commandes/valider-panier/`;
const CATEGORIES = `${ORIGINE}/api/catalogue/categories/`;
const RECHERCHE = `${ORIGINE}/fast/recherche/produits`;

const corpsCommande = { adresse_livraison: {}, coupon_code: "PROMO" } as unknown as SchemaDjango<"SimulerFraisRequest">;

function monter(surcharge: { jeton?: string | undefined; rafraichir?: () => Promise<string> } = {}) {
  let jeton = "jeton" in surcharge ? surcharge.jeton : "ancien";
  const rafraichir = vi.fn(surcharge.rafraichir ?? (() => Promise.resolve("neuf")));
  const surSessionExpiree = vi.fn();
  const clients = creerClients({
    baseDjango: ORIGINE,
    baseFastapi: `${ORIGINE}/fast`,
    obtenirJetonAcces: () => jeton,
    rafraichir: async () => {
      jeton = await rafraichir();
      return jeton;
    },
    surSessionExpiree,
  });
  return { ...clients, rafraichir, surSessionExpiree };
}

describe("resoudreBase", () => {
  it("garde une URL absolue sans barre finale", () => {
    expect(resoudreBase("https://api.anitche.ci/")).toBe("https://api.anitche.ci");
  });

  it("résout une base relative en URL absolue (hors navigateur)", () => {
    expect(resoudreBase("/fast")).toBe("http://localhost/fast");
  });
});

describe("jeton d'accès", () => {
  it("est envoyé en Bearer", async () => {
    const entetes: (string | null)[] = [];
    serveur.use(
      http.get(PROFIL, ({ request }) => {
        entetes.push(request.headers.get("Authorization"));
        return HttpResponse.json({ id: 1 });
      }),
    );
    const { django } = monter();

    await django.GET("/api/utilisateurs/profil/");

    expect(entetes).toEqual(["Bearer ancien"]);
  });

  it("n'est pas envoyé par sansJeton(), et l'en-tête interne n'atteint pas le réseau", async () => {
    const vus: Headers[] = [];
    serveur.use(
      http.get(CATEGORIES, ({ request }) => {
        vus.push(request.headers);
        return HttpResponse.json([]);
      }),
    );
    const { django } = monter();

    await django.GET("/api/catalogue/categories/", { ...sansJeton() });

    expect(vus[0]?.get("Authorization")).toBeNull();
    expect(vus[0]?.has("x-anitche-sans-jeton")).toBe(false);
  });

  it("une route publique avec un jeton expiré ne déclenche aucun rafraîchissement", async () => {
    serveur.use(http.get(CATEGORIES, () => HttpResponse.json([])));
    const { django, rafraichir } = monter();

    const { response } = await django.GET("/api/catalogue/categories/", { ...sansJeton() });

    expect(response.status).toBe(200);
    expect(rafraichir).not.toHaveBeenCalled();
  });
});

describe("401", () => {
  let appels: { jeton: string | null; corps: unknown }[];

  beforeEach(() => {
    appels = [];
  });

  it("rafraîchit une fois puis rejoue un POST avec son corps et le jeton neuf", async () => {
    serveur.use(
      http.post(VALIDER, async ({ request }) => {
        const jeton = request.headers.get("Authorization");
        appels.push({ jeton, corps: await request.json() });
        return jeton === "Bearer neuf" ? HttpResponse.json([], { status: 201 }) : erreur(401);
      }),
    );
    const { django, rafraichir, surSessionExpiree } = monter();

    const { response } = await django.POST("/api/commandes/valider-panier/", { body: corpsCommande });

    expect(response.status).toBe(201);
    expect(rafraichir).toHaveBeenCalledTimes(1);
    expect(surSessionExpiree).not.toHaveBeenCalled();
    expect(appels).toEqual([
      { jeton: "Bearer ancien", corps: corpsCommande },
      { jeton: "Bearer neuf", corps: corpsCommande },
    ]);
  });

  it("ne rejoue qu'une fois : un second 401 déclenche surSessionExpiree", async () => {
    let nombre = 0;
    serveur.use(
      http.get(PROFIL, () => {
        nombre += 1;
        return erreur(401);
      }),
    );
    const { django, rafraichir, surSessionExpiree } = monter();

    const { response } = await django.GET("/api/utilisateurs/profil/");

    expect(response.status).toBe(401);
    expect(nombre).toBe(2);
    expect(rafraichir).toHaveBeenCalledTimes(1);
    expect(surSessionExpiree).toHaveBeenCalledTimes(1);
  });

  it("session expirée quand le rafraîchissement échoue, sans rejeu", async () => {
    let nombre = 0;
    serveur.use(
      http.get(PROFIL, () => {
        nombre += 1;
        return erreur(401);
      }),
    );
    const { django, surSessionExpiree } = monter({ rafraichir: () => Promise.reject(new Error("refus")) });

    const { response } = await django.GET("/api/utilisateurs/profil/");

    expect(response.status).toBe(401);
    expect(nombre).toBe(1);
    expect(surSessionExpiree).toHaveBeenCalledTimes(1);
  });

  it("un 401 sur connexion/ (mauvais mot de passe) ne rafraîchit ni ne déconnecte", async () => {
    serveur.use(http.post(CONNEXION, () => erreur(401, { detail: "Identifiants invalides." })));
    const { django, rafraichir, surSessionExpiree } = monter();

    const erreurAttendue = await ok(
      django.POST("/api/utilisateurs/connexion/", { body: { email: "a@b.ci", password: "x" } }),
    ).catch((e: unknown) => e);

    expect(erreurAttendue).toBeInstanceOf(ErreurApi);
    expect((erreurAttendue as ErreurApi).statut).toBe(401);
    expect(rafraichir).not.toHaveBeenCalled();
    expect(surSessionExpiree).not.toHaveBeenCalled();
  });

  it("le client nu n'a aucun middleware : pas de jeton, pas de rafraîchissement", async () => {
    const entetes: (string | null)[] = [];
    serveur.use(
      http.post(CONNEXION, ({ request }) => {
        entetes.push(request.headers.get("Authorization"));
        return erreur(401);
      }),
    );

    const { response } = await creerClientNu(ORIGINE).POST("/api/utilisateurs/connexion/", {
      body: { email: "a@b.ci", password: "x" },
    });

    expect(response.status).toBe(401);
    expect(entetes).toEqual([null]);
  });

  it("FastAPI : même rafraîchissement, et le rejeu part vers /fast", async () => {
    const jetons: (string | null)[] = [];
    serveur.use(
      http.get(RECHERCHE, ({ request }) => {
        const jeton = request.headers.get("Authorization");
        jetons.push(jeton);
        return jeton === "Bearer neuf" ? HttpResponse.json({ count: 0, next: null, previous: null, results: [] }) : erreur(401);
      }),
    );
    const { fastapi } = monter();

    const { response } = await fastapi.GET("/recherche/produits");

    expect(response.status).toBe(200);
    expect(jetons).toEqual(["Bearer ancien", "Bearer neuf"]);
  });
});

describe("429, 503 et erreurs de réponse", () => {
  it("429 : aucune relance, retryAfter en secondes", async () => {
    let nombre = 0;
    serveur.use(
      http.get(PROFIL, () => {
        nombre += 1;
        return erreur(429, { detail: "Trop de requêtes.", retryAfter: 42 });
      }),
    );
    const { django, rafraichir, surSessionExpiree } = monter();

    const e = (await ok(django.GET("/api/utilisateurs/profil/")).catch((x: unknown) => x)) as ErreurApi;

    expect(e.statut).toBe(429);
    expect(e.retryAfter).toBe(42);
    expect(nombre).toBe(1);
    expect(rafraichir).not.toHaveBeenCalled();
    expect(surSessionExpiree).not.toHaveBeenCalled();
  });

  it("503 FastAPI : ErreurApi source fastapi, jamais de déconnexion", async () => {
    serveur.use(http.get(RECHERCHE, () => erreur(503, { detail: "Service indisponible.", errors: { code: ["django_injoignable"] } })));
    const { fastapi, rafraichir, surSessionExpiree } = monter();

    const e = (await ok(fastapi.GET("/recherche/produits")).catch((x: unknown) => x)) as ErreurApi;

    expect(e.statut).toBe(503);
    expect(e.source).toBe("fastapi");
    expect(e.code).toBe("django_injoignable");
    expect(rafraichir).not.toHaveBeenCalled();
    expect(surSessionExpiree).not.toHaveBeenCalled();
  });

  it("502 HTML de nginx : detail générique, aucun champ", async () => {
    serveur.use(
      http.get(PROFIL, () => new HttpResponse("<html>502 Bad Gateway</html>", { status: 502, headers: { "Content-Type": "text/html" } })),
    );
    const { django } = monter();

    const e = (await ok(django.GET("/api/utilisateurs/profil/")).catch((x: unknown) => x)) as ErreurApi;

    expect(e).toBeInstanceOf(ErreurApi);
    expect(e.statut).toBe(502);
    expect(e.detail).not.toContain("html");
    expect(e.champs).toEqual({});
  });

  it("400 : champs et code récupérés", async () => {
    serveur.use(http.post(VALIDER, () => erreur(400, { errors: { coupon_code: ["Expiré."] } })));
    const { django } = monter();

    const e = (await ok(django.POST("/api/commandes/valider-panier/", { body: corpsCommande })).catch((x: unknown) => x)) as ErreurApi;

    expect(e.champs).toEqual({ coupon_code: ["Expiré."] });
  });

  it("ok() renvoie les données d'une réponse réussie", async () => {
    serveur.use(http.get(CATEGORIES, () => HttpResponse.json([{ id: 1, nom: "Mode" }])));
    const { django } = monter();

    const donnees = await ok(django.GET("/api/catalogue/categories/"));

    expect(donnees).toEqual([{ id: 1, nom: "Mode" }]);
  });
});

describe("réseau coupé", () => {
  it("devient une ErreurApi de statut 0, avec la source", async () => {
    serveur.use(http.get(PROFIL, () => HttpResponse.error()));
    const { django } = monter();

    const e = await django.GET("/api/utilisateurs/profil/").catch((x: unknown) => x);

    expect(e).toBeInstanceOf(ErreurApi);
    expect((e as ErreurApi).statut).toBe(0);
    expect((e as ErreurApi).source).toBe("django");
  });

  it("une annulation reste une AbortError (pas une panne réseau)", async () => {
    serveur.use(http.get(PROFIL, () => HttpResponse.json({})));
    const { django } = monter();
    const controle = new AbortController();
    controle.abort();

    const e = await django.GET("/api/utilisateurs/profil/", { signal: controle.signal }).catch((x: unknown) => x);

    expect(e).not.toBeInstanceOf(ErreurApi);
    expect((e as DOMException).name).toBe("AbortError");
  });
});
