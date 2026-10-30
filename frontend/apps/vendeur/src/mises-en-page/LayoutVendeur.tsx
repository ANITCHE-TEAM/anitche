import type { ReactNode } from "react";

export function LayoutVendeur({ children }: { children: ReactNode }) {
  return (
    <div>
      <header>ANITCHE Vendeur</header>
      <main>{children}</main>
    </div>
  );
}
