ALTER TABLE audit_case ADD COLUMN evidence_progress_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE verification_report ADD COLUMN supplement_requests_json TEXT NOT NULL DEFAULT '[]';
