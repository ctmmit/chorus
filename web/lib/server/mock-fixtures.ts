/**
 * Server-only reader for web/mocks/*.json — backs the /api/mock/* route
 * handlers used when NEXT_PUBLIC_CHORUS_MOCK=1. Never import from a Client
 * Component; it touches the filesystem.
 */
import { readFile } from "node:fs/promises";
import path from "node:path";

import type { Job, ShowListing } from "@/lib/api-types";

const MOCKS_DIR = path.join(process.cwd(), "mocks");
const FIXTURE_JOB_FILES = ["job_investor.json", "job_popculture.json"] as const;

async function readJsonFile<T>(filename: string): Promise<T> {
  const raw = await readFile(path.join(MOCKS_DIR, filename), "utf-8");
  return JSON.parse(raw) as T;
}

export async function readMockShows(): Promise<ShowListing[]> {
  return readJsonFile<ShowListing[]>("shows.json");
}

export async function readMockJob(jobId: string): Promise<Job | null> {
  for (const file of FIXTURE_JOB_FILES) {
    const job = await readJsonFile<Job>(file);
    if (job.job_id === jobId) return job;
  }
  return null;
}

/** Heuristic used only in mock mode: route a submitted soul to whichever
 * canned fixture job resembles it, so "load sample" -> submit feels real
 * (the pop-culture sample soul routes to the single-episode fixture; any
 * other soul, including the investor sample, routes to the five-episode
 * fixture with one skipped/missing-transcript episode). */
export async function pickMockJobForSoul(soul: string): Promise<Job> {
  const wantsPopCulture = soul.includes("Pop-Culture Critic");
  return readJsonFile<Job>(wantsPopCulture ? "job_popculture.json" : "job_investor.json");
}

const SOUL_FILES: Record<string, string> = {
  investor: "souls/soul_investor.md",
  popculture: "souls/soul_popculture.md",
};

export async function readSampleSoul(name: string): Promise<string | null> {
  const file = SOUL_FILES[name];
  if (!file) return null;
  return readFile(path.join(MOCKS_DIR, file), "utf-8");
}
