import { vi } from "vitest";
import type { Canal, MessageAuth, StockageJetons, Verrous } from "./types";

/** Stockage en mémoire : un par « onglet » simulé (sessionStorage) ou partagé entre onglets (localStorage). */
export function creerStockageMemoire(initial?: string): StockageJetons & { valeur: () => string | undefined } {
  let refresh = initial;
  return {
    lireRefresh: () => refresh,
    ecrireRefresh: (valeur) => {
      refresh = valeur;
    },
    effacer: () => {
      refresh = undefined;
    },
    valeur: () => refresh,
  };
}

/** Web Locks simulé : exclusion mutuelle par nom, partagée entre tous les « onglets » qui reçoivent le même objet. */
export function creerVerrous(): Verrous & { prises: () => number } {
  const files = new Map<string, Promise<unknown>>();
  let prises = 0;
  return {
    request(nom, travail) {
      const precedent = files.get(nom) ?? Promise.resolve();
      const execution = precedent.then(() => {
        prises += 1;
        return travail();
      });
      files.set(
        nom,
        execution.catch(() => undefined),
      );
      return execution;
    },
    prises: () => prises,
  };
}

/** BroadcastChannel simulé : chaque canal reçoit les messages des autres, jamais les siens, en différé. */
export function creerBus() {
  const canaux = new Set<{ ecouteurs: Set<(m: MessageAuth) => void> }>();
  const messages: MessageAuth[] = [];
  return {
    messages,
    canal(): Canal {
      const moi = { ecouteurs: new Set<(m: MessageAuth) => void>() };
      canaux.add(moi);
      return {
        envoyer(message) {
          messages.push(message);
          for (const autre of canaux) {
            if (autre === moi) continue;
            for (const ecouteur of autre.ecouteurs) queueMicrotask(() => ecouteur(message));
          }
        },
        ecouter(rappel) {
          moi.ecouteurs.add(rappel);
          return () => moi.ecouteurs.delete(rappel);
        },
        fermer() {
          canaux.delete(moi);
        },
      };
    },
  };
}

/** Jeton JWT non signé dont seule la charge utile compte (`exp` en secondes depuis maintenant). */
export function fabriquerJeton(expirationDansSecondes: number, maintenantMs: number = Date.now()): string {
  const base64url = (objet: unknown) =>
    btoa(JSON.stringify(objet)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const exp = Math.floor(maintenantMs / 1000) + expirationDansSecondes;
  return `${base64url({ alg: "HS256", typ: "JWT" })}.${base64url({ exp })}.signature`;
}

export interface AppelRecu {
  methode: string;
  chemin: string;
  authorization: string | null;
  corps: unknown;
}

type Gestionnaire = (appel: AppelRecu) => Response | Promise<Response>;

export function json(statut: number, corps: unknown, entetes: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(corps), {
    status: statut,
    headers: { "Content-Type": "application/json", ...entetes },
  });
}

/**
 * Remplace `fetch` global. Clé d'un gestionnaire : `"POST /api/utilisateurs/connexion/rafraichir/"`.
 * Une route sans gestionnaire fait échouer le test.
 */
export function installerFauxReseau(gestionnaires: Record<string, Gestionnaire>) {
  const appels: AppelRecu[] = [];
  const faux = vi.fn(async (entree: Request) => {
    const url = new URL(entree.url);
    const texte = await entree.clone().text();
    const appel: AppelRecu = {
      methode: entree.method,
      chemin: url.pathname,
      authorization: entree.headers.get("Authorization"),
      corps: texte === "" ? undefined : (JSON.parse(texte) as unknown),
    };
    appels.push(appel);
    const gestionnaire = gestionnaires[`${appel.methode} ${appel.chemin}`];
    if (gestionnaire === undefined) throw new Error(`Route non prévue par le test : ${appel.methode} ${appel.chemin}`);
    return gestionnaire(appel);
  });
  vi.stubGlobal("fetch", faux);
  return {
    appels,
    compter: (cle: string) => appels.filter((a) => `${a.methode} ${a.chemin}` === cle).length,
  };
}
