import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { t } from "@/i18n";
import { ErrorMessage } from "./ErrorMessage";

describe("ErrorMessage", () => {
  it("без ошибки ничего не показывает", () => {
    const { container } = render(<ErrorMessage error={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("показывает текст ошибки с role=alert", () => {
    render(<ErrorMessage error={new ApiError(404, "not_found")} />);
    expect(screen.getByRole("alert")).toHaveTextContent(t.errors.not_found);
  });
});
