import type { Canal, MessageAuth, Portail } from "./types";

export function nomCanal(portail: Portail): string {
  return `anitche-auth-${portail}`;
}

/** Un message reçu est une donnée externe : on ne l'utilise qu'après avoir vérifié sa forme. */
export function lireMessage(donnee: unknown): MessageAuth | undefined {
  if (typeof donnee !== "object" || donnee === null) return undefined;
  const { type, access, refresh, precedent } = donnee as Record<string, unknown>;
  if (type === "deconnexion") return { type };
  if (type === "jetons" && typeof access === "string" && typeof refresh === "string") {
    return typeof precedent === "string" ? { type, access, refresh, precedent } : { type, access, refresh };
  }
  return undefined;
}

/** Canal du portail sur `BroadcastChannel`, ou `undefined` si le navigateur ne l'a pas. */
export function creerCanalNavigateur(portail: Portail): Canal | undefined {
  if (typeof BroadcastChannel === "undefined") return undefined;
  const canal = new BroadcastChannel(nomCanal(portail));
  return {
    envoyer: (message) => {
      canal.postMessage(message);
    },
    ecouter(rappel) {
      const ecouteur = (evenement: MessageEvent) => {
        const message = lireMessage(evenement.data);
        if (message !== undefined) rappel(message);
      };
      canal.addEventListener("message", ecouteur);
      return () => canal.removeEventListener("message", ecouteur);
    },
    fermer: () => {
      canal.close();
    },
  };
}
