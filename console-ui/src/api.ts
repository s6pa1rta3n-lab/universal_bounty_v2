export type Escrow = {
  verified?: boolean;
  amount_usd?: number;
  source?: string;
};

export type BountyEvent = {
  t?: string;
  type?: string;
  detail?: string;
};

export type GcpMeta = {
  project?: string;
  region?: string;
  firestore_doc?: string;
  trace_id?: string;
};

export type Bounty = {
  bounty_id?: string;
  title?: string;
  issue_url?: string;
  pr_url?: string;
  audit_status?: string;
  merge_allowed?: boolean;
  cheat_detected?: string | null;
  source?: string;
  escrow?: Escrow;
  events?: BountyEvent[];
  gcp?: GcpMeta;
  agents?: Record<string, string>;
};

export type AgentCard = {
  id: string;
  name: string;
  version?: string;
  identity?: string;
  status?: string;
  tool_scope?: string[];
};

export type Registry = {
  agents?: AgentCard[];
  track?: string;
};

export type Health = {
  service?: string;
  status?: string;
};

export async function fetchJson<T>(path: string): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) {
    throw new Error(`${path} ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export type HistoryPr = {
  wait_on?: string;
  outcome?: string;
  repo?: string;
  number?: string;
  title?: string;
  url?: string;
  opened?: string;
  closed?: string | null;
  hours_open?: number | null;
};

export type HistoryRepo = {
  repo: string;
  opened: number;
  waiting: number;
  merged: number;
  closed: number;
  merge_rate?: string;
};

export type HistoryClaim = {
  wait_on?: string;
  repo?: string;
  number?: string;
  title?: string;
  url?: string;
  platform?: string;
  status?: string;
  payout_usd?: number;
  payout_note?: string;
};

export type HistoryArchive = {
  why_parked?: string;
  state?: string;
  repo?: string;
  number?: string;
  title?: string;
  url?: string;
  opened?: string;
};

export type Overseer = {
  meta?: {
    source?: string;
    snapshot?: string;
    window?: string;
    rule?: string;
    money?: string;
  };
  sprint?: {
    opened?: number;
    waiting?: number;
    merged?: number;
    closed?: number;
    days?: { day: string; opened: number }[];
    repos?: HistoryRepo[];
    prs?: HistoryPr[];
  };
  claims?: HistoryClaim[];
  archive?: HistoryArchive[];
};

export function loadFleet() {
  return Promise.all([
    fetchJson<Health>("/health"),
    fetchJson<Registry>("/api/registry"),
    fetchJson<{ bounty: Bounty | null }>("/api/bounties/latest"),
  ]);
}

export function loadHistory() {
  return fetchJson<Overseer>("/api/history");
}

/* =========================================================================
   3D Constellation Pipeline Types and API Integration
   ========================================================================= */

export type CanonicalStage =
  | "queued"
  | "pending_triage"
  | "pr_open"
  | "completed"
  | "failed";

export const CANONICAL_STAGES: readonly CanonicalStage[] = [
  "queued",
  "pending_triage",
  "pr_open",
  "completed",
  "failed",
] as const;

export type PipelineLead = {
  id: string;
  repo: string;
  issue_number: number | null;
  title: string;
  status: CanonicalStage;
  raw_status: string;
  projected_payout: string;
  projected_payout_usd: number;
  qualification_reason: string;
  ecosystem: string;
  escrow_verified: boolean;
  issue_url: string;
  pr_url: string;
};

export type PipelineCounts = {
  queued: number;
  pending_triage: number;
  pr_open: number;
  completed: number;
  failed: number;
  total: number;
};

export type PipelineGrouped = {
  queued: PipelineLead[];
  pending_triage: PipelineLead[];
  pr_open: PipelineLead[];
  completed: PipelineLead[];
  failed: PipelineLead[];
};

export type PipelinePayload = {
  leads: PipelineLead[];
  grouped: PipelineGrouped;
  counts: PipelineCounts;
};

export type StageSpatialConfig = {
  label: string;
  colorHex: number;
  colorCss: string;
  anchor: [number, number, number];
  camPos: [number, number, number];
  radius: number;
  description: string;
};

export const STAGE_CONFIG: Record<CanonicalStage, StageSpatialConfig> = {
  queued: {
    label: "Queued",
    colorHex: 0x94a3b8,
    colorCss: "#94a3b8",
    anchor: [-60, 15, -30],
    camPos: [-60, 25, 20],
    radius: 18,
    description: "Intake & candidate pool pending qualification",
  },
  pending_triage: {
    label: "Pending Triage",
    colorHex: 0xe8c36a,
    colorCss: "#e8c36a",
    anchor: [-30, -20, 25],
    camPos: [-30, -10, 75],
    radius: 20,
    description: "Under active qualification and priority triage",
  },
  pr_open: {
    label: "PR Open",
    colorHex: 0x00f0ff,
    colorCss: "#00f0ff",
    anchor: [0, 25, 0],
    camPos: [0, 35, 50],
    radius: 24,
    description: "Active fleet execution with open draft/ready PRs",
  },
  completed: {
    label: "Completed",
    colorHex: 0xc0ff70,
    colorCss: "#c0ff70",
    anchor: [50, 15, -15],
    camPos: [50, 25, 35],
    radius: 22,
    description: "Merged, verified, and settled payouts",
  },
  failed: {
    label: "Failed",
    colorHex: 0xe11d2e,
    colorCss: "#e11d2e",
    anchor: [45, -25, 30],
    camPos: [45, -15, 80],
    radius: 18,
    description: "Rejected, disqualified, or abandoned leads",
  },
};

export function loadPipeline(): Promise<PipelinePayload> {
  return fetchJson<PipelinePayload>("/api/pipeline");
}

export function formatUsd(amount: number): string {
  if (amount <= 0) return "$0.00";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}
