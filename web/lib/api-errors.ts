/** Turns whatever a failed API call threw into a sentence a person can act
 * on, and says whether the cause was a rejected key. */
import { ApiError } from "./api-client";
import { isAuthStatus } from "./subscribe";

export interface DescribedError {
  message: string;
  /** True for 401/403: the stored key is not (or no longer) accepted. */
  auth: boolean;
}

export const AUTH_REJECTED_MESSAGE = "Your key was not accepted. Request a new one or paste a different key.";
export const NETWORK_ERROR_MESSAGE =
  "Could not reach the Chorus API. Check your connection, then try again.";

export function describeApiError(err: unknown, fallback: string): DescribedError {
  if (err instanceof ApiError) {
    if (isAuthStatus(err.status)) return { message: AUTH_REJECTED_MESSAGE, auth: true };
    return { message: err.message || fallback, auth: false };
  }
  // fetch() rejects with a TypeError when the network or CORS blocks the call.
  if (err instanceof TypeError) return { message: NETWORK_ERROR_MESSAGE, auth: false };
  return { message: fallback, auth: false };
}

/** True when an in-flight request was cancelled on purpose (a newer search
 * superseded it) and should be ignored rather than reported. */
export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
