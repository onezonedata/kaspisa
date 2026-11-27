import type { Metadata } from "next";
import { t } from "@/i18n";
import { PREFS_SCRIPT } from "@/lib/prefs";
import "./fonts.css";
import "./tokens.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "kaspisa",
  description: t.meta.description,
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // suppressHydrationWarning: встроенный скрипт ставит data-theme и data-sidebar до гидратации.
    <html lang="ru" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: PREFS_SCRIPT }} />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
