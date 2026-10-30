import { useEffect, useEffectEvent, useState } from "react";

export interface MessageLimiteProps {
  /** Durée d'attente à compter, par exemple l'en-tête `Retry-After`. Un changement relance le compte. */
  secondes: number;
  /** Texte affiché pour `restant` secondes (le libellé appartient à l'appelant). */
  message: (restant: number) => string;
  /** Appelée une fois, quand le compte atteint 0. Pas appelée si `secondes` vaut déjà 0 au départ. */
  surFin?: () => void;
}

/** Compte à rebours d'un blocage temporaire (limitation de débit). Piloté uniquement par ses props. */
export function MessageLimite({ secondes, message, surFin }: MessageLimiteProps) {
  // `key` : un nouveau délai repart de zéro sans effet de synchronisation.
  return <Decompte key={secondes} secondes={secondes} message={message} surFin={surFin} />;
}

function Decompte({ secondes, message, surFin }: MessageLimiteProps) {
  const [ecoule, setEcoule] = useState(0);
  const restant = Math.max(0, Math.ceil(secondes) - ecoule);
  const signalerFin = useEffectEvent(() => surFin?.());

  useEffect(() => {
    if (restant === 0) return;
    const minuteur = setTimeout(() => {
      setEcoule((valeur) => valeur + 1);
      if (restant === 1) signalerFin();
    }, 1000);
    return () => clearTimeout(minuteur);
  }, [restant]);

  return (
    <p role="status" aria-live="polite" data-slot="message-limite" className="text-sm text-muted-foreground">
      {message(restant)}
    </p>
  );
}
