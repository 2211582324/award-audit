-- award-audit 台账四张表（M2）。核心原则：永不物理覆盖、永不物理删除；改数据=新版本+旧失效+审计。

-- 导入批次：每次导入一批文件，可整批预览/回滚
CREATE TABLE IF NOT EXISTS import_batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,                    -- 批次名，如 提交-27
    source      TEXT    NOT NULL DEFAULT 'excel',    -- 来源：excel / 爬取
    imported_at TEXT    NOT NULL,
    importer    TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT '暂存',      -- 暂存/审核中/可入库/已入库/已回滚
    n_files     INTEGER NOT NULL DEFAULT 0,
    n_rows      INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''
);

-- 待审记录：结构化后的每行，带去重键与校验结果
CREATE TABLE IF NOT EXISTS staging_record (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES import_batch(id),
    file          TEXT    NOT NULL,
    sheet         TEXT    NOT NULL DEFAULT '',
    row_no        INTEGER NOT NULL,
    table_code    TEXT    NOT NULL,
    resource_code TEXT    NOT NULL DEFAULT '',
    dedup_key     TEXT    NOT NULL DEFAULT '',
    data_json     TEXT    NOT NULL,                  -- 字段代码->值
    check_status  TEXT    NOT NULL DEFAULT 'pass',   -- pass/warn/fail
    issues_json   TEXT    NOT NULL DEFAULT '[]',
    review_status TEXT    NOT NULL DEFAULT '待复核',   -- 待复核/通过/打回
    reviewer      TEXT    NOT NULL DEFAULT '',
    reviewed_at   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_staging_batch ON staging_record(batch_id);
CREATE INDEX IF NOT EXISTS idx_staging_key   ON staging_record(dedup_key);

-- 正式库：版本化。改一条=新增 version，旧版本 is_current=0、valid_to 置时间
CREATE TABLE IF NOT EXISTS record (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_key    TEXT    NOT NULL,                -- 去重键（业务主键）
    table_code      TEXT    NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    is_current      INTEGER NOT NULL DEFAULT 1,
    valid_from      TEXT    NOT NULL,
    valid_to        TEXT,
    source_batch_id INTEGER REFERENCES import_batch(id),
    data_json       TEXT    NOT NULL,
    created_by      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_record_key ON record(business_key, is_current);

-- 审计溯源：任何变更记字段级 diff + 原因 + 操作人 + 时间
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    INTEGER,
    business_key TEXT    NOT NULL DEFAULT '',
    action       TEXT    NOT NULL,                   -- create/correct/invalidate/rollback/promote
    diff_json    TEXT    NOT NULL DEFAULT '{}',       -- {字段:{old,new}}
    reason       TEXT    NOT NULL DEFAULT '',
    operator     TEXT    NOT NULL DEFAULT '',
    ts           TEXT    NOT NULL,
    batch_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_key ON audit_log(business_key);

-- L5 联网核对结论（M4）：一个资源项一条（资源项级证据链），进复核台「联网核对」页终审。
-- 与 staging_record（逐行数据）分开：联网核对是对整个资源项的核对判断，不是某一行。
CREATE TABLE IF NOT EXISTS audit_result (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES import_batch(id),
    resource_code     TEXT    NOT NULL,
    award_name        TEXT    NOT NULL DEFAULT '',
    year              TEXT    NOT NULL DEFAULT '',
    verdict           TEXT    NOT NULL DEFAULT '无法核对',  -- 一致/疑似缺漏/疑似多采/基本一致（需人工抽核）/来源年份不符/无法核对
    confidence        TEXT    NOT NULL DEFAULT 'low',       -- high/medium/low
    triage            TEXT    NOT NULL DEFAULT 'manual',    -- 分诊桶 auto_pass/review/manual（由 verdict+confidence 派生，见 core.models.triage）
    reason_codes_json TEXT    NOT NULL DEFAULT '[]',        -- 降级/成因码（"为什么没把握"的机器可读留痕）
    source_kind       TEXT    NOT NULL DEFAULT 'none',      -- excel/page/image/none
    source_url        TEXT    NOT NULL DEFAULT '',
    page_year         TEXT    NOT NULL DEFAULT '',
    extracted_count   INTEGER NOT NULL DEFAULT 0,
    submitted_count   INTEGER NOT NULL DEFAULT 0,
    missing_json      TEXT    NOT NULL DEFAULT '[]',        -- 官网有、提交无（疑漏采）
    extra_json        TEXT    NOT NULL DEFAULT '[]',        -- 提交有、官网无（疑多采）
    source_urls_json  TEXT    NOT NULL DEFAULT '[]',        -- 采集清单登记的官网网址（人工入口）
    found_assets_json TEXT    NOT NULL DEFAULT '[]',        -- 抓取中发现的名单文件/图片（可直接打开）
    evidence_json     TEXT    NOT NULL DEFAULT '[]',        -- 过程证据链
    notes             TEXT    NOT NULL DEFAULT '',
    review_status     TEXT    NOT NULL DEFAULT '待复核',      -- 待复核/通过/打回
    reviewer          TEXT    NOT NULL DEFAULT '',
    reviewed_at       TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_result_batch ON audit_result(batch_id);
