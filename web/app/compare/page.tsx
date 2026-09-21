import { CompareView } from "@/components/CompareView";

export default async function ComparePage({
  searchParams,
}: {
  searchParams: Promise<{ a?: string; b?: string }>;
}) {
  const { a, b } = await searchParams;
  return <CompareView jobIdA={a ?? null} jobIdB={b ?? null} />;
}
