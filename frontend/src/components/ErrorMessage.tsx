import type { ApiError } from "@/api/client";
import { errorText } from "@/i18n";

export function ErrorMessage({ error }: { error: ApiError | null }) {
  if (error === null) return null;
  return (
    <p role="alert" className="error">
      {errorText(error)}
    </p>
  );
}
