// Numéro ivoirien : 10 chiffres commençant par 0, avec ou sans l'indicatif 225.
const NATIONAL = /^0\d{9}$/;
const E164_CI = /^\+225(0\d{9})$/;

/**
 * Saisie → E.164 ivoirien `+2250707070707`.
 * Accepte `07…`, `+225 07…`, `00225…`, `225…` ; espaces, points et tirets sont ignorés.
 * Lève `RangeError` si ce n'est pas un numéro ivoirien de 10 chiffres.
 */
export function normaliserTelephone(saisie: string): string {
  const compact = saisie.replace(/[\s.-]/g, "");
  const chiffres = compact.startsWith("+") ? compact.slice(1) : compact;
  const national = /^\d+$/.test(chiffres) ? nationalDepuis(chiffres) : null;
  if (national === null) {
    throw new RangeError(`Numéro ivoirien invalide : ${saisie}`);
  }
  return `+225${national}`;
}

/** E.164 ivoirien → affichage `07 07 07 07 07`. Lève `RangeError` si l'entrée n'est pas un E.164 ivoirien. */
export function formaterTelephone(e164: string): string {
  const national = E164_CI.exec(e164)?.[1];
  if (national === undefined) {
    throw new RangeError(`E.164 ivoirien attendu : ${e164}`);
  }
  return national.replace(/(\d{2})(?=\d)/g, "$1 ");
}

export function estTelephoneCI(saisie: string): boolean {
  try {
    normaliserTelephone(saisie);
    return true;
  } catch {
    return false;
  }
}

function nationalDepuis(chiffres: string): string | null {
  const sansIndicatif = chiffres.startsWith("00225")
    ? chiffres.slice(5)
    : chiffres.startsWith("225")
      ? chiffres.slice(3)
      : chiffres;
  return NATIONAL.test(sansIndicatif) ? sansIndicatif : null;
}
