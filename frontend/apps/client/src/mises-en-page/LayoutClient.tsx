import type { ReactNode } from "react";

export function LayoutClient({ children }: { children: ReactNode }) {
  return (
    <div>
      <header>ANITCHE Client</header>
      <main>{children}</main>
    </div>
  );
}
