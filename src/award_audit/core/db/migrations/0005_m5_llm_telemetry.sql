ALTER TABLE audit_case ADD COLUMN llm_usage_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE audit_case ADD COLUMN verifier_llm_usage_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE audit_case ADD COLUMN last_error_detail TEXT NOT NULL DEFAULT '';
