import { act, render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { describe, expect, it } from "vitest";
import { Toaster } from "./sonner";

describe("Toaster", () => {
  it("affiche un message émis avec toast()", async () => {
    render(<Toaster />);
    act(() => {
      toast("Enregistré");
    });
    expect(await screen.findByText("Enregistré")).toBeInTheDocument();
  });
});
