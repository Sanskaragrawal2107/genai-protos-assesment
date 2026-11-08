-- ============================================================================
-- 0005_perf_indexes.sql — indexes for hot read paths
--
-- Two lookups were doing sequential scans on growing tables:
--   * scheduler / active-job guard: ingestion_jobs by (connected_account_id, status)
--   * inbound-email dedupe:          candidate_communications by
--                                    (candidate_id, provider_message_id)
--
-- Safe to re-run: IF NOT EXISTS. Mirrored in apps/api/app/db.py.
-- ============================================================================

create index if not exists ix_jobs_account_status
    on public.ingestion_jobs(connected_account_id, status);

create index if not exists ix_comms_candidate_provider
    on public.candidate_communications(candidate_id, provider_message_id);
