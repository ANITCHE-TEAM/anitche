import type { CSSProperties } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";
import { CircleCheckIcon, InfoIcon, Loader2Icon, OctagonXIcon, TriangleAlertIcon } from "lucide-react";

/** `theme` se passe en prop (« light », « dark », « system ») : ui ne gère pas le thème de l'application. */
function Toaster({ ...props }: ToasterProps) {
  return (
    <Sonner
      className="toaster group"
      icons={{
        success: <CircleCheckIcon className="size-4" />,
        info: <InfoIcon className="size-4" />,
        warning: <TriangleAlertIcon className="size-4" />,
        error: <OctagonXIcon className="size-4" />,
        loading: <Loader2Icon className="size-4 animate-spin" />,
      }}
      style={
        {
          "--normal-bg": "var(--anitche-popover)",
          "--normal-text": "var(--anitche-popover-foreground)",
          "--normal-border": "var(--anitche-border)",
          "--border-radius": "var(--anitche-radius)",
        } as CSSProperties
      }
      {...props}
    />
  );
}

export { Toaster };
