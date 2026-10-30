import type { ReactNode } from "react";

export function LayoutAdmin({ children }: { children: ReactNode }) {
  return (
    <div>
      <header>ANITCHE Admin</header>
      <main>{children}</main>
    </div>
  );
}
