-- 0007 统一审核编排：(资源项码,年) 贯穿 + 显式执行状态 + 原子并发 + 资源项最终结论门禁。
-- 数据感知：对已填充旧库先解冲突/回填/标记，再建唯一索引；空库（测试/新 tmp 库）全部为 no-op。

-- ① staging 业务年份：入库门禁按 (码,年) 匹配，不在 promote 时临时猜
ALTER TABLE staging_record ADD COLUMN year TEXT NOT NULL DEFAULT '';

-- ② 需重新导入标记：旧数据无法满足新不变式时置 1，禁止继续审核与入库（fail-closed）
ALTER TABLE import_batch ADD COLUMN needs_reimport INTEGER NOT NULL DEFAULT 0;

-- ③ 批次阶段执行事实 + 租约锁：导入含多次提交、半途可失败，执行事实必须落表（不靠记录有无猜）；
--    同批 M4/M5 不得交叉（claim 经本表租约）。
CREATE TABLE IF NOT EXISTS batch_stage_run (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES import_batch(id),
    stage            TEXT    NOT NULL,                     -- local / m4 / m5
    status           TEXT    NOT NULL DEFAULT 'pending',   -- pending/running/done/failed/partial
    attempt          INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT    NOT NULL DEFAULT '',
    error_message    TEXT    NOT NULL DEFAULT '',
    lease_owner      TEXT    NOT NULL DEFAULT '',
    lease_expires_at TEXT    NOT NULL DEFAULT '',
    state_version    INTEGER NOT NULL DEFAULT 1,
    started_at       TEXT    NOT NULL DEFAULT '',
    finished_at      TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_stage ON batch_stage_run(batch_id, stage);

-- ④ 资源项级(m4)执行状态真相 + 租约 + 当前结果指针：
--    重试会追加多条 audit_result（永不删）；门禁与建案只认 current_result_id，旧结果仅留审计。
CREATE TABLE IF NOT EXISTS audit_stage_item (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES import_batch(id),
    stage             TEXT    NOT NULL DEFAULT 'm4',
    resource_code     TEXT    NOT NULL,                    -- 归一化(zfill 8)
    year              TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'pending',   -- pending/running/done/failed/skipped
    attempt           INTEGER NOT NULL DEFAULT 0,
    current_result_id INTEGER REFERENCES audit_result(id),
    lease_owner       TEXT    NOT NULL DEFAULT '',
    lease_expires_at  TEXT    NOT NULL DEFAULT '',
    state_version     INTEGER NOT NULL DEFAULT 1,
    error_code        TEXT    NOT NULL DEFAULT '',
    error_message     TEXT    NOT NULL DEFAULT '',
    started_at        TEXT    NOT NULL DEFAULT '',
    finished_at       TEXT    NOT NULL DEFAULT '',
    updated_at        TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_stage_item
    ON audit_stage_item(batch_id, stage, resource_code, year);

-- ⑤ 导入溯源：源目录 + 文件哈希 + 序列化版本 + 模板/台账指纹；L5 读取时重验，漂移即 fail-closed。
CREATE TABLE IF NOT EXISTS batch_import_context (
    batch_id             INTEGER PRIMARY KEY REFERENCES import_batch(id),
    source_folder        TEXT    NOT NULL DEFAULT '',
    files_json           TEXT    NOT NULL DEFAULT '[]',    -- [{file_name, path, sha256}]
    check_result_json    TEXT    NOT NULL DEFAULT '{}',    -- 完整 BatchResult（含文件级问题）
    context_version      INTEGER NOT NULL DEFAULT 1,
    template_fingerprint TEXT    NOT NULL DEFAULT '',
    ledger_fingerprint   TEXT    NOT NULL DEFAULT '',
    created_at           TEXT    NOT NULL DEFAULT ''
);

-- ⑥ Web 任务并发防重（原子）：先清旧库重复活跃任务，再建部分唯一索引。
--    (a) 同 (kind,batch) 只留最新活跃
UPDATE audit_job SET status='cancelled', updated_at=datetime('now')
 WHERE status IN ('queued','running') AND batch_id IS NOT NULL AND id NOT IN (
     SELECT MAX(id) FROM audit_job
      WHERE status IN ('queued','running') AND batch_id IS NOT NULL
      GROUP BY kind, batch_id
 );
--    (b) 同批 audit_batch/review_batch 只留最新活跃（禁 M4/M5 交叉并发）
UPDATE audit_job SET status='cancelled', updated_at=datetime('now')
 WHERE status IN ('queued','running') AND batch_id IS NOT NULL
   AND kind IN ('audit_batch','review_batch') AND id NOT IN (
     SELECT MAX(id) FROM audit_job
      WHERE status IN ('queued','running') AND batch_id IS NOT NULL
        AND kind IN ('audit_batch','review_batch')
      GROUP BY batch_id
 );
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_job
    ON audit_job(kind, batch_id)
    WHERE status IN ('queued','running') AND batch_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_batch_stage
    ON audit_job(batch_id)
    WHERE kind IN ('audit_batch','review_batch')
      AND status IN ('queued','running') AND batch_id IS NOT NULL;

-- ⑦ 案件唯一约束去 trigger_key → 一 (批,码,年) 一活跃案：
--    旧库同(批,码,年)多活跃案先把 trigger 合并到最早案件，其余降终态留痕。
--    trigger_codes_json 为空的早期案件以 trigger_key 作为可恢复的单个 trigger。
DROP INDEX IF EXISTS uq_audit_case_active_trigger;
UPDATE audit_case
   SET resource_code=printf('%08d', CAST(resource_code AS INTEGER))
 WHERE trim(resource_code)<>''
   AND resource_code NOT GLOB '*[^0-9]*'
   AND length(resource_code)<8;
UPDATE audit_case
   SET trigger_codes_json = COALESCE((
           SELECT json_group_array(trigger_code) FROM (
               SELECT DISTINCT trigger_code FROM (
                   SELECT trim(CAST(j.value AS TEXT)) AS trigger_code
                     FROM audit_case AS source_case,
                          json_each(CASE WHEN json_valid(source_case.trigger_codes_json)
                                         THEN source_case.trigger_codes_json ELSE '[]' END) AS j
                    WHERE source_case.batch_id=audit_case.batch_id
                      AND source_case.resource_code=audit_case.resource_code
                      AND source_case.year=audit_case.year
                      AND source_case.status IN ('queued','running','waiting_human')
                   UNION
                   SELECT trim(source_case.trigger_key) AS trigger_code
                     FROM audit_case AS source_case
                    WHERE source_case.batch_id=audit_case.batch_id
                      AND source_case.resource_code=audit_case.resource_code
                      AND source_case.year=audit_case.year
                      AND source_case.status IN ('queued','running','waiting_human')
                      AND trim(source_case.trigger_key)<>''
               ) WHERE trigger_code<>'' ORDER BY trigger_code
           )
       ), trigger_codes_json),
       trigger_key = COALESCE((
           SELECT group_concat(trigger_code, '|') FROM (
               SELECT DISTINCT trigger_code FROM (
                   SELECT trim(CAST(j.value AS TEXT)) AS trigger_code
                     FROM audit_case AS source_case,
                          json_each(CASE WHEN json_valid(source_case.trigger_codes_json)
                                         THEN source_case.trigger_codes_json ELSE '[]' END) AS j
                    WHERE source_case.batch_id=audit_case.batch_id
                      AND source_case.resource_code=audit_case.resource_code
                      AND source_case.year=audit_case.year
                      AND source_case.status IN ('queued','running','waiting_human')
                   UNION
                   SELECT trim(source_case.trigger_key) AS trigger_code
                     FROM audit_case AS source_case
                    WHERE source_case.batch_id=audit_case.batch_id
                      AND source_case.resource_code=audit_case.resource_code
                      AND source_case.year=audit_case.year
                      AND source_case.status IN ('queued','running','waiting_human')
                      AND trim(source_case.trigger_key)<>''
               ) WHERE trigger_code<>'' ORDER BY trigger_code
           )
       ), trigger_key),
       state_version=state_version+1,
       updated_at=datetime('now')
 WHERE id IN (
     SELECT MIN(id) FROM audit_case
      WHERE status IN ('queued','running','waiting_human')
      GROUP BY batch_id, resource_code, year HAVING COUNT(*) > 1
 );
-- 只有无法恢复出任何 trigger 的冲突组才要求重新导入。
UPDATE import_batch SET needs_reimport=1 WHERE id IN (
    SELECT batch_id FROM audit_case
     WHERE status IN ('queued','running','waiting_human')
     GROUP BY batch_id, resource_code, year
    HAVING COUNT(*) > 1
       AND SUM(CASE WHEN trim(trigger_key)<>'' THEN 1 ELSE 0 END)=0
);
UPDATE audit_case SET status='failed',
       last_error='superseded_by_0007_year_identity', updated_at=datetime('now')
 WHERE status IN ('queued','running','waiting_human') AND id NOT IN (
     SELECT MIN(id) FROM audit_case
      WHERE status IN ('queued','running','waiting_human')
      GROUP BY batch_id, resource_code, year
 );
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_case_active
    ON audit_case(batch_id, resource_code, year)
    WHERE status IN ('queued','running','waiting_human');

-- ⑧ 旧批次 staging.year：先从常见业务年份字段可靠回填；仍为空或非四位年份才标 needs_reimport。
UPDATE staging_record
   SET year=trim(CAST(COALESCE(
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.PDNY'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.HJND'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.HJNF'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.ND'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.FBND'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.PZND'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.year'),
       json_extract(CASE WHEN json_valid(data_json) THEN data_json ELSE '{}' END, '$.年份'),
       ''
   ) AS TEXT))
 WHERE year='';
UPDATE staging_record SET year=''
 WHERE year<>'' AND (length(year)<>4 OR year NOT GLOB '[12][0-9][0-9][0-9]');
UPDATE import_batch SET needs_reimport=1
 WHERE id IN (SELECT DISTINCT batch_id FROM staging_record WHERE year='');
