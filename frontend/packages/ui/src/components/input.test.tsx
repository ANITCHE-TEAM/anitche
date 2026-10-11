import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Input } from "./input";

describe("Input", () => {
  it("transmet type, placeholder et la saisie", async () => {
    render(<Input type="email" placeholder="Courriel" />);
    const champ = screen.getByPlaceholderText("Courriel");
    expect(champ).toHaveAttribute("type", "email");
    await userEvent.type(champ, "a@b.ci");
    expect(champ).toHaveValue("a@b.ci");
  });

  it("désactivé : refuse la saisie", async () => {
    render(<Input disabled placeholder="Courriel" />);
    const champ = screen.getByPlaceholderText("Courriel");
    await userEvent.type(champ, "x");
    expect(champ).toBeDisabled();
    expect(champ).toHaveValue("");
  });
});
