export function ProvenanceLine({
  soulVersion,
  soulOrigin,
}: {
  soulVersion: string;
  soulOrigin: string;
}) {
  return (
    <p className="label-caps">
      soul_version <span className="font-mono normal-case text-ink">{soulVersion}</span>
      {"  ·  "}
      soul_origin <span className="font-mono normal-case text-ink">{soulOrigin}</span>
    </p>
  );
}
