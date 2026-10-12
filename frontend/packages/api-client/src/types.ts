import type { components as ComposantsDjango } from "./generes/django";
import type { components as ComposantsFastapi } from "./generes/fastapi";

export type { paths as CheminsDjango } from "./generes/django";
export type { paths as CheminsFastapi } from "./generes/fastapi";

/** Schéma nommé du backend Django : `SchemaDjango<"Commande">`. */
export type SchemaDjango<Nom extends keyof ComposantsDjango["schemas"]> = ComposantsDjango["schemas"][Nom];

/** Schéma nommé du backend FastAPI : `SchemaFastapi<"ReponseRecherche">`. */
export type SchemaFastapi<Nom extends keyof ComposantsFastapi["schemas"]> = ComposantsFastapi["schemas"][Nom];

/** Enveloppe de pagination DRF (`PageNumberPagination`). */
export interface Page<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export type Role = SchemaDjango<"RoleEnum">;
export type StatutKyc = SchemaDjango<"StatutKycEnum">;
