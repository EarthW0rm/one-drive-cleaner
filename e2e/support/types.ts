// SPDX-License-Identifier: GPL-3.0-only

export type ImageKind = "photo" | "screenshot" | "meme" | "document";

export interface Job {
  readonly id: number;
  readonly type: string;
  readonly status: "queued" | "running" | "paused" | "done" | "failed";
  readonly pause_reason: string | null;
  readonly error_code: string | null;
  readonly progress: { readonly done: number; readonly total: number };
}

export interface JobsResponse {
  readonly jobs: Job[];
}

export interface Category {
  readonly id: number;
  readonly slug: string;
  readonly name: string;
  readonly removable: boolean;
  readonly protected: boolean;
  readonly archived: boolean;
  readonly labels: number;
}

export interface CategoriesResponse {
  readonly categories: Category[];
}

export interface ReviewItem {
  readonly item_id: number;
  readonly suggested_category_id: number;
  readonly confidence: number;
  readonly user_category_id: number | null;
}

export interface ReviewResponse {
  readonly items: ReviewItem[];
  readonly next_cursor: string | null;
}

export interface LabelsResponse {
  readonly saved: number;
  readonly train_job_id: number | null;
}

export interface QuarantineItem {
  readonly item_id: number;
  readonly category_id: number | null;
  readonly mode: string;
}

export interface RestoreNotice {
  readonly item_id: number;
  readonly category_id: number | null;
  readonly restore_reason: string | null;
  readonly restore_fallback: boolean;
}

export interface QuarantineResponse {
  readonly items: QuarantineItem[];
  readonly total_count: number;
  readonly total_bytes: number;
  readonly next_cursor: string | null;
  readonly restore_notices: RestoreNotice[];
}

export interface MetricsSummary {
  readonly by_state: Readonly<Record<string, number>>;
  readonly backlog: {
    readonly extract: number;
    readonly classify: number;
    readonly review: number;
  };
  readonly quarantine: { readonly items: number; readonly bytes: number };
  readonly labels: { readonly total: number; readonly since_last_training: number };
  readonly false_positive_rate: { readonly last_7d: number | null; readonly total: number | null };
  readonly alerts: readonly { readonly code: string; readonly since: string }[];
}

export interface ModelsResponse {
  readonly current: { readonly id: number; readonly status: string } | null;
  readonly gate: {
    readonly eligible: boolean;
    readonly effective_mode: "manual" | "auto";
    readonly labels: number;
    readonly support: number;
    readonly precision: number | null;
  };
}

export interface ModelsTrainResponse {
  readonly job_id: number;
}
