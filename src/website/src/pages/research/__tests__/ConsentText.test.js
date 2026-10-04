import React from "react";
import { render, screen } from "@testing-library/react";
import ConsentText, { ConsentReview } from "../ConsentText";

test("keeps line breaks and turns only web and mail links into anchors", () => {
  const { container } = render(
    <ConsentText text={"Line one\nVisit https://example.org/a. Mail mailto:team@example.org\njavascript:alert(1)"} />,
  );
  expect(container.firstChild).toHaveClass("consent-text");
  expect(container.textContent).toContain("Line one\nVisit");
  const links = screen.getAllByRole("link");
  expect(links.map((link) => link.getAttribute("href"))).toEqual(["https://example.org/a", "mailto:team@example.org"]);
  links.forEach((link) => expect(link).toHaveAttribute("rel", "noopener noreferrer"));
  // Not a link: the scheme is not allowed.
  expect(container.textContent).toContain("javascript:alert(1)");
});

test("never interprets the document as HTML", () => {
  const { container } = render(<ConsentText text={'<img src=x onerror="alert(1)"><script>alert(2)</script>'} />);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain("<script>alert(2)</script>");
});

const STATEMENTS = {
  document: null,
  notice: "Notice",
  statements: [
    { id: "a", text: "Required one", required: true },
    { id: "b", text: "Optional one", required: false },
  ],
};

test("a read-only record lists the answers without form controls", () => {
  render(<ConsentReview consent={STATEMENTS} answers={{ a: true, b: false }} />);
  expect(screen.queryByRole("checkbox")).toBeNull();
  const items = screen.getAllByRole("listitem");
  expect(items[0]).toHaveTextContent("Required one (required)");
  expect(items[0].querySelector("title").textContent).toBe("Ticked");
  expect(items[1]).toHaveTextContent("Optional one (optional)");
  expect(items[1].querySelector("title").textContent).toBe("Not ticked");
});

test("an owner preview lists the statements without ticks", () => {
  render(<ConsentReview consent={STATEMENTS} />);
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.getAllByRole("listitem").map((item) => item.querySelector("title"))).toEqual([null, null]);
});
