ALTER TABLE audit_case ADD COLUMN reflection_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audit_case ADD COLUMN human_decision TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_case ADD COLUMN human_decision_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_case ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_case ADD COLUMN reviewed_at TEXT NOT NULL DEFAULT '';

CREATE TABLE verification_report (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id                  INTEGER NOT NULL REFERENCES audit_case(id),
    sequence                 INTEGER NOT NULL,
    target_match             TEXT    NOT NULL,
    year_match               TEXT    NOT NULL,
    source_authority         TEXT    NOT NULL,
    coverage_complete        TEXT    NOT NULL,
    contradictions_json      TEXT    NOT NULL DEFAULT '[]',
    missing_evidence_json    TEXT    NOT NULL DEFAULT '[]',
    recommended_action       TEXT    NOT NULL,
    reason_codes_json        TEXT    NOT NULL DEFAULT '[]',
    deterministic_action     TEXT    NOT NULL,
    model_used               INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT    NOT NULL,
    UNIQUE(case_id, sequence)
);
CREATE INDEX idx_verification_report_case
    ON verification_report(case_id, sequence);

CREATE TABLE error_taxonomy (
    code                TEXT    NOT NULL,
    version             INTEGER NOT NULL,
    name                TEXT    NOT NULL,
    definition          TEXT    NOT NULL,
    examples_json       TEXT    NOT NULL DEFAULT '[]',
    candidate_eligible  INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'active',
    created_at          TEXT    NOT NULL,
    PRIMARY KEY(code, version)
);

INSERT INTO error_taxonomy
    (code,version,name,definition,examples_json,candidate_eligible,status,created_at)
VALUES
    ('SOURCE_DISCOVERY',1,'来源定位','官网入口、替代入口或名单附件位置具有可复用定位模式','["官网栏目迁移","历史公示替代页"]',1,'active',datetime('now')),
    ('SOURCE_VERSION',1,'来源版本','公示、公告和最终名单之间存在可复用版本识别模式','["公示页不是最终名单","结果页晚于新闻稿"]',1,'active',datetime('now')),
    ('COVERAGE_PATTERN',1,'覆盖模式','名单由多赛道、分页或多个附件组成','["多赛道附件","分页名单"]',1,'active',datetime('now')),
    ('DOCUMENT_EXTRACTION',1,'文档抽取','PDF、OCR 或表格需要特定可复用读取策略','["扫描页需OCR","指定页表格抽取"]',1,'active',datetime('now')),
    ('FIELD_SEMANTICS',1,'字段语义','模板字段存在可复用错列、混填或语义识别模式','["推荐单位列混入人名","姓名字段混入机构"]',1,'active',datetime('now')),
    ('STANDARD_CORRECTION',1,'标准修正','人工确认了可复用的标准修正方法','["标准分隔符修正","字段归位方法"]',1,'active',datetime('now')),
    ('EVIDENCE_CONFLICT',1,'证据冲突','多个来源结论冲突，通常需要逐案人工判断','["两个官方页面名单不同"]',0,'active',datetime('now')),
    ('OTHER',1,'其他','无法稳定归入现有分类的个案','[]',0,'active',datetime('now'));

CREATE TABLE case_memory (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    status                   TEXT    NOT NULL DEFAULT 'candidate',
    category_code            TEXT    NOT NULL,
    taxonomy_version         INTEGER NOT NULL DEFAULT 1,
    resource_type            TEXT    NOT NULL DEFAULT '',
    field_code               TEXT    NOT NULL DEFAULT '',
    symptom_text             TEXT    NOT NULL,
    normalized_pattern       TEXT    NOT NULL,
    resolution               TEXT    NOT NULL,
    evidence_summary         TEXT    NOT NULL DEFAULT '',
    final_human_decision     TEXT    NOT NULL,
    source_case_id           INTEGER NOT NULL REFERENCES audit_case(id),
    applicable_from          TEXT    NOT NULL DEFAULT '',
    applicable_to            TEXT    NOT NULL DEFAULT '',
    occurrence_count         INTEGER NOT NULL DEFAULT 1,
    fingerprint              TEXT    NOT NULL UNIQUE,
    created_by               TEXT    NOT NULL,
    approved_by              TEXT    NOT NULL DEFAULT '',
    merged_into_id           INTEGER REFERENCES case_memory(id),
    state_version            INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL,
    FOREIGN KEY(category_code, taxonomy_version)
        REFERENCES error_taxonomy(code, version)
);
CREATE INDEX idx_case_memory_retrieval
    ON case_memory(status, resource_type, field_code, category_code);

CREATE TABLE case_memory_source (
    memory_id   INTEGER NOT NULL REFERENCES case_memory(id),
    case_id     INTEGER NOT NULL REFERENCES audit_case(id),
    linked_at   TEXT    NOT NULL,
    PRIMARY KEY(memory_id, case_id)
);
CREATE INDEX idx_case_memory_source_case
    ON case_memory_source(case_id, memory_id);
