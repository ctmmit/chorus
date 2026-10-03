import type { Metadata } from "next";

import { SubscribeWizard } from "@/components/SubscribeWizard";

export const metadata: Metadata = {
  title: "Subscribe — Chorus",
  description: "Get a podcast digest in your inbox, cut to what matters to you.",
};

export default function SubscribePage() {
  return <SubscribeWizard />;
}
