import { estErreurApi } from "@anitche/api-client";
import { ErreurSession, type Canal, type Jetons, type MessageAuth, type StockageJetons, type Verrous } from "./types";

export interface DepsCoordinateur {
  stockage: StockageJetons;
  /** Absent : pas de synchronisation entre onglets. */
  canal?: Canal | undefined;
  /** Absent (navigateur sans Web Locks) : un seul rafraîchissement à la fois par onglet, pas entre onglets. */
  verrous?: Verrous | undefined;
  nomVerrou: string;
  /** Appelle `rafraichir/` avec ce refresh. Rejette une `ErreurApi` (ou une erreur de transport). */
  appelerRafraichir: (refresh: string) => Promise<Jetons>;
  /** La session ne peut plus être prolongée (refresh refusé, ou déconnexion venue d'un autre onglet). */
  surSessionPerdue: () => void;
  /** Un autre onglet a ouvert ou prolongé la session : ses jetons sont adoptés. */
  surJetonsExternes: () => void;
}

export interface Coordinateur {
  /** Jeton d'accès en mémoire (jamais persisté). */
  accesCourant(): string | undefined;
  /** Un jeton d'accès neuf. Une seule requête réseau, quel que soit le nombre d'appelants et d'onglets. */
  rafraichir(): Promise<string>;
  /** Enregistre la paire obtenue par une connexion et la diffuse. */
  adopter(jetons: Jetons): void;
  /** Oublie la session localement et prévient les autres onglets. */
  terminer(): void;
  /** Vrai si le dernier rafraîchissement a échoué sur une panne (réseau, 5xx, 429) et non sur un refus. */
  echecTransitoire(): boolean;
  fermer(): void;
}

/** `rafraichir/` documente 400 et 401 : le refresh est inconnu, expiré, déjà utilisé ou révoqué. */
function estRefus(erreur: unknown): boolean {
  return estErreurApi(erreur) && (erreur.statut === 401 || erreur.statut === 400);
}

/**
 * Coordonne le rafraîchissement du portail. Le refresh est à usage unique (rotation + liste noire) :
 * deux appels avec le même refresh déconnecteraient l'utilisateur. D'où :
 * - une promesse partagée dans l'onglet ;
 * - un verrou `navigator.locks` entre onglets, pris avant de relire le refresh stocké ;
 * - une diffusion des nouveaux jetons, pour que l'onglet suivant n'ait aucun appel à faire.
 */
export function creerCoordinateur(deps: DepsCoordinateur): Coordinateur {
  const { stockage, canal } = deps;
  let access: string | undefined;
  let enCours: Promise<string> | undefined;
  let transitoire = false;
  /** Incrémenté à chaque fin de session : un rafraîchissement lancé avant ne doit rien écrire après. */
  let generation = 0;
  /** Nombre de jetons reçus d'autres onglets : s'il change pendant l'attente du verrou, l'accès reçu est neuf. */
  let adoptions = 0;

  function oublier(): void {
    generation += 1;
    stockage.effacer();
    access = undefined;
  }

  function recevoir(message: MessageAuth): void {
    if (message.type === "deconnexion") {
      oublier();
      deps.surSessionPerdue();
      return;
    }
    const stocke = stockage.lireRefresh();
    // localStorage : l'autre onglet a déjà écrit `refresh`. sessionStorage : seul un onglet qui tenait
    // le refresh consommé (`precedent`) est le même parcours ; un autre onglet n'a pas à recevoir cette session.
    const memeSession = stocke === message.refresh || (message.precedent !== undefined && stocke === message.precedent);
    if (!memeSession) return;
    if (stocke !== message.refresh) stockage.ecrireRefresh(message.refresh);
    access = message.access;
    adoptions += 1;
    deps.surJetonsExternes();
  }

  const arreterEcoute = canal?.ecouter(recevoir);

  async function executer(): Promise<string> {
    if (stockage.lireRefresh() === undefined) throw new ErreurSession();
    const adoptionsAuDebut = adoptions;
    const generationAuDebut = generation;

    const travail = async (): Promise<string> => {
      if (generation !== generationAuDebut) throw new ErreurSession();
      const refresh = stockage.lireRefresh();
      if (refresh === undefined) throw new ErreurSession();
      // Un autre onglet a tourné le refresh pendant notre attente : son accès est déjà là.
      if (adoptions !== adoptionsAuDebut && access !== undefined) return access;

      let jetons: Jetons;
      try {
        jetons = await deps.appelerRafraichir(refresh);
      } catch (erreur) {
        if (generation !== generationAuDebut) throw new ErreurSession();
        if (estRefus(erreur)) {
          transitoire = false;
          oublier();
          canal?.envoyer({ type: "deconnexion" });
          deps.surSessionPerdue();
          throw new ErreurSession("Session expirée.", { cause: erreur });
        }
        transitoire = true;
        throw erreur;
      }

      transitoire = false;
      if (generation !== generationAuDebut) throw new ErreurSession();
      stockage.ecrireRefresh(jetons.refresh);
      access = jetons.access;
      canal?.envoyer({ type: "jetons", access: jetons.access, refresh: jetons.refresh, precedent: refresh });
      return jetons.access;
    };

    return deps.verrous === undefined ? travail() : deps.verrous.request(deps.nomVerrou, travail);
  }

  return {
    accesCourant: () => access,

    rafraichir() {
      enCours ??= executer().finally(() => {
        enCours = undefined;
      });
      return enCours;
    },

    adopter(jetons) {
      generation += 1;
      transitoire = false;
      stockage.ecrireRefresh(jetons.refresh);
      access = jetons.access;
      canal?.envoyer({ type: "jetons", access: jetons.access, refresh: jetons.refresh });
    },

    terminer() {
      oublier();
      canal?.envoyer({ type: "deconnexion" });
    },

    echecTransitoire: () => transitoire,

    fermer() {
      arreterEcoute?.();
      canal?.fermer();
    },
  };
}
