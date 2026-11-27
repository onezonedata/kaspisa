import type { ButtonHTMLAttributes, ReactNode } from "react";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  busy: boolean;
  busyText: string;
  children: ReactNode;
}

/** Кнопка отправки: на время запроса заблокирована и показывает текст ожидания. */
export function BusyButton({ busy, busyText, children, disabled, ...rest }: Props) {
  return (
    <button {...rest} disabled={busy || disabled}>
      {busy ? busyText : children}
    </button>
  );
}
