import type { ReactNode } from "react";
import { cn } from "cn";
import { Label } from "./label";

/** Attributs à poser sur le contrôle (Input, etc.) pour le relier à son libellé et à son message. */
export interface AttributsChamp {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
}

export interface ChampProps {
  /** Identifiant du contrôle ; sert aussi de base aux identifiants de l'aide et de l'erreur. */
  id: string;
  libelle: string;
  /** Message d'erreur déjà rédigé ; absent ou vide : pas d'erreur. */
  erreur?: string;
  aide?: string;
  className?: string;
  children: (attributs: AttributsChamp) => ReactNode;
}

/**
 * Libellé + contrôle + message d'erreur. Présentationnel : ne connaît ni le formulaire ni la validation.
 * Le contrôle est rendu par `children`, qui reçoit les attributs d'accessibilité à lui appliquer.
 */
export function Champ({ id, libelle, erreur, aide, className, children }: ChampProps) {
  const idAide = `${id}-aide`;
  const idErreur = `${id}-erreur`;
  const enErreur = erreur !== undefined && erreur !== "";
  const decrit = [aide !== undefined ? idAide : undefined, enErreur ? idErreur : undefined]
    .filter((valeur) => valeur !== undefined)
    .join(" ");

  return (
    <div data-slot="champ" className={cn("grid gap-1.5", className)}>
      <Label htmlFor={id}>{libelle}</Label>
      {children({
        id,
        "aria-describedby": decrit === "" ? undefined : decrit,
        "aria-invalid": enErreur ? true : undefined,
      })}
      {aide !== undefined && (
        <p id={idAide} className="text-sm text-muted-foreground">
          {aide}
        </p>
      )}
      {enErreur && (
        <p id={idErreur} className="text-sm text-destructive">
          {erreur}
        </p>
      )}
    </div>
  );
}
