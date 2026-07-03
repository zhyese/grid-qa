-- ============================================================
-- 问答缓存表（Redis LRU 淘汰持久化 → MySQL 二级缓存）
-- 查询路径: Redis(L1 hit) → MySQL(L2 hit) → LLM(L3 miss)
-- 执行方式: docker compose exec mysql mysql -uroot -proot123456 grid_qa < migration_qa_cache.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS qa_cache (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    cache_key           VARCHAR(512) NOT NULL COMMENT '缓存键: qa:{model}:{normalized}',
    model_type          VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'LLM 模型',
    query_hash          CHAR(32) NOT NULL COMMENT 'MD5(cache_key) 精确匹配',
    query_normalized    TEXT NOT NULL COMMENT '归一化后问题文本',
    query_original      TEXT NOT NULL COMMENT '用户原始问题',
    answer              MEDIUMTEXT NOT NULL COMMENT '问答结果 JSON（与 Redis 缓存同结构）',
    retrieval_sources   JSON DEFAULT NULL COMMENT '引用来源 JSON 数组',
    confidence          VARCHAR(16) NOT NULL DEFAULT 'high' COMMENT '可信度: high/medium/low/refused',
    hallucination_rate  FLOAT NOT NULL DEFAULT 0.0 COMMENT '幻觉率',
    hit_count           INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '累计命中次数（热度）',
    ttl_seconds         INT UNSIGNED NOT NULL DEFAULT 259200 COMMENT 'TTL 秒数（默认 3 天）',
    expires_at          DATETIME NOT NULL COMMENT '过期时间 = created_at + ttl_seconds',
    last_hit_at         DATETIME DEFAULT NULL COMMENT '最后命中时间',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    is_deleted          TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删标记',

    UNIQUE KEY `uk_query_hash` (`query_hash`),
    KEY `idx_model_type` (`model_type`),
    KEY `idx_expires_at` (`expires_at`),
    KEY `idx_hit_count` (`hit_count`),
    KEY `idx_updated_at` (`updated_at`),
    KEY `idx_is_deleted` (`is_deleted`),
    FULLTEXT INDEX `ft_query_normalized` (`query_normalized`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='问答缓存：Redis LRU 淘汰后 MySQL 冷备，三级缓存 L2 层';


-- ============================================================
-- MySQL Event Scheduler：每天凌晨 3:00 清理过期/冷/软删数据
-- 应用层 cache_persist.cleanup_loop() 每 6h 兜底执行
-- ============================================================
SET GLOBAL event_scheduler = ON;

DROP EVENT IF EXISTS ev_cleanup_qa_cache;

DELIMITER //
CREATE EVENT ev_cleanup_qa_cache
ON SCHEDULE EVERY 1 DAY
STARTS CONCAT(CURRENT_DATE, ' 03:00:00')
DO BEGIN
    -- ① 已过期的缓存
    DELETE FROM qa_cache WHERE expires_at < NOW() AND is_deleted = 0 LIMIT 5000;
    -- ② 3 天未命中的冷数据
    DELETE FROM qa_cache WHERE updated_at < NOW() - INTERVAL 3 DAY AND is_deleted = 0 LIMIT 5000;
    -- ③ 软删超过 7 天的物理删除
    DELETE FROM qa_cache WHERE is_deleted = 1 AND updated_at < NOW() - INTERVAL 7 DAY LIMIT 1000;
END //
DELIMITER ;
