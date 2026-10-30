const FUSEAU = "Africa/Abidjan";

const FORMAT_DATE = new Intl.DateTimeFormat("fr-FR", { timeZone: FUSEAU, dateStyle: "long" });
const FORMAT_DATE_HEURE = new Intl.DateTimeFormat("fr-FR", {
  timeZone: FUSEAU,
  dateStyle: "long",
  timeStyle: "short",
});
const FORMAT_RELATIF = new Intl.RelativeTimeFormat("fr", { numeric: "auto" });

// ISO 8601 complet avec décalage (`Z`, `+00:00`, `+0000`) : sans décalage, le navigateur lirait l'heure en local.
const ISO_AVEC_DECALAGE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/;

const SECONDE = 1000;
const MINUTE = 60 * SECONDE;
const HEURE = 60 * MINUTE;
const JOUR = 24 * HEURE;

export interface TempsRestant {
  minutes: number;
  secondes: number;
  expire: boolean;
}

/** `7 octobre 2026`, en heure d'Abidjan quel que soit le fuseau du navigateur. */
export function formaterDate(date: string): string {
  return FORMAT_DATE.format(lireDate(date));
}

/** `7 octobre 2026 à 14:30`, en heure d'Abidjan. */
export function formaterDateHeure(date: string): string {
  return FORMAT_DATE_HEURE.format(lireDate(date));
}

/** `il y a 5 minutes`, `demain`, `dans 2 heures`. `maintenant` est un paramètre : pas d'horloge cachée. */
export function formaterRelatif(date: string, maintenant: Date): string {
  const ecart = lireDate(date).getTime() - maintenant.getTime();
  const absolu = Math.abs(ecart);
  if (absolu < MINUTE) return FORMAT_RELATIF.format(Math.trunc(ecart / SECONDE), "second");
  if (absolu < HEURE) return FORMAT_RELATIF.format(Math.trunc(ecart / MINUTE), "minute");
  if (absolu < JOUR) return FORMAT_RELATIF.format(Math.trunc(ecart / HEURE), "hour");
  return FORMAT_RELATIF.format(Math.trunc(ecart / JOUR), "day");
}

/** Compte à rebours jusqu'à `echeance` ; `expire` dès que l'échéance est atteinte (minutes et secondes à 0). */
export function tempsRestant(echeance: string, maintenant: Date): TempsRestant {
  const reste = lireDate(echeance).getTime() - maintenant.getTime();
  if (reste <= 0) return { minutes: 0, secondes: 0, expire: true };
  const secondesTotal = Math.ceil(reste / SECONDE);
  return { minutes: Math.floor(secondesTotal / 60), secondes: secondesTotal % 60, expire: false };
}

function lireDate(date: string): Date {
  const lue = ISO_AVEC_DECALAGE.test(date) ? new Date(date) : new Date(Number.NaN);
  if (Number.isNaN(lue.getTime())) {
    throw new RangeError(`Date ISO 8601 avec décalage attendue : ${date}`);
  }
  return lue;
}
