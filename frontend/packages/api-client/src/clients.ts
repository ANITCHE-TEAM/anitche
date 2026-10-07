import createClient, { type Client, type Middleware } from "openapi-fetch";
import { erreurDepuisCorps, erreurReseau, type SourceApi } from "./erreurs";
import type { paths as CheminsDjango } from "./generes/django";
import type { paths as CheminsFastapi } from "./generes/fastapi";

export interface ConfigClients {
  /** Origine seule : les chemins du schéma Django contiennent déjà `/api`. */
  baseDjango: string;
  /** Origine + `/fast` : les chemins du schéma FastAPI n'ont pas ce préfixe. */
  baseFastapi: string;
  obtenirJetonAcces: () => string | undefined;
  /** Renvoie un jeton d'accès neuf, ou rejette si la session ne peut pas être prolongée. */
  rafraichir: () => Promise<string>;
  /** Appelée quand la session est perdue (rafraîchissement refusé, ou 401 après rejeu). */
  surSessionExpiree: () => void;
}

export type ClientDjango = Client<CheminsDjango>;
export type ClientFastapi = Client<CheminsFastapi>;

/** En-tête interne posé par `sansJeton()` ; le middleware le retire avant l'envoi. */
const ENTETE_SANS_JETON = "x-anitche-sans-jeton";

/** Un 401 sur ces routes veut dire « identifiants refusés » ou « jeton de rafraîchissement refusé », pas « session expirée ». */
const ROUTES_SANS_RAFRAICHISSEMENT = /\/utilisateurs\/(connexion|connexion-google|connexion\/rafraichir|deconnexion)\/$/;

/** Source de chaque réponse reçue, pour que `ok()` sache quelle API a répondu. */
const sourceDesReponses = new WeakMap<Response, SourceApi>();

/**
 * Options à passer à un appel de route publique : aucun jeton n'est envoyé.
 * Un jeton expiré sur une route publique donnerait un 401 au lieu du contenu.
 * Remplace `headers` de l'appel : à n'utiliser qu'avec `{ ...sansJeton(), params: … }`.
 */
export function sansJeton(): { headers: Record<string, string> } {
  return { headers: { [ENTETE_SANS_JETON]: "1" } };
}

/** Base absolue : `new Request("/fast/…")` est refusé hors navigateur. */
export function resoudreBase(base: string): string {
  const origine = typeof location === "undefined" ? "http://localhost" : location.origin;
  return new URL(base, origine).toString().replace(/\/+$/, "");
}

function creerMiddleware(source: SourceApi, config: ConfigClients): Middleware {
  // Copie de la requête partante, pour la rejouer : le corps d'une Request est à usage unique.
  const copies = new Map<string, Request>();

  return {
    onRequest({ request, id }) {
      if (request.headers.has(ENTETE_SANS_JETON)) {
        request.headers.delete(ENTETE_SANS_JETON);
        request.headers.delete("Authorization");
        return request;
      }
      const jeton = config.obtenirJetonAcces();
      if (jeton !== undefined) request.headers.set("Authorization", `Bearer ${jeton}`);
      if (!ROUTES_SANS_RAFRAICHISSEMENT.test(new URL(request.url).pathname)) copies.set(id, request.clone());
      return request;
    },

    async onResponse({ response, id, options }) {
      const copie = copies.get(id);
      copies.delete(id);

      if (response.status !== 401 || copie === undefined) {
        sourceDesReponses.set(response, source);
        return response;
      }

      let jeton: string;
      try {
        jeton = await config.rafraichir();
      } catch {
        config.surSessionExpiree();
        sourceDesReponses.set(response, source);
        return response;
      }

      const rejeu = new Request(copie, { headers: copie.headers });
      rejeu.headers.set("Authorization", `Bearer ${jeton}`);
      const reponseRejeu = await options.fetch(rejeu);
      if (reponseRejeu.status === 401) config.surSessionExpiree();
      sourceDesReponses.set(reponseRejeu, source);
      return reponseRejeu;
    },

    onError({ error, id }) {
      copies.delete(id);
      // Une annulation (AbortSignal de TanStack Query) n'est pas une panne réseau.
      if (error instanceof DOMException && error.name === "AbortError") return undefined;
      return erreurReseau(source, error);
    },
  };
}

/** Les deux clients typés : `django` (chemins `/api/...`) et `fastapi` (recherche, livraison, conseiller). */
export function creerClients(config: ConfigClients): { django: ClientDjango; fastapi: ClientFastapi } {
  const django = createClient<CheminsDjango>({ baseUrl: resoudreBase(config.baseDjango) });
  const fastapi = createClient<CheminsFastapi>({ baseUrl: resoudreBase(config.baseFastapi) });
  django.use(creerMiddleware("django", config));
  fastapi.use(creerMiddleware("fastapi", config));
  return { django, fastapi };
}

/**
 * Client Django sans middleware : `auth` s'en sert pour `connexion/` et `rafraichir/`,
 * qui ne doivent ni porter de jeton ni déclencher un rafraîchissement.
 */
export function creerClientNu(base: string): ClientDjango {
  return createClient<CheminsDjango>({ baseUrl: resoudreBase(base) });
}

/**
 * Convertit le résultat d'un appel en valeur, ou jette une `ErreurApi`.
 * À utiliser directement dans un `queryFn` : `queryFn: () => ok(django.GET("/api/…"))`.
 */
export async function ok<D>(appel: Promise<{ data?: D; error?: unknown; response: Response }>): Promise<D> {
  const { data, error, response } = await appel;
  if (response.ok) return data as D;
  throw erreurDepuisCorps(response.status, error, response.headers, sourceDesReponses.get(response) ?? "django");
}
