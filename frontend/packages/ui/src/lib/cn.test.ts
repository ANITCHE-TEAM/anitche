import { describe, expect, it } from "vitest";
import { cn } from "./cn";

describe("cn", () => {
  it("joint les valeurs conditionnelles", () => {
    const actif = Boolean(globalThis.undefined);
    expect(cn("a", actif && "b", { c: true, d: actif }, ["e"])).toBe("a c e");
  });

  it("résout les conflits Tailwind au profit de la dernière classe", () => {
    expect(cn("px-2 py-1", "px-4")).toBe("py-1 px-4");
  });
});
