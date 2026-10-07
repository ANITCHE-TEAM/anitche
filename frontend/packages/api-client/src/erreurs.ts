export type SourceApi = "django" | "fastapi";

const DETAIL_GENERIQUE = "Une erreur est survenue. Réessayez dans un instant.";
const DETAIL_RESEAU = "Connexion impossible : vérifiez votre réseau.";

interface OptionsErreur {
  detail?: string;
  champs?: Record<string, string[]>;
  code?: string;
  retryAfter?: number;
  cause?: unknown;
}

/** Erreur unique du frontend : réseau (`statut` 0), réponse HTTP d'erreur Django ou FastAPI. */
export class ErreurApi extends Error {
  /** 0 = réseau coupé, sinon le code HTTP. */
  readonly statut: number;
  readonly source: SourceApi;
  /** Message principal, affichable tel quel. */
  readonly detail: string;
  /** `errors` de l'enveloppe sans la clé `code` : champ (clé à points) → messages. */
  readonly champs: Record<string, string[]>;
  /** `errors.code[0]` : identifiant machine, à préférer au texte. */
  readonly code?: string;
  /** Secondes à attendre avant de réessayer (en-tête `Retry-After`). */
  readonly retryAfter?: number;

  constructor(statut: number, source: SourceApi, options: OptionsErreur = {}) {
    const detail = options.detail ?? (statut === 0 ? DETAIL_RESEAU : DETAIL_GENERIQUE);
    super(detail, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "ErreurApi";
    this.statut = statut;
    this.source = source;
    this.detail = detail;
    this.champs = options.champs ?? {};
    this.code = options.code;
    this.retryAfter = options.retryAfter;
  }
}

export function estErreurApi(valeur: unknown): valeur is ErreurApi {
  return valeur instanceof ErreurApi;
}

/** Erreur de transport (DNS, connexion refusée, hors ligne). */
export function erreurReseau(source: SourceApi, cause?: unknown): ErreurApi {
  return new ErreurApi(0, source, { cause });
}

/**
 * `Retry-After` en secondes : entier positif, ou date HTTP (convertie en écart avec `maintenant`).
 * Valeur absente ou illisible : `undefined`.
 */
export function lireRetryAfter(valeur: string | null, maintenant: number = Date.now()): number | undefined {
  if (valeur === null) return undefined;
  const texte = valeur.trim();
  if (/^\d+$/.test(texte)) return Number(texte);
  const date = Date.parse(texte);
  if (Number.isNaN(date)) return undefined;
  return Math.max(0, Math.ceil((date - maintenant) / 1000));
}

function estObjet(valeur: unknown): valeur is Record<string, unknown> {
  return typeof valeur === "object" && valeur !== null && !Array.isArray(valeur);
}

/** `errors` d'une enveloppe → clé → messages (les valeurs qui ne sont pas des listes de chaînes sont ignorées). */
function lireChamps(errors: unknown): { champs: Record<string, string[]>; code?: string } {
  const champs: Record<string, string[]> = {};
  let code: string | undefined;
  if (!estObjet(errors)) return { champs };
  for (const [cle, valeur] of Object.entries(errors)) {
    if (!Array.isArray(valeur)) continue;
    const messages = valeur.filter((element): element is string => typeof element === "string");
    if (cle === "code") code = messages[0];
    else if (messages.length > 0) champs[cle] = messages;
  }
  return { champs, code };
}

/**
 * Construit l'erreur depuis une réponse non-OK dont le corps est déjà lu
 * (`openapi-fetch` le lit : JSON si possible, sinon texte).
 * Un corps qui n'est pas l'enveloppe `{ detail, errors }` (page HTML d'un proxy) donne un `detail` générique.
 */
export function erreurDepuisCorps(statut: number, corps: unknown, entetes: Headers, source: SourceApi): ErreurApi {
  const retryAfter = lireRetryAfter(entetes.get("Retry-After"));
  if (!estObjet(corps)) return new ErreurApi(statut, source, { retryAfter });
  const { champs, code } = lireChamps(corps["errors"]);
  const detail = typeof corps["detail"] === "string" && corps["detail"] !== "" ? corps["detail"] : undefined;
  return new ErreurApi(statut, source, { detail, champs, code, retryAfter });
}

/** Comme `erreurDepuisCorps`, en lisant le corps de la réponse (jamais interprété s'il n'est pas du JSON). */
export async function erreurDepuisReponse(response: Response, source: SourceApi): Promise<ErreurApi> {
  let corps: unknown;
  try {
    corps = JSON.parse(await response.text());
  } catch {
    corps = undefined;
  }
  return erreurDepuisCorps(response.status, corps, response.headers, source);
}

/** Premier message de chaque champ. */
export function erreursParChamp(erreur: ErreurApi): Record<string, string> {
  const resultat: Record<string, string> = {};
  for (const [champ, messages] of Object.entries(erreur.champs)) {
    const premier = messages[0];
    if (premier !== undefined) resultat[champ] = premier;
  }
  return resultat;
}

/** Forme minimale de React Hook Form : aucune dépendance au paquet. */
export interface FormulaireAvecErreurs {
  setError(nom: string, erreur: { message: string }): void;
}

/**
 * Pose les erreurs de champ sur le formulaire (les clés à points sont des chemins RHF).
 * `non_field_errors` est posé sous `root`. Retourne `false` si rien n'a été posé :
 * le module affiche alors `erreur.detail`.
 */
export function appliquerErreursAuFormulaire(form: FormulaireAvecErreurs, erreur: ErreurApi): boolean {
  let pose = false;
  for (const [champ, message] of Object.entries(erreursParChamp(erreur))) {
    form.setError(champ === "non_field_errors" ? "root" : champ, { message });
    pose = true;
  }
  return pose;
}
