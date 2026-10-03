"use client";

import { useId, useState, type FormEvent } from "react";

import { listSubscriptions, requestKey } from "@/lib/api-client";
import { describeApiError } from "@/lib/api-errors";
import { CHORUS_PROXY_ENABLED, MOCK_MODE } from "@/lib/config";
import { isValidEmail, maskToken } from "@/lib/subscribe";

import {
  ERROR_TEXT_CLASS,
  FIELD_LABEL_CLASS,
  HELP_TEXT_CLASS,
  INPUT_CLASS,
  PRIMARY_BUTTON_CLASS,
  SECONDARY_BUTTON_CLASS,
  TEXT_BUTTON_CLASS,
} from "./ui";

/** Demo key offered in mock mode, where no email is sent and any text works. */
const MOCK_DEMO_KEY = "demo";

/**
 * Step 1: who you are. Three ways in: the sign-in link from the key email
 * (handled by the wizard before this renders), an emailed key pasted here,
 * or a key you already have. With a stored key it just confirms and offers to
 * continue.
 */
export function SignInStep({
  baseUrl,
  onBaseUrlChange,
  token,
  email,
  onEmailChange,
  notice,
  signedInByLink,
  onToken,
  onSignOut,
  onContinue,
}: {
  baseUrl: string;
  onBaseUrlChange: (next: string) => void;
  token: string;
  email: string;
  onEmailChange: (next: string) => void;
  /** A message to show first, e.g. "Your key was not accepted". */
  notice: string | null;
  signedInByLink: boolean;
  onToken: (token: string) => void;
  onSignOut: () => void;
  onContinue: () => void;
}) {
  const uid = useId();
  const [sending, setSending] = useState(false);
  const [keySent, setKeySent] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [pasteValue, setPasteValue] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [pasteError, setPasteError] = useState<string | null>(null);
  // Decided once, on the first render after hydration, so the field does not
  // vanish while someone is typing the address.
  const [apiAddressOpen] = useState(() => baseUrl.trim() === "");

  const needsApiAddressField = !MOCK_MODE && !CHORUS_PROXY_ENABLED;
  const signedIn = token.trim().length > 0;

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!isValidEmail(email)) {
      setSendError("Enter a valid email address.");
      return;
    }
    setSending(true);
    setSendError(null);
    try {
      await requestKey(baseUrl, email.trim());
      setKeySent(true);
    } catch (err) {
      setSendError(describeApiError(err, "Could not send a key. Try again.").message);
    } finally {
      setSending(false);
    }
  }

  async function verifyAndUse(candidate: string) {
    const trimmed = candidate.trim();
    if (!trimmed) {
      setPasteError("Paste your key first.");
      return;
    }
    setVerifying(true);
    setPasteError(null);
    try {
      // Any authenticated read proves the key works before we keep it.
      await listSubscriptions(baseUrl, trimmed);
      setPasteValue("");
      onToken(trimmed);
    } catch (err) {
      setPasteError(describeApiError(err, "Could not check that key. Try again.").message);
    } finally {
      setVerifying(false);
    }
  }

  function handlePaste(e: FormEvent) {
    e.preventDefault();
    void verifyAndUse(pasteValue);
  }

  const emailId = `${uid}-email`;
  const keyId = `${uid}-key`;
  const apiId = `${uid}-api`;

  if (signedIn) {
    return (
      <div className="space-y-6">
        {notice ? (
          <p role="status" className={HELP_TEXT_CLASS}>
            {notice}
          </p>
        ) : null}
        <p className="max-w-xl font-serif text-[15px] leading-relaxed text-ink">
          {signedInByLink ? "You are signed in with the link from your email." : "You are signed in."}{" "}
          <span className={HELP_TEXT_CLASS}>
            Key ending <span className="font-mono">{maskToken(token)}</span>.
          </span>
        </p>
        <div className="flex flex-wrap items-center gap-4">
          <button type="button" onClick={onContinue} className={PRIMARY_BUTTON_CLASS}>
            Continue
          </button>
          <button type="button" onClick={onSignOut} className={TEXT_BUTTON_CLASS}>
            Use a different key
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {notice ? (
        <p role="alert" className={ERROR_TEXT_CLASS}>
          {notice}
        </p>
      ) : null}

      <section aria-labelledby={`${uid}-email-h`} className="space-y-3">
        <h3 id={`${uid}-email-h`} className="label-caps">
          New here
        </h3>
        <form onSubmit={handleSend} className="space-y-2" noValidate>
          <label htmlFor={emailId} className={FIELD_LABEL_CLASS}>
            Your email
          </label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              id={emailId}
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => onEmailChange(e.target.value)}
              placeholder="you@example.com"
              className={`${INPUT_CLASS} sm:max-w-sm`}
            />
            <button type="submit" disabled={sending} className={keySent ? SECONDARY_BUTTON_CLASS : PRIMARY_BUTTON_CLASS}>
              {sending ? "Sending…" : keySent ? "Send again" : "Email me a key"}
            </button>
          </div>
          <p className={HELP_TEXT_CLASS}>
            We email you a key and a sign-in link. There is nothing else to fill in.
          </p>
        </form>
        <div aria-live="polite">
          {sendError ? (
            <p role="alert" className={ERROR_TEXT_CLASS}>
              {sendError}
            </p>
          ) : null}
          {keySent ? (
            <p role="status" className="font-serif text-[15px] text-navy-text">
              Check your inbox. Click the link in the email, or paste the key below.
            </p>
          ) : null}
        </div>
      </section>

      <section aria-labelledby={`${uid}-key-h`} className="space-y-3">
        <h3 id={`${uid}-key-h`} className="label-caps">
          {keySent ? "Paste the key from your email" : "Already have a key?"}
        </h3>
        <form onSubmit={handlePaste} className="space-y-2" noValidate>
          <label htmlFor={keyId} className="sr-only">
            Chorus key
          </label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              id={keyId}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={pasteValue}
              onChange={(e) => setPasteValue(e.target.value)}
              placeholder="Paste your key"
              className={`${INPUT_CLASS} font-mono sm:max-w-sm`}
            />
            <button type="submit" disabled={verifying} className={SECONDARY_BUTTON_CLASS}>
              {verifying ? "Checking…" : "Use this key"}
            </button>
          </div>
        </form>
        {MOCK_MODE ? (
          <p className={HELP_TEXT_CLASS}>
            Mock mode: no email is sent and any key works.{" "}
            <button
              type="button"
              onClick={() => void verifyAndUse(MOCK_DEMO_KEY)}
              className={TEXT_BUTTON_CLASS}
            >
              Use the demo key
            </button>
          </p>
        ) : null}
        <div aria-live="polite">
          {pasteError ? (
            <p role="alert" className={ERROR_TEXT_CLASS}>
              {pasteError}
            </p>
          ) : null}
        </div>
      </section>

      {needsApiAddressField ? (
        <details open={apiAddressOpen} className="font-sans text-xs text-ink">
          <summary className="cursor-pointer text-silver">Advanced: Chorus API address</summary>
          <div className="mt-3 space-y-1">
            <label htmlFor={apiId} className={FIELD_LABEL_CLASS}>
              API address
            </label>
            <input
              id={apiId}
              type="url"
              value={baseUrl}
              onChange={(e) => onBaseUrlChange(e.target.value)}
              placeholder="https://chorus.example.com"
              className={`${INPUT_CLASS} font-mono sm:max-w-md`}
            />
            <p className={HELP_TEXT_CLASS}>
              Normally preset by whoever runs this site. Set it only if you were given an address.
            </p>
          </div>
        </details>
      ) : null}
    </div>
  );
}
