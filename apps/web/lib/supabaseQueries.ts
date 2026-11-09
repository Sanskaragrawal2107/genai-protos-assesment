import { SupabaseClient } from "@supabase/supabase-js";
import type {
  CandidateDetail,
  CandidateSummary,
  Communication,
  DashboardData,
  EvaluationRecord,
  Interview,
  Job,
  PendingAction,
  PipelineStatus,
} from "./types";

export async function getActiveWorkspaceId(supabase: SupabaseClient): Promise<string> {
  const { data: userRes } = await supabase.auth.getUser();
  const userId = userRes?.user?.id;
  if (!userId) {
    throw new Error("User is not authenticated");
  }

  const { data: member } = await supabase
    .from("workspace_members")
    .select("workspace_id")
    .eq("user_id", userId)
    .order("created_at", { ascending: true })
    .limit(1)
    .maybeSingle();

  if (member?.workspace_id) {
    return member.workspace_id;
  }

  // Fallback: check workspaces directly
  const { data: ws } = await supabase
    .from("workspaces")
    .select("id")
    .limit(1)
    .maybeSingle();

  if (ws?.id) {
    return ws.id;
  }

  throw new Error("No active workspace found for user");
}

export async function fetchMeDirect(supabase: SupabaseClient): Promise<any> {
  const { data: userRes } = await supabase.auth.getUser();
  const user = userRes?.user;
  if (!user) {
    throw new Error("Not authenticated");
  }

  let wsId: string = "default";
  let wsName = "Recruitment Workspace";
  let wsSettings: Record<string, any> = {};

  const { data: member } = await supabase
    .from("workspace_members")
    .select("workspace_id, workspaces(name, settings)")
    .eq("user_id", user.id)
    .order("created_at", { ascending: true })
    .limit(1)
    .maybeSingle();

  if (member?.workspace_id) {
    wsId = member.workspace_id;
    const wsData: any = member.workspaces;
    if (wsData?.name) wsName = wsData.name;
    if (wsData?.settings) wsSettings = wsData.settings;
  } else {
    const { data: ws } = await supabase.from("workspaces").select("id, name, settings").limit(1).maybeSingle();
    if (ws) {
      wsId = ws.id;
      wsName = ws.name || wsName;
      if (ws.settings) wsSettings = ws.settings;
    }
  }

  const { data: accounts } = await supabase
    .from("connected_accounts")
    .select("*")
    .eq("workspace_id", wsId)
    .order("created_at", { ascending: false });

  return {
    identity: {
      user_id: user.id,
      email: user.email,
      name: user.user_metadata?.full_name || user.email?.split("@")[0] || "Recruiter",
      mode: "supabase_auth",
    },
    workspace: {
      id: wsId,
      name: wsName,
      company_name: typeof wsSettings.company_name === "string" ? wsSettings.company_name : "",
      company_phone: typeof wsSettings.company_phone === "string" ? wsSettings.company_phone : "",
      interviewer_name:
        typeof wsSettings.interviewer_name === "string" ? wsSettings.interviewer_name : "",
      profile_complete: Boolean(wsSettings.company_name),
    },
    capabilities: {
      llm: true,
      composio: true,
      storage: "supabase",
      supabase_auth: true,
    },
    connectors: (accounts || []).map((acc: any) => ({
      id: acc.id,
      provider: acc.provider,
      connected_email: acc.connected_email,
      display_name: acc.display_name,
      status: acc.status,
      last_error: acc.last_error,
      created_at: acc.created_at,
      last_verified_at: acc.last_verified_at,
    })),
  };
}

export async function fetchDashboardData(supabase: SupabaseClient, workspaceId?: string): Promise<DashboardData> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  const now = new Date();
  const weekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000).toISOString();
  const twoHoursAgo = new Date(now.getTime() - 2 * 60 * 60 * 1000).toISOString();

  // Run all independent queries concurrently over Supabase HTTP/2
  const [
    allCandidatesRes,
    newThisWeekRes,
    awaitingRes,
    shortlistedRes,
    interviewsScheduledRes,
    pendingEmailsRes,
    pendingActionsRes,
    statusGroupRes,
    recentJobsRes,
    recentActivityRes,
    upcomingInterviewsRes,
  ] = await Promise.all([
    supabase.from("candidates").select("id", { count: "exact", head: true }).eq("workspace_id", wsId),
    supabase.from("candidates").select("id", { count: "exact", head: true }).eq("workspace_id", wsId).gte("created_at", weekAgo),
    supabase.from("candidates").select("id", { count: "exact", head: true }).eq("workspace_id", wsId).in("pipeline_status", ["APPLIED", "SCREENED", "ON_HOLD"]),
    supabase.from("candidates").select("id", { count: "exact", head: true }).eq("workspace_id", wsId).eq("pipeline_status", "SHORTLISTED"),
    supabase.from("interviews").select("id", { count: "exact", head: true }).eq("workspace_id", wsId).in("status", ["scheduled", "rescheduled"]).gte("start_time", twoHoursAgo),
    supabase.from("candidate_communications").select("id", { count: "exact", head: true }).eq("workspace_id", wsId).eq("status", "pending_approval"),
    supabase.from("pending_actions").select("id", { count: "exact", head: true }).eq("workspace_id", wsId).eq("status", "pending"),
    supabase.from("candidates").select("pipeline_status").eq("workspace_id", wsId),
    supabase.from("ingestion_jobs").select("*").eq("workspace_id", wsId).order("created_at", { ascending: false }).limit(6),
    supabase.from("recruiter_activity_logs").select("*").eq("workspace_id", wsId).order("created_at", { ascending: false }).limit(10),
    supabase.from("interviews").select("id, start_time, end_time, provider, meeting_url, status, candidates(full_name)").eq("workspace_id", wsId).in("status", ["scheduled", "rescheduled"]).gte("start_time", twoHoursAgo).order("start_time", { ascending: true }).limit(5),
  ]);

  const byStatus: Record<string, number> = {};
  if (statusGroupRes.data) {
    for (const row of statusGroupRes.data) {
      const st = row.pipeline_status || "APPLIED";
      byStatus[st] = (byStatus[st] || 0) + 1;
    }
  }

  const upcoming = (upcomingInterviewsRes.data || []).map((row: any) => ({
    id: row.id,
    candidate_name: row.candidates?.full_name || "Unknown Candidate",
    start_time: row.start_time,
    end_time: row.end_time,
    provider: row.provider,
    meeting_url: row.meeting_url,
    status: row.status,
  }));

  const pendingEmailsCount = pendingEmailsRes.count || 0;
  const pendingActionsCount = pendingActionsRes.count || 0;

  return {
    totals: {
      candidates: allCandidatesRes.count || 0,
      new_this_week: newThisWeekRes.count || 0,
      awaiting_screening: awaitingRes.count || 0,
      shortlisted: shortlistedRes.count || 0,
      interviews_scheduled: interviewsScheduledRes.count || 0,
      pending_followups: pendingEmailsCount + pendingActionsCount,
    },
    by_status: byStatus,
    recent_ingestion: (recentJobsRes.data || []).map((job: any) => ({
      id: job.id,
      status: job.status,
      trigger: job.trigger,
      attempts: job.attempts,
      stats: job.stats || {},
      error_message: job.error_message,
      created_at: job.created_at,
      completed_at: job.completed_at,
    })),
    recent_activity: (recentActivityRes.data || []).map((log: any) => ({
      id: log.id,
      action: log.action,
      entity_type: log.entity_type,
      entity_id: log.entity_id,
      metadata: log.metadata || {},
      created_at: log.created_at,
    })),
    upcoming_interviews: upcoming,
  };
}

export async function fetchCandidates(
  supabase: SupabaseClient,
  workspaceId?: string,
  params?: Record<string, string>,
): Promise<CandidateSummary[]> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  let query = supabase
    .from("candidates")
    .select("*, candidate_evaluations(match_score, skills_score, experience_score, project_score, location_score, evidence_score)")
    .eq("workspace_id", wsId);

  if (params?.status) {
    query = query.eq("pipeline_status", params.status);
  }
  if (params?.location) {
    query = query.ilike("location", `%${params.location}%`);
  }
  if (params?.min_experience_years) {
    query = query.gte("experience_years", Number(params.min_experience_years));
  }

  query = query.order("created_at", { ascending: false });

  const limit = params?.limit ? parseInt(params.limit, 10) : 200;
  query = query.limit(limit);

  const { data, error } = await query;
  if (error) throw new Error(error.message);

  return (data || []).map((row: any) => {
    const evals = row.candidate_evaluations || [];
    const bestEval = evals.length > 0 ? evals[0] : null;
    return {
      id: row.id,
      full_name: row.full_name,
      email: row.email,
      phone: row.phone,
      location: row.location,
      experience_years: row.experience_years,
      current_role: row.current_role,
      current_company: row.current_company,
      skills: Array.isArray(row.skills) ? row.skills : [],
      github_url: row.github_url,
      leetcode_url: row.leetcode_url,
      linkedin_url: row.linkedin_url,
      pipeline_status: row.pipeline_status as PipelineStatus,
      resume_storage_path: row.resume_storage_path,
      created_at: row.created_at,
      match_score: bestEval ? bestEval.match_score : null,
      evaluation: bestEval,
    };
  });
}

export async function fetchCandidateDetail(
  supabase: SupabaseClient,
  candidateId: string,
  workspaceId?: string,
): Promise<CandidateDetail> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));

  const [
    candRes,
    evalsRes,
    notesRes,
    statusHistoryRes,
    commsRes,
    interviewsRes,
    appsRes,
  ] = await Promise.all([
    supabase.from("candidates").select("*").eq("id", candidateId).eq("workspace_id", wsId).single(),
    supabase.from("candidate_evaluations").select("*").eq("candidate_id", candidateId).eq("workspace_id", wsId).order("evaluated_at", { ascending: false }),
    supabase.from("recruiter_notes").select("*").eq("candidate_id", candidateId).eq("workspace_id", wsId).order("created_at", { ascending: false }),
    supabase.from("candidate_status_history").select("*").eq("candidate_id", candidateId).eq("workspace_id", wsId).order("created_at", { ascending: false }),
    supabase.from("candidate_communications").select("*").eq("candidate_id", candidateId).eq("workspace_id", wsId).order("created_at", { ascending: false }),
    supabase.from("interviews").select("*").eq("candidate_id", candidateId).eq("workspace_id", wsId).order("start_time", { ascending: false }),
    supabase.from("candidate_applications").select("*").eq("candidate_id", candidateId).eq("workspace_id", wsId),
  ]);

  if (candRes.error || !candRes.data) {
    throw new Error(candRes.error?.message || "Candidate not found");
  }

  const row = candRes.data;
  const evals = evalsRes.data || [];
  const bestEval = evals.length > 0 ? evals[0] : null;

  return {
    id: row.id,
    full_name: row.full_name,
    email: row.email,
    phone: row.phone,
    location: row.location,
    preferred_location: row.preferred_location,
    experience_years: row.experience_years,
    current_role: row.current_role,
    current_company: row.current_company,
    previous_companies: Array.isArray(row.previous_companies) ? row.previous_companies : [],
    education: Array.isArray(row.education) ? row.education : [],
    university: row.university,
    graduation_year: row.graduation_year,
    skills: Array.isArray(row.skills) ? row.skills : [],
    programming_languages: Array.isArray(row.programming_languages) ? row.programming_languages : [],
    frameworks: Array.isArray(row.frameworks) ? row.frameworks : [],
    tools: Array.isArray(row.tools) ? row.tools : [],
    projects: Array.isArray(row.projects) ? row.projects : [],
    summary: row.summary,
    github_url: row.github_url,
    leetcode_url: row.leetcode_url,
    linkedin_url: row.linkedin_url,
    portfolio_url: row.portfolio_url,
    resume_storage_path: row.resume_storage_path,
    resume_filename: row.resume_filename,
    extraction_method: row.extraction_method,
    pipeline_status: row.pipeline_status as PipelineStatus,
    created_at: row.created_at,
    updated_at: row.updated_at,
    match_score: bestEval ? bestEval.match_score : null,
    evaluation: bestEval,
    applications: appsRes.data || [],
    communications: (commsRes.data || []).map((c: any) => ({
      id: c.id,
      candidate_id: c.candidate_id,
      candidate_name: row.full_name,
      direction: c.direction,
      kind: c.kind,
      subject: c.subject,
      content: c.content,
      status: c.status,
      error_message: c.error_message,
      created_at: c.created_at,
      sent_at: c.sent_at,
    })),
    interviews: (interviewsRes.data || []).map((i: any) => ({
      id: i.id,
      candidate_id: i.candidate_id,
      candidate_name: row.full_name,
      provider: i.provider,
      calendar_provider: i.calendar_provider,
      calendar_event_id: i.calendar_event_id,
      calendar_html_link: i.calendar_html_link,
      meeting_id: i.meeting_id,
      meeting_url: i.meeting_url,
      start_time: i.start_time,
      end_time: i.end_time,
      timezone: i.timezone,
      status: i.status,
      invite_status: i.invite_status,
      attendees: Array.isArray(i.attendees) ? i.attendees : [],
      notes: i.notes,
    })),
    status_history: (statusHistoryRes.data || []).map((h: any) => ({
      from_status: h.from_status,
      to_status: h.to_status,
      reason: h.reason,
      created_at: h.created_at,
    })),
    notes: (notesRes.data || []).map((n: any) => ({
      note: n.note,
      created_at: n.created_at,
      next_followup_at: n.next_followup_at,
    })),
    evaluations: evals.map((e: any) => ({
      id: e.id,
      match_score: e.match_score,
      skills_score: e.skills_score,
      experience_score: e.experience_score,
      project_score: e.project_score,
      location_score: e.location_score,
      evidence_score: e.evidence_score,
      strengths: Array.isArray(e.strengths) ? e.strengths : [],
      missing_requirements: Array.isArray(e.missing_requirements) ? e.missing_requirements : [],
      concerns: Array.isArray(e.concerns) ? e.concerns : [],
      explanation: e.explanation,
      verification_evidence: e.verification_evidence,
      evaluated_at: e.evaluated_at,
      method: e.method,
    })),
  };
}

export async function fetchJobs(supabase: SupabaseClient, workspaceId?: string): Promise<Job[]> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  const { data, error } = await supabase
    .from("jobs")
    .select("*, candidate_evaluations(count)")
    .eq("workspace_id", wsId)
    .order("created_at", { ascending: false });

  if (error) throw new Error(error.message);

  return (data || []).map((job: any) => ({
    id: job.id,
    title: job.title,
    description: job.description,
    status: job.status,
    application_subject_prefix: job.application_subject_prefix,
    structured_requirements: job.structured_requirements,
    created_at: job.created_at,
    candidate_count: job.candidate_evaluations?.[0]?.count || 0,
  }));
}

export async function fetchJobDetail(supabase: SupabaseClient, jobId: string, workspaceId?: string): Promise<any> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));

  const [jobRes, evalsRes] = await Promise.all([
    supabase.from("jobs").select("*").eq("id", jobId).eq("workspace_id", wsId).single(),
    supabase
      .from("candidate_evaluations")
      .select("*, candidates(*)")
      .eq("job_id", jobId)
      .eq("workspace_id", wsId)
      .order("match_score", { ascending: false }),
  ]);

  if (jobRes.error || !jobRes.data) {
    throw new Error(jobRes.error?.message || "Job not found");
  }

  const job = jobRes.data;
  const candidates = (evalsRes.data || []).map((row: any) => {
    const cand = row.candidates || {};
    return {
      candidate_id: cand.id,
      full_name: cand.full_name,
      match_score: row.match_score,
      skills_score: row.skills_score,
      experience_score: row.experience_score,
      project_score: row.project_score,
      location_score: row.location_score,
      evidence_score: row.evidence_score,
      strengths: Array.isArray(row.strengths) ? row.strengths : [],
      missing_requirements: Array.isArray(row.missing_requirements) ? row.missing_requirements : [],
      concerns: Array.isArray(row.concerns) ? row.concerns : [],
      explanation: row.explanation,
      method: row.method,
      verification: row.verification_evidence || {},
      candidate: {
        id: cand.id,
        full_name: cand.full_name,
        email: cand.email,
        phone: cand.phone,
        location: cand.location,
        experience_years: cand.experience_years,
        current_role: cand.current_role,
        current_company: cand.current_company,
        skills: Array.isArray(cand.skills) ? cand.skills : [],
        github_url: cand.github_url,
        leetcode_url: cand.leetcode_url,
        linkedin_url: cand.linkedin_url,
        pipeline_status: cand.pipeline_status,
        resume_storage_path: cand.resume_storage_path,
        created_at: cand.created_at,
        match_score: row.match_score,
      },
    };
  });

  return {
    ...job,
    candidates,
  };
}

export async function fetchCommunications(
  supabase: SupabaseClient,
  workspaceId?: string,
  limit: number = 50,
): Promise<Communication[]> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  const { data, error } = await supabase
    .from("candidate_communications")
    .select("*, candidates(full_name)")
    .eq("workspace_id", wsId)
    .order("created_at", { ascending: false })
    .limit(limit);

  if (error) throw new Error(error.message);

  return (data || []).map((row: any) => ({
    id: row.id,
    candidate_id: row.candidate_id,
    candidate_name: row.candidates?.full_name || "Unknown Candidate",
    direction: row.direction,
    kind: row.kind,
    subject: row.subject,
    content: row.content,
    status: row.status,
    error_message: row.error_message,
    created_at: row.created_at,
    sent_at: row.sent_at,
  }));
}

export async function fetchInterviews(supabase: SupabaseClient, workspaceId?: string): Promise<Interview[]> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  const { data, error } = await supabase
    .from("interviews")
    .select("*, candidates(full_name)")
    .eq("workspace_id", wsId)
    .order("start_time", { ascending: true });

  if (error) throw new Error(error.message);

  return (data || []).map((row: any) => ({
    id: row.id,
    candidate_id: row.candidate_id,
    candidate_name: row.candidates?.full_name || "Unknown Candidate",
    provider: row.provider,
    calendar_provider: row.calendar_provider,
    calendar_event_id: row.calendar_event_id,
    calendar_html_link: row.calendar_html_link,
    meeting_id: row.meeting_id,
    meeting_url: row.meeting_url,
    start_time: row.start_time,
    end_time: row.end_time,
    timezone: row.timezone,
    status: row.status,
    invite_status: row.invite_status,
    attendees: Array.isArray(row.attendees) ? row.attendees : [],
    notes: row.notes,
  }));
}

export async function fetchPendingActions(
  supabase: SupabaseClient,
  workspaceId?: string,
  status: string = "pending",
): Promise<PendingAction[]> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  const { data, error } = await supabase
    .from("pending_actions")
    .select("*")
    .eq("workspace_id", wsId)
    .eq("status", status)
    .order("created_at", { ascending: false });

  if (error) throw new Error(error.message);

  return (data || []).map((row: any) => ({
    id: row.id,
    kind: row.kind,
    title: row.title,
    payload: row.payload || {},
    status: row.status,
    result: row.result || {},
    error_message: row.error_message,
    created_at: row.created_at,
  }));
}

export async function updateCandidateStatusDirect(
  supabase: SupabaseClient,
  candidateId: string,
  newStatus: string,
  reason?: string,
): Promise<void> {
  const wsId = await getActiveWorkspaceId(supabase);
  const { data: userRes } = await supabase.auth.getUser();

  const { data: current } = await supabase
    .from("candidates")
    .select("pipeline_status")
    .eq("id", candidateId)
    .eq("workspace_id", wsId)
    .single();

  const fromStatus = current?.pipeline_status;

  const { error: updateErr } = await supabase
    .from("candidates")
    .update({ pipeline_status: newStatus, updated_at: new Date().toISOString() })
    .eq("id", candidateId)
    .eq("workspace_id", wsId);

  if (updateErr) throw new Error(updateErr.message);

  // Record history
  await supabase.from("candidate_status_history").insert({
    workspace_id: wsId,
    candidate_id: candidateId,
    from_status: fromStatus,
    to_status: newStatus,
    changed_by: userRes?.user?.email || "recruiter",
    reason: reason || null,
  });

  // Record activity
  await supabase.from("recruiter_activity_logs").insert({
    workspace_id: wsId,
    action: `status_change:${newStatus}`,
    entity_type: "candidate",
    entity_id: candidateId,
    metadata: { from: fromStatus, to: newStatus, reason },
  });
}

export async function addCandidateNoteDirect(
  supabase: SupabaseClient,
  candidateId: string,
  note: string,
  nextFollowupAt?: string | null,
): Promise<void> {
  const wsId = await getActiveWorkspaceId(supabase);
  const { data: userRes } = await supabase.auth.getUser();

  const { error } = await supabase.from("recruiter_notes").insert({
    workspace_id: wsId,
    candidate_id: candidateId,
    recruiter_id: userRes?.user?.id || null,
    note,
    next_followup_at: nextFollowupAt || null,
  });

  if (error) throw new Error(error.message);

  await supabase.from("recruiter_activity_logs").insert({
    workspace_id: wsId,
    action: "add_note",
    entity_type: "candidate",
    entity_id: candidateId,
    metadata: { note: note.slice(0, 100) },
  });
}

export async function fetchChatThreads(supabase: SupabaseClient, workspaceId?: string): Promise<any[]> {
  const wsId = workspaceId || (await getActiveWorkspaceId(supabase));
  const { data, error } = await supabase
    .from("chat_threads")
    .select("id, title, created_at, updated_at")
    .eq("workspace_id", wsId)
    .order("updated_at", { ascending: false });

  if (error) throw new Error(error.message);
  return data || [];
}

export async function fetchThreadMessages(supabase: SupabaseClient, threadId: string): Promise<any[]> {
  const { data, error } = await supabase
    .from("chat_messages")
    .select("id, role, content, payload, created_at")
    .eq("thread_id", threadId)
    .order("created_at", { ascending: true });

  if (error) throw new Error(error.message);
  return data || [];
}

export async function deleteThreadDirect(supabase: SupabaseClient, threadId: string): Promise<void> {
  const { error } = await supabase.from("chat_threads").delete().eq("id", threadId);
  if (error) throw new Error(error.message);
}

export async function fetchCandidateAttachments(
  supabase: SupabaseClient,
  candidateId: string,
): Promise<any[]> {
  try {
    const wsId = await getActiveWorkspaceId(supabase);
    const { data: apps } = await supabase
      .from("candidate_applications")
      .select("email_message_id")
      .eq("candidate_id", candidateId);

    const emailMsgIds = (apps || []).map((a) => a.email_message_id).filter(Boolean);
    if (emailMsgIds.length === 0) return [];

    const { data } = await supabase
      .from("email_attachments")
      .select("*")
      .eq("workspace_id", wsId)
      .in("email_message_id", emailMsgIds);

    return data || [];
  } catch {
    return [];
  }
}

export async function fetchJobEvaluationsDirect(
  supabase: SupabaseClient,
  jobId: string,
  limit = 50,
): Promise<any[]> {
  try {
    const wsId = await getActiveWorkspaceId(supabase);
    const { data, error } = await supabase
      .from("candidate_evaluations")
      .select("*, candidates(*)")
      .eq("job_id", jobId)
      .eq("workspace_id", wsId)
      .order("match_score", { ascending: false })
      .limit(limit);

    if (error) return [];
    return (data || []).map((row: any) => ({
      ...row,
      candidate: row.candidates
        ? {
            ...row.candidates,
            skills: row.candidates.skills || [],
            match_score: row.match_score,
          }
        : null,
    }));
  } catch {
    return [];
  }
}

export async function createJobDirect(
  supabase: SupabaseClient,
  title: string,
  description: string,
  applicationSubjectPrefix?: string | null,
): Promise<any> {
  const wsId = await getActiveWorkspaceId(supabase);
  const { data: userRes } = await supabase.auth.getUser();

  const { data, error } = await supabase
    .from("jobs")
    .insert({
      workspace_id: wsId,
      title,
      description,
      status: "open",
      created_by: userRes?.user?.id || null,
    })
    .select()
    .single();

  if (error) throw new Error(error.message);
  return data;
}

