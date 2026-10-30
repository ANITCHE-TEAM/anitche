import { ErreurApi } from "@anitche/api-client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { creerAuth, type ConfigAuth } from "./actions";
import {
  creerBus,
  creerStockageMemoire,
  creerVerrous,
  fabriquerJeton,
  installerFauxReseau,
  json,
  type AppelRecu,
} from "./test-support";
import { ErreurSession } from "./types";

const ORIGINE = "http://api.test";
const CONNEXION = "POST /api/utilisateurs/connexion/";
const RAFRAICHIR = "POST /api/utilisateurs/connexion/rafraichir/";
const DECONNEXION = "POST /api/utilisateurs/deconnexion/";
const PROFIL = "GET /api/utilisateurs/profil/";
const PANIER = "GET /api/panier/panier/";

const profil = (role = "client") => ({ id: 1, email: "a@b.ci", nom: "A", prenom: "B", role });

afterEach(() => {
  vi.unstubAllGlobals();
});

function monter(surcharge: Partial<ConfigAuth> = {}) {
  const stockage = surcharge.stockage ?? creerStockageMemoire();
  const surFinDeSession = vi.fn();
  const auth = creerAuth({
    portail: "client",
    baseDjango: ORIGINE,
    baseFastapi: `${ORIGINE}/fast`,
    stockage,
    canal: undefined,
    verrous: undefined,
    surFinDeSession,
    ...surcharge,
  });
  return { auth, stockage: stockage as ReturnType<typeof creerStockageMemoire>, surFinDeSession };
}

/** Réponse 401 tant que le jeton n'est pas `valide`. */
const protegeePar = (valide: string) => (appel: AppelRecu) =>
  appel.authorization === `Bearer ${valide}`
    ? json(200, { ok: true })
    : json(401, { success: false, status_code: 401, detail: "Jeton expiré.", errors: {} });

describe("connecter", () => {
  it("stocke le refresh sous la clé du portail, garde l'accès en mémoire, charge le profil", async () => {
    const access = fabriquerJeton(900);
    installerFauxReseau({
      [CONNEXION]: () => json(200, { access, refresh: "R1" }),
      [PROFIL]: (appel) => (appel.authorization === `Bearer ${access}` ? json(200, profil("vendeur")) : json(401, {})),
    });
    const { auth, stockage } = monter();

    const resultat = await auth.connecter("a@b.ci", "secret");

    expect(resultat.role).toBe("vendeur");
    expect(auth.session.getState()).toMatchObject({ etat: "connecte", role: "vendeur" });
    expect(stockage.valeur()).toBe("R1");
    await expect(auth.obtenirJetonValide()).resolves.toBe(access);
  });

  it("un 401 sur connexion/ est « identifiants refusés » : aucune tentative de rafraîchissement", async () => {
    const reseau = installerFauxReseau({
      [CONNEXION]: () => json(401, { success: false, status_code: 401, detail: "Identifiants invalides.", errors: {} }),
      [RAFRAICHIR]: () => json(200, { access: "X", refresh: "Y" }),
    });
    const { auth, stockage, surFinDeSession } = monter({ stockage: creerStockageMemoire("R-EXISTANT") });

    const echec = await auth.connecter("a@b.ci", "mauvais").catch((e: unknown) => e);

    expect(echec).toBeInstanceOf(ErreurApi);
    expect((echec as ErreurApi).statut).toBe(401);
    expect(reseau.compter(RAFRAICHIR)).toBe(0);
    expect(stockage.valeur()).toBe("R-EXISTANT");
    expect(surFinDeSession).not.toHaveBeenCalled();
  });

  it("n'envoie aucun Authorization sur connexion/, même avec un jeton déjà en mémoire (il serait refusé s'il est expiré)", async () => {
    const reseau = installerFauxReseau({
      [CONNEXION]: () => json(200, { access: "A1", refresh: "R1" }),
      [PROFIL]: () => json(200, profil()),
    });
    const { auth } = monter();
    await auth.connecter("a@b.ci", "secret");

    await auth.connecter("a@b.ci", "secret");

    const connexions = reseau.appels.filter((a) => `${a.methode} ${a.chemin}` === CONNEXION);
    expect(connexions.map((a) => a.authorization)).toEqual([null, null]);
  });

  it("profil illisible après connexion : la session n'est pas laissée à moitié ouverte", async () => {
    installerFauxReseau({
      [CONNEXION]: () => json(200, { access: "A1", refresh: "R1" }),
      [PROFIL]: () => json(500, {}),
      [DECONNEXION]: () => new Response(null, { status: 205 }),
    });
    const { auth, stockage } = monter();

    await expect(auth.connecter("a@b.ci", "secret")).rejects.toBeInstanceOf(ErreurApi);

    expect(stockage.valeur()).toBeUndefined();
    expect(auth.session.getState().etat).toBe("invite");
  });
});

describe("rafraîchissement sur 401", () => {
  const sessionExpiree = () => creerStockageMemoire("R1");

  it("10 requêtes simultanées expirées : 1 seul appel rafraichir/, toutes rejouées avec le nouveau jeton", async () => {
    const reseau = installerFauxReseau({
      [PANIER]: protegeePar("A2"),
      [RAFRAICHIR]: async () => {
        await new Promise((resolu) => setTimeout(resolu, 10));
        return json(200, { access: "A2", refresh: "R2" });
      },
    });
    const { auth, stockage } = monter({ stockage: sessionExpiree() });

    const reponses = await Promise.all(
      Array.from({ length: 10 }, () => auth.clients.django.GET("/api/panier/panier/")),
    );

    expect(reponses.map((r) => r.response.status)).toEqual(Array(10).fill(200));
    expect(reseau.compter(RAFRAICHIR)).toBe(1);
    expect(stockage.valeur()).toBe("R2");
  });

  it("refresh refusé (401) : session terminée, stockage vidé, fin de session signalée", async () => {
    installerFauxReseau({
      [PANIER]: protegeePar("jamais"),
      [RAFRAICHIR]: () => json(401, { success: false, status_code: 401, detail: "Jeton invalide.", errors: {} }),
    });
    const { auth, stockage, surFinDeSession } = monter({ stockage: sessionExpiree() });

    const { response } = await auth.clients.django.GET("/api/panier/panier/");

    expect(response.status).toBe(401);
    expect(stockage.valeur()).toBeUndefined();
    expect(auth.session.getState().etat).toBe("invite");
    expect(surFinDeSession).toHaveBeenCalledTimes(1);
  });

  it("503 pendant le rafraîchissement : pas de déconnexion, le refresh reste utilisable", async () => {
    installerFauxReseau({
      [PANIER]: protegeePar("jamais"),
      [RAFRAICHIR]: () => json(503, { success: false, status_code: 503, detail: "Indisponible.", errors: {} }),
    });
    const { auth, stockage, surFinDeSession } = monter({ stockage: sessionExpiree() });
    auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    await auth.clients.django.GET("/api/panier/panier/");

    expect(stockage.valeur()).toBe("R1");
    expect(auth.session.getState().etat).toBe("connecte");
    expect(surFinDeSession).not.toHaveBeenCalled();
  });

  it("réseau coupé pendant le rafraîchissement : pas de déconnexion", async () => {
    installerFauxReseau({
      [PANIER]: protegeePar("jamais"),
      [RAFRAICHIR]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const { auth, stockage, surFinDeSession } = monter({ stockage: sessionExpiree() });
    auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    await auth.clients.django.GET("/api/panier/panier/");

    expect(stockage.valeur()).toBe("R1");
    expect(auth.session.getState().etat).toBe("connecte");
    expect(surFinDeSession).not.toHaveBeenCalled();
  });

  it("401 persistant après un rafraîchissement réussi : la session est terminée", async () => {
    installerFauxReseau({
      [PANIER]: protegeePar("jamais"),
      [RAFRAICHIR]: () => json(200, { access: "A2", refresh: "R2" }),
    });
    const { auth, surFinDeSession } = monter({ stockage: sessionExpiree() });
    auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    await auth.clients.django.GET("/api/panier/panier/");

    expect(auth.session.getState().etat).toBe("invite");
    expect(surFinDeSession).toHaveBeenCalledTimes(1);
  });

  it("invité qui touche une route protégée : aucun appel rafraichir/, aucune fausse fin de session", async () => {
    const reseau = installerFauxReseau({ [PANIER]: protegeePar("jamais") });
    const { auth, surFinDeSession } = monter();
    auth.session.setState({ etat: "invite", role: undefined, profil: undefined }, true);

    await auth.clients.django.GET("/api/panier/panier/");

    expect(reseau.compter(RAFRAICHIR)).toBe(0);
    expect(surFinDeSession).not.toHaveBeenCalled();
  });
});

describe("deconnecter", () => {
  it("révoque le refresh sans Authorization, efface le stockage et passe invité", async () => {
    const reseau = installerFauxReseau({ [DECONNEXION]: () => new Response(null, { status: 205 }) });
    const { auth, stockage, surFinDeSession } = monter({ stockage: creerStockageMemoire("R1") });
    auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    await auth.deconnecter();

    const appel = reseau.appels[0];
    expect(appel?.corps).toEqual({ refresh: "R1" });
    expect(appel?.authorization).toBeNull();
    expect(stockage.valeur()).toBeUndefined();
    expect(auth.session.getState().etat).toBe("invite");
    expect(surFinDeSession).toHaveBeenCalledTimes(1);
  });

  it("l'appel réseau en échec n'empêche pas l'effacement local", async () => {
    installerFauxReseau({
      [DECONNEXION]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const { auth, stockage, surFinDeSession } = monter({ stockage: creerStockageMemoire("R1") });
    auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    await expect(auth.deconnecter()).resolves.toBeUndefined();

    expect(stockage.valeur()).toBeUndefined();
    expect(auth.session.getState().etat).toBe("invite");
    expect(surFinDeSession).toHaveBeenCalledTimes(1);
  });

  it("l'efface localement avant d'attendre le réseau", async () => {
    let liberer: () => void = () => undefined;
    const reseau = installerFauxReseau({
      [DECONNEXION]: () => new Promise<Response>((r) => (liberer = () => r(new Response(null, { status: 205 })))),
    });
    const { auth, stockage } = monter({ stockage: creerStockageMemoire("R1") });
    auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    const enCours = auth.deconnecter();
    await vi.waitFor(() => expect(reseau.compter(DECONNEXION)).toBe(1));

    // La requête réseau est encore en attente.
    expect(auth.session.getState().etat).toBe("invite");
    expect(stockage.valeur()).toBeUndefined();
    liberer();
    await enCours;
  });

  it("prévient les autres onglets du portail", async () => {
    installerFauxReseau({ [DECONNEXION]: () => new Response(null, { status: 205 }) });
    const bus = creerBus();
    const a = monter({ stockage: creerStockageMemoire("R1"), canal: bus.canal() });
    const b = monter({ stockage: creerStockageMemoire("R1"), canal: bus.canal() });
    b.auth.session.setState({ etat: "connecte", role: "client", profil: undefined }, true);

    await a.auth.deconnecter();
    await Promise.resolve();

    expect(b.auth.session.getState().etat).toBe("invite");
    expect(b.stockage.valeur()).toBeUndefined();
    expect(b.surFinDeSession).toHaveBeenCalledTimes(1);
  });
});

describe("demarrer", () => {
  it("sans refresh stocké : invité, aucun appel réseau", async () => {
    const reseau = installerFauxReseau({});
    const { auth } = monter();

    await auth.demarrer();

    expect(auth.session.getState().etat).toBe("invite");
    expect(reseau.appels).toEqual([]);
  });

  it("avec un refresh : rafraîchit, charge le profil, devient connecté", async () => {
    installerFauxReseau({
      [RAFRAICHIR]: () => json(200, { access: "A2", refresh: "R2" }),
      [PROFIL]: (appel) => (appel.authorization === "Bearer A2" ? json(200, profil("livreur")) : json(401, {})),
    });
    const { auth, stockage } = monter({ stockage: creerStockageMemoire("R1") });

    await auth.demarrer();

    expect(auth.session.getState()).toMatchObject({ etat: "connecte", role: "livreur" });
    expect(stockage.valeur()).toBe("R2");
  });

  it("refresh refusé : invité et stockage vidé", async () => {
    installerFauxReseau({ [RAFRAICHIR]: () => json(401, { success: false, status_code: 401, detail: "x", errors: {} }) });
    const { auth, stockage } = monter({ stockage: creerStockageMemoire("R1") });

    await expect(auth.demarrer()).rejects.toBeInstanceOf(ErreurSession);

    expect(auth.session.getState().etat).toBe("invite");
    expect(stockage.valeur()).toBeUndefined();
  });

  it("panne réseau au démarrage : la session reste inconnue et le refresh est conservé", async () => {
    installerFauxReseau({
      [RAFRAICHIR]: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const { auth, stockage } = monter({ stockage: creerStockageMemoire("R1") });

    await expect(auth.demarrer()).rejects.toBeInstanceOf(ErreurApi);

    expect(auth.session.getState().etat).toBe("inconnue");
    expect(stockage.valeur()).toBe("R1");
  });
});

describe("obtenirJetonValide (WebSocket)", () => {
  it("renvoie l'accès en mémoire tant qu'il reste plus de 60 s", async () => {
    const valide = fabriquerJeton(600);
    const reseau = installerFauxReseau({
      [CONNEXION]: () => json(200, { access: valide, refresh: "R1" }),
      [PROFIL]: () => json(200, profil()),
    });
    const { auth } = monter();
    await auth.connecter("a@b.ci", "secret");
    const avant = reseau.appels.length;

    await expect(auth.obtenirJetonValide()).resolves.toBe(valide);

    expect(reseau.appels.length).toBe(avant);
  });

  it("rafraîchit quand l'accès expire dans moins de 60 s", async () => {
    const presqueExpire = fabriquerJeton(30);
    const neuf = fabriquerJeton(900);
    const reseau = installerFauxReseau({
      [CONNEXION]: () => json(200, { access: presqueExpire, refresh: "R1" }),
      [PROFIL]: () => json(200, profil()),
      [RAFRAICHIR]: () => json(200, { access: neuf, refresh: "R2" }),
    });
    const { auth, stockage } = monter();
    await auth.connecter("a@b.ci", "secret");

    await expect(auth.obtenirJetonValide()).resolves.toBe(neuf);

    expect(reseau.compter(RAFRAICHIR)).toBe(1);
    expect(stockage.valeur()).toBe("R2");
  });

  it("plusieurs demandes simultanées (reconnexions WebSocket) : 1 seul rafraîchissement", async () => {
    const reseau = installerFauxReseau({
      [RAFRAICHIR]: () => json(200, { access: fabriquerJeton(900), refresh: "R2" }),
    });
    const { auth } = monter({ stockage: creerStockageMemoire("R1") });

    await Promise.all([auth.obtenirJetonValide(), auth.obtenirJetonValide(), auth.obtenirJetonValide()]);

    expect(reseau.compter(RAFRAICHIR)).toBe(1);
  });

  it("sans session : ErreurSession", async () => {
    installerFauxReseau({});
    const { auth } = monter();

    await expect(auth.obtenirJetonValide()).rejects.toBeInstanceOf(ErreurSession);
  });
});

describe("connexion dans un autre onglet", () => {
  it("localStorage partagé : l'onglet invité devient connecté", async () => {
    installerFauxReseau({
      [CONNEXION]: () => json(200, { access: "A1", refresh: "R1" }),
      [PROFIL]: () => json(200, profil("client")),
    });
    const bus = creerBus();
    const verrous = creerVerrous();
    const stockage = creerStockageMemoire();
    const a = monter({ stockage, canal: bus.canal(), verrous });
    const b = monter({ stockage, canal: bus.canal(), verrous });
    b.auth.session.setState({ etat: "invite", role: undefined, profil: undefined }, true);

    await a.auth.connecter("a@b.ci", "secret");
    await vi.waitFor(() => expect(b.auth.session.getState().etat).toBe("connecte"));
  });

  it("sessionStorage (admin) : un onglet ouvert à neuf ne reçoit pas la session d'un autre", async () => {
    const reseau = installerFauxReseau({
      [CONNEXION]: () => json(200, { access: "A1", refresh: "R1" }),
      [PROFIL]: () => json(200, profil("admin")),
    });
    const bus = creerBus();
    const a = monter({ stockage: creerStockageMemoire(), canal: bus.canal() });
    const b = monter({ stockage: creerStockageMemoire(), canal: bus.canal() });
    b.auth.session.setState({ etat: "invite", role: undefined, profil: undefined }, true);

    await a.auth.connecter("a@b.ci", "secret");
    await Promise.resolve();
    await Promise.resolve();

    expect(b.auth.session.getState().etat).toBe("invite");
    expect(b.stockage.valeur()).toBeUndefined();
    expect(reseau.compter(PROFIL)).toBe(1);
  });
});
