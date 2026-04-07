-- 宏观诊断结果表：存储自动触发的诊断结果
CREATE TABLE IF NOT EXISTS macro_diagnosis_results (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,          -- 触发原因：'每10章检查点' / '第N幕完成' / 'manual'
    trait TEXT NOT NULL,                   -- 扫描的目标人设标签
    conflict_tags TEXT,                    -- 使用的冲突标签（JSON 数组）
    breakpoints TEXT NOT NULL DEFAULT '[]', -- 扫描结果（JSON 数组）
    breakpoint_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed', -- pending / completed / failed
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_macro_diagnosis_novel ON macro_diagnosis_results(novel_id);
CREATE INDEX IF NOT EXISTS idx_macro_diagnosis_created ON macro_diagnosis_results(novel_id, created_at DESC);
