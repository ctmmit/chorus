import type { JobStatus } from "@/lib/api-types";

const LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  digest_ready: "Digest ready — rendering audio",
  done: "Done",
  failed: "Failed",
};

// Red is reserved for the one genuinely negative signal (failed). Every
// other state is navy/silver — status, not sentiment.
const COLOR: Record<JobStatus, string> = {
  queued: "text-silver",
  digest_ready: "text-navy",
  done: "text-navy",
  failed: "text-red",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return (
    <span className={`label-caps ${COLOR[status]}`} role="status">
      {LABELS[status]}
    </span>
  );
}
