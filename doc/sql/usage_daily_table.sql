-- 每日用量表：存放 /external/bill 查到的「按天用量」。
-- 注意：COLLATE 必须与 device/data 表一致（都是 utf8mb4_general_ci），
--      否则 MySQL 26.7 的默认 0900_ai_ci 会在 join 时报 1267 排序规则冲突。
-- 与 data 表分开，避免污染示数语义（data.total_reading 是表底示数，不是当日用量）。
CREATE TABLE IF NOT EXISTS `usage_daily` (
  `id` INT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `device_id` VARCHAR(32) NOT NULL COMMENT '设备id（对应 device.id）',
  `day` DATE NOT NULL COMMENT '日期',
  `usage_amount` DECIMAL(12,4) DEFAULT NULL COMMENT '当日用量（电：kW·h / 水：m³）',
  `use_money` DECIMAL(12,4) DEFAULT NULL COMMENT '当日金额（若有）',
  `last_two_day_usage` DECIMAL(12,4) DEFAULT NULL COMMENT '接口同时返回的「前一日用量」，仅供核对',
  `source` VARCHAR(16) NOT NULL DEFAULT 'bill' COMMENT '数据来源：bill=外部账单接口 / snapshot=每日快照',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_device_day` (`device_id`, `day`),
  KEY `idx_day` (`day`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='按天的用量统计（来自 /external/bill）';
