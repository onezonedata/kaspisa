import { t } from "@/i18n";
import { ResetPasswordForm } from "@/components/ResetPasswordForm";
import { SourceFooter } from "@/components/SourceFooter";

export default function Page() {
  return (
    <>
      <main className="auth">
        <p className="auth-brand">{t.ui.appName}</p>
        <ResetPasswordForm />
      </main>
      <SourceFooter />
    </>
  );
}
