import { cn } from "cn";
import { Button } from "./button";

export interface EtatErreurProps {
  titre: string;
  message?: string;
  /** Présent : affiche un bouton ; le libellé est fourni par l'appelant. */
  reessayer?: { libelle: string; onClick: () => void };
  className?: string;
}

/** Zone d'erreur annoncée aux lecteurs d'écran. Aucun texte propre : tout vient des props. */
export function EtatErreur({ titre, message, reessayer, className }: EtatErreurProps) {
  return (
    <div
      role="alert"
      data-slot="etat-erreur"
      className={cn("grid justify-items-center gap-2 p-6 text-center", className)}
    >
      <p className="font-medium">{titre}</p>
      {message !== undefined && <p className="text-sm text-muted-foreground">{message}</p>}
      {reessayer !== undefined && (
        <Button variant="outline" onClick={reessayer.onClick}>
          {reessayer.libelle}
        </Button>
      )}
    </div>
  );
}
