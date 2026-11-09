"use client";

import { CompanyProfileForm } from "@/components/CompanyProfileForm";
import { Card, PageHeader } from "@/components/ui";

export default function CompanySettingsPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        title="Company profile"
        subtitle="Saved once and reused automatically in every email and interview invitation."
      />
      <Card className="max-w-xl p-5">
        <CompanyProfileForm />
      </Card>
    </div>
  );
}