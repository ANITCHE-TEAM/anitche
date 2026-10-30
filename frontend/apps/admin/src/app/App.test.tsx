import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("App admin", () => {
  it("affiche le layout et la page vide", () => {
    render(<App />);
    expect(screen.getByRole("banner")).toHaveTextContent("ANITCHE Admin");
    expect(screen.getByRole("main")).toHaveTextContent("Page vide");
  });
});
