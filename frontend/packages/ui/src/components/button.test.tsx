import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button } from "./button";

describe("Button", () => {
  it("expose le rôle button et déclenche onClick", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Valider</Button>);
    await userEvent.click(screen.getByRole("button", { name: "Valider" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("désactivé : ne déclenche pas onClick", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Valider
      </Button>,
    );
    const bouton = screen.getByRole("button", { name: "Valider" });
    expect(bouton).toBeDisabled();
    await userEvent.click(bouton);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("applique la variante et fusionne className sans doublon de classes en conflit", () => {
    render(
      <Button variant="destructive" className="h-12">
        Supprimer
      </Button>,
    );
    const bouton = screen.getByRole("button", { name: "Supprimer" });
    expect(bouton).toHaveAttribute("data-variant", "destructive");
    expect(bouton).toHaveClass("h-12");
    expect(bouton).not.toHaveClass("h-8");
  });

  it("asChild : rend l'enfant (un lien) à la place d'un button", () => {
    render(
      <Button asChild>
        <a href="/panier">Panier</a>
      </Button>,
    );
    expect(screen.getByRole("link", { name: "Panier" })).toHaveAttribute("href", "/panier");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
