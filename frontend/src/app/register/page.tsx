import { t } from "@/i18n";
import { RegisterForm } from "@/components/RegisterForm";
import { SourceFooter } from "@/components/SourceFooter";

export default function Page() {
  return (
    <>
      <main className="auth">
        <p className="auth-brand">{t.ui.appName}</p>
        <RegisterForm />
      </main>
      <SourceFooter />
    </>
  );
}
