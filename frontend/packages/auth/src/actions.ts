import {
  creerClientNu,
  creerClients,
  erreurReseau,
  estErreurApi,
  ok,
  type ClientDjango,
} from "@anitche/api-client";
import { creerCanalNavigateur, nomCanal } from "./canal";
import { expireBientot } from "./jwt";
import { creerCoordinateur } from "./rafraichissement";
import { creerStoreSession, marquerConnecte, marquerInvite, type StoreSession } from "./session";
import { creerStockageParDefaut } from "./stockage";
import type { Canal, Portail, Profil, StockageJetons, Verrous } from "./types";

export interface ConfigAuth {
  portail: Portail;
  /** Origine seule (les chemins du schéma contiennent déjà `/api`). */
  baseDjango: string;
  /** Origine + `/fast`. */
  baseFastapi: string;
  /** Par défaut : `sessionStorage` pour l'admin, `localStorage` sinon. */
  stockage?: StockageJetons;
  /** Par défaut : `BroadcastChannel` du portail s'il existe. */
  canal?: Canal | undefined;
  /** Par défaut : `navigator.locks` s'il existe. */
  verrous?: Verrous | undefined;
  maintenant?: () => number;
  /** Appelée à chaque fin de session (déconnexion, session expirée, déconnexion d'un autre onglet) : y vider le cache TanStack Query. */
  surFinDeSession?: () => void;
}

export type Auth = ReturnType<typeof creerAuth>;

/** Un échec de transport hors du client à middleware (`creerClientNu` ne convertit rien) devient une `ErreurApi` de statut 0. */
async function appel<D>(promesse: Promise<{ data?: D; error?: unknown; response: Response }>): Promise<D> {
  try {
    return await ok(promesse);
  } catch (erreur) {
    if (estErreurApi(erreur)) throw erreur;
    throw erreurReseau("django", erreur);
  }
}

/**
 * Branche tout ce qu'un portail attend d'`auth` : stockage, coordinateur de rafraîchissement,
 * session, et les deux clients HTTP (déjà reliés au jeton, au rafraîchissement et à la fin de session).
 */
export function creerAuth(config: ConfigAuth) {
  const { portail } = config;
  const maintenant = config.maintenant ?? Date.now;
  const stockage = config.stockage ?? creerStockageParDefaut(portail);
  const canal = "canal" in config ? config.canal : creerCanalNavigateur(portail);
  const verrous = "verrous" in config ? config.verrous : (globalThis.navigator?.locks as Verrous | undefined);
  const session: StoreSession = creerStoreSession();
  const nu: ClientDjango = creerClientNu(config.baseDjango);

  function finDeSession(): void {
    marquerInvite(session);
    config.surFinDeSession?.();
  }

  const coordinateur = creerCoordinateur({
    stockage,
    canal,
    verrous,
    nomVerrou: nomCanal(portail),
    appelerRafraichir: (refresh) => appel(nu.POST("/api/utilisateurs/connexion/rafraichir/", { body: { refresh } })),
    surSessionPerdue: finDeSession,
    surJetonsExternes: () => {
      if (session.getState().etat !== "connecte") void chargerProfil().catch(() => undefined);
    },
  });

  const clients = creerClients({
    baseDjango: config.baseDjango,
    baseFastapi: config.baseFastapi,
    obtenirJetonAcces: () => coordinateur.accesCourant(),
    rafraichir: () => coordinateur.rafraichir(),
    surSessionExpiree: () => {
      if (session.getState().etat === "invite" || coordinateur.echecTransitoire()) return;
      coordinateur.terminer();
      finDeSession();
    },
  });

  async function chargerProfil(): Promise<Profil> {
    const profil = await appel(clients.django.GET("/api/utilisateurs/profil/"));
    marquerConnecte(session, profil);
    return profil;
  }

  async function connecter(email: string, motDePasse: string): Promise<Profil> {
    // Client nu : un jeton d'accès expiré envoyé à connexion/ y donnerait un 401 « session expirée ».
    const jetons = await appel(nu.POST("/api/utilisateurs/connexion/", { body: { email, password: motDePasse } }));
    coordinateur.adopter(jetons);
    try {
      return await chargerProfil();
    } catch (erreur) {
      await deconnecter();
      throw erreur;
    }
  }

  /** L'effacement local passe avant le réseau : un serveur injoignable ne doit pas laisser l'utilisateur connecté. */
  async function deconnecter(): Promise<void> {
    const refresh = stockage.lireRefresh();
    coordinateur.terminer();
    finDeSession();
    if (refresh === undefined) return;
    try {
      await appel(nu.POST("/api/utilisateurs/deconnexion/", { body: { refresh } }));
    } catch {
      // Le refresh restera valable côté serveur jusqu'à son expiration ; la session locale est déjà détruite.
    }
  }

  /** Une seule exécution à la fois : `<StrictMode>` monte les effets deux fois. */
  let demarrage: Promise<void> | undefined;

  function demarrer(): Promise<void> {
    demarrage ??= (async () => {
      if (stockage.lireRefresh() === undefined) {
        marquerInvite(session);
        return;
      }
      await coordinateur.rafraichir();
      await chargerProfil();
    })().finally(() => {
      demarrage = undefined;
    });
    return demarrage;
  }

  async function obtenirJetonValide(): Promise<string> {
    const access = coordinateur.accesCourant();
    if (access !== undefined && !expireBientot(access, maintenant())) return access;
    return coordinateur.rafraichir();
  }

  return {
    session,
    clients,
    connecter,
    deconnecter,
    chargerProfil,
    demarrer,
    obtenirJetonValide,
    fermer: () => {
      coordinateur.fermer();
    },
  };
}

