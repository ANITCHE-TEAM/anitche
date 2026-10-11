import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Label } from "./label";

describe("Label", () => {
  it("nomme le contrôle visé par htmlFor", () => {
    render(
      <>
        <Label htmlFor="mail">Courriel</Label>
        <input id="mail" />
      </>,
    );
    expect(screen.getByLabelText("Courriel")).toHaveAttribute("id", "mail");
  });
});
