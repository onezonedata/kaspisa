import { t } from "@/i18n";
import { LoginForm } from "@/components/LoginForm";
import { SourceFooter } from "@/components/SourceFooter";

export default function Page() {
  return (
    <>
      <main className="auth">
        <p className="auth-brand">{t.ui.appName}</p>
        <LoginForm />
      </main>
      <SourceFooter />
    </>
  );
}
