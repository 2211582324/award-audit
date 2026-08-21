-- M5 unified orchestration: persist the exact M4 result consumed by each M5 case.
-- Historical decisions remain audit records. Only inert queued shells are safe to
-- backfill; completed/running/waiting cases must not be guessed into validity.

ALTER TABLE audit_case
    ADD COLUMN origin_m4_result_id INTEGER REFERENCES audit_result(id);

UPDATE audit_case
   SET origin_m4_result_id = (
       SELECT item.current_result_id
         FROM audit_stage_item AS item
        WHERE item.batch_id = audit_case.batch_id
          AND item.stage = 'm4'
          AND item.resource_code = audit_case.resource_code
          AND item.year = audit_case.year
   )
 WHERE status = 'queued'
   AND step_count = 0
   AND COALESCE(human_decision, '') = ''
   AND NOT EXISTS (SELECT 1 FROM tool_trace WHERE case_id = audit_case.id)
   AND NOT EXISTS (SELECT 1 FROM evidence_artifact WHERE case_id = audit_case.id)
   AND EXISTS (
       SELECT 1
         FROM audit_stage_item AS item
        WHERE item.batch_id = audit_case.batch_id
          AND item.stage = 'm4'
          AND item.resource_code = audit_case.resource_code
          AND item.year = audit_case.year
          AND item.current_result_id IS NOT NULL
   );

CREATE INDEX IF NOT EXISTS idx_audit_case_origin_m4_result
    ON audit_case(origin_m4_result_id);
