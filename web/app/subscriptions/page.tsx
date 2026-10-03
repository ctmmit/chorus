import type { Metadata } from "next";

import { SubscriptionsView } from "@/components/SubscriptionsView";

export const metadata: Metadata = {
  title: "Subscriptions — Chorus",
  description: "Manage your Chorus podcast digests.",
};

export default function SubscriptionsPage() {
  return <SubscriptionsView />;
}
