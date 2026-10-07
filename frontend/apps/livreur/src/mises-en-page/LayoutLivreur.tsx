import type { ReactNode } from "react";

export function LayoutLivreur({ children }: { children: ReactNode }) {
  return (
    <div>
      <header>ANITCHE Livreur</header>
      <main>{children}</main>
    </div>
  );
}
