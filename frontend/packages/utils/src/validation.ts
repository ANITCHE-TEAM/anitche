import { z } from "zod";
import { versFcfa } from "./montant";
import { normaliserTelephone } from "./telephone";

const MESSAGE_TELEPHONE = "Numéro ivoirien à 10 chiffres attendu (ex. 07 07 07 07 07)";

/** Téléphone ivoirien saisi librement → E.164 (`+2250707070707`). */
export const telephoneCI = z.string({ error: MESSAGE_TELEPHONE }).transform((saisie, ctx) => {
  try {
    return normaliserTelephone(saisie);
  } catch {
    ctx.addIssue({ code: "custom", message: MESSAGE_TELEPHONE });
    return z.NEVER;
  }
});

export const email = z.email({ error: "Adresse e-mail invalide" });

/** Montant FCFA entier strictement positif, en nombre ou en chaîne (`15000`, `"15000"`) → entier. */
export const montantEntier = z.union([z.string(), z.number()], { error: "Montant entier en FCFA, 1 ou plus" }).transform((valeur, ctx) => {
  try {
    const montant = versFcfa(valeur);
    if (montant >= 1) return montant;
  } catch {
    // message commun ci-dessous
  }
  ctx.addIssue({ code: "custom", message: "Montant entier en FCFA, 1 ou plus" });
  return z.NEVER;
});
