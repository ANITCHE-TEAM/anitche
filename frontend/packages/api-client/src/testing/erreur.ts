export interface OptionsErreurTest {
  detail?: string;
  /** Champ (clé à points) → messages ; `code` pour l'identifiant machine. */
  errors?: Record<string, string[]>;
  /** Secondes, ou date HTTP, posé dans l'en-tête `Retry-After`. */
  retryAfter?: number | string;
}

/** Réponse d'erreur à la forme exacte du backend : `{ success: false, status_code, detail, errors }`. */
export function erreur(statut: number, options: OptionsErreurTest = {}): Response {
  const entetes = new Headers({ "Content-Type": "application/json" });
  if (options.retryAfter !== undefined) entetes.set("Retry-After", String(options.retryAfter));
  const corps = {
    success: false,
    status_code: statut,
    detail: options.detail ?? "Erreur de test.",
    errors: options.errors ?? {},
  };
  return new Response(JSON.stringify(corps), { status: statut, headers: entetes });
}
