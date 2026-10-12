import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("App livreur", () => {
  it("affiche le layout et la page vide", () => {
    render(<App />);
    expect(screen.getByRole("banner")).toHaveTextContent("ANITCHE Livreur");
    expect(screen.getByRole("main")).toHaveTextContent("Page vide");
  });
});
