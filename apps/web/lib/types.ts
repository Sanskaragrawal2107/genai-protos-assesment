export type PipelineStatus =
  | "APPLIED"
  | "SCREENED"
  | "SHORTLISTED"
  | "CONTACTED"
  | "INTERVIEW_SCHEDULED"
  | "INTERVIEWED"
  | "SELECTED"
  | "REJECTED"
  | "ON_HOLD";

export const PIPELINE_STATUSES: PipelineStatus[] = [
  "APPLIED",
  "SCREENED",
  "SHORTLISTED",
  "CONTACTED",
  "INTERVIEW_SCHEDULED",
  "INTERVIEWED",
  "SELECTED",
  "REJECTED",
  "ON_HOLD",
];

export interface CandidateSummary {
  id: string;
  full_name: string;
  email?: string | null;
  phone?: string | null;
  location?: string | null;
  experience_years?: number | null;
  current_role?: string | null;
  current_company?: string | null;
  skills: string[];
  github_url?: string | null;
  leetcode_url?: string | null;
  linkedin_url?: string | null;
  pipeline_status: PipelineStatus;
  resume_storage_path?: string | null;
  created_at?: string | null;
  match_score?: number | null;
  evaluation?: Record<string, unknown> | null;
}

export interface CandidateDetail extends CandidateSummary {
  preferred_location?: string | null;
  previous_companies: string[];
  education: { degree?: string; institution?: string; graduation_year?: number }[];
  university?: string | null;
  graduation_year?: number | null;
  programming_languages: string[];
  frameworks: string[];
  tools: string[];
  projects: { name?: string; description?: string; technologies?: string[] }[];
  summary?: string | null;
  portfolio_url?: string | null;
  resume_filename?: string | null;
  extraction_method?: string | null;
  updated_at?: string | null;
  applications: Record<string, unknown>[];
  communications: Communication[];
  interviews: Interview[];
  status_history: { from_status?: string; to_status: string; reason?: string; created_at?: string }[];
  notes: { note: string; created_at?: string; next_followup_at?: string }[];
  evaluations: EvaluationRecord[];
}

export interface Verification {
  available?: boolean;
  reason?: string;
  source_url?: string;
  profile_url?: string;
  relevant_repositories?: { name: string; url: string; matched_keywords: string[] }[];
  total_solved?: number;
  verified_keywords?: string[];
  caveat?: string;
}

export interface EvaluationRecord {
  id?: string;
  match_score: number;
  skills_score: number;
  experience_score: number;
  project_score: number;
  location_score: number;
  evidence_score: number;
  strengths: string[];
  missing_requirements: string[];
  concerns: string[];
  explanation: string;
  verification_evidence?: { github?: Verification; leetcode?: Verification };
  evaluated_at?: string;
  method?: string;
}

export interface EvaluationResult extends Partial<EvaluationRecord> {
  candidate_id: string;
  full_name: string;
  match_score: number;
  skills_score: number;
  experience_score: number;
  project_score: number;
  location_score: number;
  evidence_score: number;
  strengths: string[];
  missing_requirements: string[];
  concerns: string[];
  explanation: string;
  method: string;
  verification: { github?: Verification; leetcode?: Verification; signals?: string[] };
  candidate: CandidateSummary;
}

export interface Job {
  id: string;
  title: string;
  description: string;
  status: string;
  application_subject_prefix?: string | null;
  structured_requirements?: Record<string, unknown>;
  created_at?: string;
  candidate_count?: number;
}

export interface JobRequirements {
  title?: string | null;
  required_skills: string[];
  preferred_skills: string[];
  min_experience_years?: number | null;
  preferred_experience_years?: number | null;
  required_location?: string | null;
  locations?: string[];
  education?: string | null;
  responsibilities?: string[];
  qualifications?: string[];
}

export interface EvaluationRun {
  job_id: string;
  requirements: JobRequirements | null;
  requirement_method: string;
  candidate_pool?: number;
  evaluated: number;
  results: EvaluationResult[];
  errors?: string[];
}

export interface Interview {
  id: string;
  candidate_id: string;
  candidate_name?: string;
  provider: string;
  calendar_provider: string;
  calendar_event_id?: string | null;
  calendar_html_link?: string | null;
  meeting_id?: string | null;
  meeting_url?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  timezone: string;
  status: string;
  invite_status: string;
  attendees: { email: string }[];
  notes?: string | null;
}

export interface Communication {
  id: string;
  candidate_id: string;
  candidate_name?: string;
  direction: string;
  kind: string;
  subject?: string | null;
  content?: string | null;
  status: string;
  error_message?: string | null;
  created_at?: string;
  sent_at?: string;
}

export interface PendingAction {
  id: string;
  kind: string;
  title: string;
  payload: Record<string, unknown>;
  status: string;
  result?: Record<string, unknown>;
  error_message?: string | null;
  created_at?: string;
}

export interface Connector {
  id: string;
  provider: string;
  connected_email?: string | null;
  display_name?: string | null;
  status: string;
  last_error?: string | null;
  created_at?: string | null;
  last_verified_at?: string | null;
  sync?: SyncSnapshot;
}

export interface SyncSnapshot {
  enabled: boolean;
  interval_minutes: number;
  last_synced_at?: string | null;
  next_sync_at?: string | null;
  last_sync_status?: string | null;
  last_sync_error?: string | null;
  consecutive_failures: number;
}

export interface Me {
  identity: { user_id: string; email?: string | null; name?: string | null; mode: string };
  workspace: {
    id: string;
    name: string;
    company_name: string;
    company_phone: string;
    interviewer_name: string;
    profile_complete: boolean;
  };
  capabilities: {
    llm: boolean;
    composio: boolean;
    storage: string;
    supabase_auth: boolean;
  };
  connectors: Connector[];
}

export interface DashboardData {
  totals: {
    candidates: number;
    new_this_week: number;
    awaiting_screening: number;
    shortlisted: number;
    interviews_scheduled: number;
    pending_followups: number;
  };
  by_status: Record<string, number>;
  recent_ingestion: {
    id: string;
    status: string;
    trigger: string;
    attempts: number;
    stats: Record<string, number>;
    error_message?: string | null;
    created_at?: string;
    completed_at?: string;
  }[];
  recent_activity: {
    id: string;
    action: string;
    entity_type?: string;
    entity_id?: string;
    metadata?: Record<string, unknown>;
    created_at?: string;
  }[];
  upcoming_interviews: {
    id: string;
    candidate_name: string;
    start_time?: string;
    end_time?: string;
    provider: string;
    meeting_url?: string | null;
    status: string;
  }[];
}

export interface SyncJob {
  id: string;
  status: string;
  trigger: string;
  attempts: number;
  error_message?: string | null;
  stats: Record<string, number>;
  started_at?: string | null;
  completed_at?: string | null;
  next_retry_at?: string | null;
  created_at?: string | null;
}

export interface SyncSettingsResponse {
  connected: boolean;
  connected_email?: string;
  connector_status?: string;
  presets: number[];
  settings?: {
    id: string;
    enabled: boolean;
    interval_minutes: number;
    is_custom_interval: boolean;
    last_synced_at?: string | null;
    next_sync_at?: string | null;
    last_sync_status?: string | null;
    last_sync_error?: string | null;
    consecutive_failures: number;
    query: string;
  };
}

export interface AppConnection {
  toolkit: string;
  name?: string;
  logo?: string;
  url: string;
}

export interface ChatPayload {
  thread_id: string;
  intent: string;
  message: string;
  reply?: string;
  reasoning?: string | null;
  data: Record<string, unknown>;
  candidates: CandidateSummary[];
  pending_actions: PendingAction[];
  connections?: AppConnection[];
}
