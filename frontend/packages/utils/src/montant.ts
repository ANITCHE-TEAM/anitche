const FORMAT_ENTIER = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
const ESPACE_INSECABLE = "\u00a0";
// Chaîne décimale de l'API : un FCFA n'a pas de centimes, la partie décimale doit être nulle.
const MONTANT_API = /^-?\d+(\.0+)?$/;

/**
 * Montant de l'API (chaîne décimale `"65000.00"`) → entier FCFA.
 * Lève `RangeError` si la valeur n'est pas un entier de FCFA (`"65000.50"`, `"abc"`, `""`, `NaN`) :
 * un échec visible vaut mieux qu'un prix arrondi. Aucun calcul n'est fait ici.
 */
export function versFcfa(valeur: string | number): number {
  const entier = typeof valeur === "string" ? analyserChaine(valeur) : valeur;
  if (!Number.isSafeInteger(entier)) {
    throw new RangeError(`Montant FCFA invalide : ${String(valeur)}`);
  }
  return entier === 0 ? 0 : entier;
}

/** Affichage d'un montant : `65 000 FCFA` (milliers en fr-FR, espace insécable avant `FCFA`). */
export function formaterFcfa(valeur: string | number): string {
  return `${FORMAT_ENTIER.format(versFcfa(valeur))}${ESPACE_INSECABLE}FCFA`;
}

function analyserChaine(valeur: string): number {
  return MONTANT_API.test(valeur) ? Number(valeur) : Number.NaN;
}
