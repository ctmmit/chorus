import { PersonaView } from "@/components/PersonaView";

export default async function PersonaPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <PersonaView personaId={id} />;
}
