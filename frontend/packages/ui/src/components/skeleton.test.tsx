import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Skeleton } from "./skeleton";

describe("Skeleton", () => {
  it("rend un bloc décoratif et fusionne className", () => {
    const { container } = render(<Skeleton className="h-4 w-20" />);
    const bloc = container.querySelector('[data-slot="skeleton"]');
    expect(bloc).not.toBeNull();
    expect(bloc).toHaveClass("animate-pulse", "h-4", "w-20");
  });
});
