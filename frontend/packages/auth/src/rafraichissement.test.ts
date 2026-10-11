import { ErreurApi } from "@anitche/api-client";
import { describe, expect, it, vi } from "vitest";
import { creerCoordinateur } from "./rafraichissement";
import { creerBus, creerStockageMemoire, creerVerrous } from "./test-support";
import { ErreurSession, type Jetons } from "./types";

const ROTATION: Record<string, Jetons> = {
  R1: { access: "A2", refresh: "R2" },
  R2: { access: "A3", refresh: "R3" },
};

/** Faux `rafraichir/` : le refresh est à usage unique, le rejouer donne 401 comme le backend. */
function fauxBackend() {
  const consommes = new Set<string>();
  const appelerRafraichir = vi.fn(async (refresh: string): Promise<Jetons> => {
    await Promise.resolve();
    const jetons = ROTATION[refresh];
    if (consommes.has(refresh) || jetons === undefined) throw new ErreurApi(401, "django");
    consommes.add(refresh);
    return jetons;
  });
  return { appelerRafraichir };
}

function monterOnglet(options: {
  stockage?: ReturnType<typeof creerStockageMemoire>;
  verrous?: ReturnType<typeof creerVerrous> | undefined;
  bus?: ReturnType<typeof creerBus>;
  appelerRafraichir: (refresh: string) => Promise<Jetons>;
}) {
  const stockage = options.stockage ?? creerStockageMemoire("R1");
  const surSessionPerdue = vi.fn();
  const surJetonsExternes = vi.fn();
  const coordinateur = creerCoordinateur({
    stockage,
    canal: options.bus?.canal(),
    verrous: options.verrous,
    nomVerrou: "anitche-auth-client",
    appelerRafraichir: options.appelerRafraichir,
    surSessionPerdue,
    surJetonsExternes,
  });
  return { stockage, coordinateur, surSessionPerdue, surJetonsExternes };
}

describe("dans un onglet", () => {
  it("10 demandes simultanées déclenchent 1 seul appel rafraichir/", async () => {
    const { appelerRafraichir } = fauxBackend();
    const { coordinateur, stockage } = monterOnglet({ appelerRafraichir });

    const resultats = await Promise.all(Array.from({ length: 10 }, () => coordinateur.rafraichir()));

    expect(appelerRafraichir).toHaveBeenCalledTimes(1);
    expect(resultats).toEqual(Array(10).fill("A2"));
    expect(stockage.valeur()).toBe("R2");
    expect(coordinateur.accesCourant()).toBe("A2");
  });

  it("un rafraîchissement suivant repart du nouveau refresh", async () => {
    const { appelerRafraichir } = fauxBackend();
    const { coordinateur } = monterOnglet({ appelerRafraichir });

    await coordinateur.rafraichir();
    const second = await coordinateur.rafraichir();

    expect(appelerRafraichir).toHaveBeenNthCalledWith(1, "R1");
    expect(appelerRafraichir).toHaveBeenNthCalledWith(2, "R2");
    expect(second).toBe("A3");
  });

  it("sans refresh stocké : ErreurSession, aucun appel", async () => {
    const { appelerRafraichir } = fauxBackend();
    const { coordinateur } = monterOnglet({ appelerRafraichir, stockage: creerStockageMemoire() });

    await expect(coordinateur.rafraichir()).rejects.toBeInstanceOf(ErreurSession);
    expect(appelerRafraichir).not.toHaveBeenCalled();
  });

  it("fonctionne sans Web Locks (repli) : toujours 1 appel par onglet", async () => {
    const { appelerRafraichir } = fauxBackend();
    const { coordinateur } = monterOnglet({ appelerRafraichir, verrous: undefined });

    await Promise.all([coordinateur.rafraichir(), coordinateur.rafraichir()]);

    expect(appelerRafraichir).toHaveBeenCalledTimes(1);
  });
});

describe("entre onglets (localStorage partagé)", () => {
  it("deux onglets qui expirent ensemble : 1 seul appel, le second adopte la paire stockée", async () => {
    const { appelerRafraichir } = fauxBackend();
    const stockage = creerStockageMemoire("R1");
    const verrous = creerVerrous();
    const bus = creerBus();
    const a = monterOnglet({ appelerRafraichir, stockage, verrous, bus });
    const b = monterOnglet({ appelerRafraichir, stockage, verrous, bus });

    const [accesA, accesB] = await Promise.all([a.coordinateur.rafraichir(), b.coordinateur.rafraichir()]);

    expect(appelerRafraichir).toHaveBeenCalledTimes(1);
    expect(accesA).toBe("A2");
    expect(accesB).toBe("A2");
    expect(stockage.valeur()).toBe("R2");
  });

  it("n'écrit pas en double dans le stockage partagé quand un autre onglet a tourné", async () => {
    const { appelerRafraichir } = fauxBackend();
    const stockage = creerStockageMemoire("R1");
    const ecrire = vi.spyOn(stockage, "ecrireRefresh");
    const verrous = creerVerrous();
    const bus = creerBus();
    const a = monterOnglet({ appelerRafraichir, stockage, verrous, bus });
    const b = monterOnglet({ appelerRafraichir, stockage, verrous, bus });

    await Promise.all([a.coordinateur.rafraichir(), b.coordinateur.rafraichir()]);

    expect(ecrire).toHaveBeenCalledTimes(1);
  });
});

describe("entre onglets dupliqués (sessionStorage séparés)", () => {
  it("deux onglets avec le même refresh : 1 seul appel, le second reçoit le nouveau refresh par diffusion", async () => {
    const { appelerRafraichir } = fauxBackend();
    const verrous = creerVerrous();
    const bus = creerBus();
    const stockageA = creerStockageMemoire("R1");
    const stockageB = creerStockageMemoire("R1");
    const a = monterOnglet({ appelerRafraichir, stockage: stockageA, verrous, bus });
    const b = monterOnglet({ appelerRafraichir, stockage: stockageB, verrous, bus });

    const [accesA, accesB] = await Promise.all([a.coordinateur.rafraichir(), b.coordinateur.rafraichir()]);

    expect(appelerRafraichir).toHaveBeenCalledTimes(1);
    expect(accesA).toBe("A2");
    expect(accesB).toBe("A2");
    expect(stockageA.valeur()).toBe("R2");
    expect(stockageB.valeur()).toBe("R2");
  });

  it("un onglet qui tient une autre session n'adopte pas les jetons diffusés", async () => {
    const { appelerRafraichir } = fauxBackend();
    const verrous = creerVerrous();
    const bus = creerBus();
    const a = monterOnglet({ appelerRafraichir, stockage: creerStockageMemoire("R1"), verrous, bus });
    const autre = monterOnglet({ appelerRafraichir, stockage: creerStockageMemoire("AUTRE"), verrous, bus });
    const neuf = monterOnglet({ appelerRafraichir, stockage: creerStockageMemoire(), verrous, bus });

    await a.coordinateur.rafraichir();
    await Promise.resolve();

    expect(autre.stockage.valeur()).toBe("AUTRE");
    expect(autre.coordinateur.accesCourant()).toBeUndefined();
    expect(neuf.stockage.valeur()).toBeUndefined();
    expect(neuf.coordinateur.accesCourant()).toBeUndefined();
    expect(neuf.surJetonsExternes).not.toHaveBeenCalled();
  });
});

describe("refus du refresh (401)", () => {
  it("vide le stockage, prévient tous les onglets et lève ErreurSession", async () => {
    const appelerRafraichir = vi.fn(() => Promise.reject(new ErreurApi(401, "django")));
    const verrous = creerVerrous();
    const bus = creerBus();
    const a = monterOnglet({ appelerRafraichir, verrous, bus });
    const b = monterOnglet({ appelerRafraichir, stockage: creerStockageMemoire("R1"), verrous, bus });

    await expect(a.coordinateur.rafraichir()).rejects.toBeInstanceOf(ErreurSession);
    await Promise.resolve();

    expect(a.stockage.valeur()).toBeUndefined();
    expect(a.coordinateur.accesCourant()).toBeUndefined();
    expect(a.surSessionPerdue).toHaveBeenCalledTimes(1);
    expect(bus.messages).toContainEqual({ type: "deconnexion" });
    expect(b.stockage.valeur()).toBeUndefined();
    expect(b.surSessionPerdue).toHaveBeenCalledTimes(1);
  });
});

describe("panne transitoire : jamais de déconnexion", () => {
  it.each([
    ["503", new ErreurApi(503, "django")],
    ["réseau coupé", new ErreurApi(0, "django")],
    ["429", new ErreurApi(429, "django", { retryAfter: 30 })],
    ["erreur inattendue", new TypeError("boom")],
  ])("%s : stockage intact, aucune diffusion, l'erreur remonte", async (_nom, erreur) => {
    const appelerRafraichir = vi.fn(() => Promise.reject(erreur));
    const bus = creerBus();
    const a = monterOnglet({ appelerRafraichir, bus });

    await expect(a.coordinateur.rafraichir()).rejects.toBe(erreur);

    expect(a.stockage.valeur()).toBe("R1");
    expect(a.surSessionPerdue).not.toHaveBeenCalled();
    expect(bus.messages).toEqual([]);
    expect(a.coordinateur.echecTransitoire()).toBe(true);
  });

  it("un rafraîchissement réussi ensuite remet le drapeau à zéro", async () => {
    let appels = 0;
    const appelerRafraichir = vi.fn((): Promise<Jetons> => {
      appels += 1;
      return appels === 1 ? Promise.reject(new ErreurApi(503, "django")) : Promise.resolve({ access: "A2", refresh: "R2" });
    });
    const a = monterOnglet({ appelerRafraichir });

    await expect(a.coordinateur.rafraichir()).rejects.toBeInstanceOf(ErreurApi);
    await a.coordinateur.rafraichir();

    expect(a.coordinateur.echecTransitoire()).toBe(false);
  });
});

describe("déconnexion pendant un rafraîchissement", () => {
  it("la réponse tardive ne ressuscite pas la session", async () => {
    let resoudre: (jetons: Jetons) => void = () => undefined;
    const appelerRafraichir = vi.fn(() => new Promise<Jetons>((r) => (resoudre = r)));
    const a = monterOnglet({ appelerRafraichir });

    const enCours = a.coordinateur.rafraichir();
    const verdict = expect(enCours).rejects.toBeInstanceOf(ErreurSession);
    await vi.waitFor(() => expect(appelerRafraichir).toHaveBeenCalled());
    a.coordinateur.terminer();
    resoudre({ access: "A2", refresh: "R2" });

    await verdict;
    expect(a.stockage.valeur()).toBeUndefined();
    expect(a.coordinateur.accesCourant()).toBeUndefined();
  });
});

describe("connexion dans un autre onglet", () => {
  it("un onglet invité qui partage le stockage adopte les jetons et le signale", async () => {
    const { appelerRafraichir } = fauxBackend();
    const stockage = creerStockageMemoire();
    const bus = creerBus();
    const verrous = creerVerrous();
    const a = monterOnglet({ appelerRafraichir, stockage, bus, verrous });
    const b = monterOnglet({ appelerRafraichir, stockage, bus, verrous });

    a.coordinateur.adopter({ access: "A1", refresh: "R1" });
    await Promise.resolve();

    expect(stockage.valeur()).toBe("R1");
    expect(b.coordinateur.accesCourant()).toBe("A1");
    expect(b.surJetonsExternes).toHaveBeenCalledTimes(1);
    expect(a.surJetonsExternes).not.toHaveBeenCalled();
  });
});
