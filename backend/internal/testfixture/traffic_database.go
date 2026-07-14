// Package testfixture 提供不含线上数据的最小统计数据库，用于跨包测试和 CI。
package testfixture

import (
	"database/sql"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

var beijing = time.FixedZone("Asia/Shanghai", 8*60*60)

// SnapshotEpoch 是夹具中的固定采集截止时刻，所有测试据此获得可重复的时间范围。
var SnapshotEpoch = time.Date(2026, time.July, 14, 12, 0, 0, 0, beijing).Unix()

// CreateTrafficDatabase 在测试临时目录创建脱敏的最小采集数据库，并返回其文件路径。
func CreateTrafficDatabase(t testing.TB) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "traffic.sqlite3")
	database, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if _, err := database.Exec(`
		CREATE TABLE samples (
			captured_at INTEGER NOT NULL,
			client_id INTEGER NOT NULL,
			inbound_id INTEGER NOT NULL,
			email TEXT NOT NULL,
			label TEXT NOT NULL,
			up_bytes INTEGER NOT NULL,
			down_bytes INTEGER NOT NULL,
			PRIMARY KEY (captured_at, client_id, inbound_id)
		);
		CREATE TABLE daily_usage (
			day TEXT NOT NULL,
			client_id INTEGER NOT NULL,
			inbound_id INTEGER NOT NULL,
			email TEXT NOT NULL,
			label TEXT NOT NULL,
			up_bytes INTEGER NOT NULL DEFAULT 0,
			down_bytes INTEGER NOT NULL DEFAULT 0,
			PRIMARY KEY (day, client_id, inbound_id)
		);
		CREATE TABLE collector_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
	`); err != nil {
		t.Fatal(err)
	}

	current := time.Unix(SnapshotEpoch, 0).In(beijing)
	// Truncate 会按 UTC 边界截断，因此显式构造北京时间的自然日零点。
	day := time.Date(current.Year(), current.Month(), current.Day(), 0, 0, 0, 0, beijing)
	// 每台设备在当天前保留一条计数器，用于验证首个小时桶的增量不会丢失。
	for id := int64(1); id <= 4; id++ {
		email := "device-" + strconv.FormatInt(id, 10)
		label := []string{"Windows", "iOS", "Android", "HarmonyOS"}[id-1]
		baseUp, baseDown := id*100, id*1_000
		rows := [][3]int64{
			{day.Add(-time.Minute).Unix(), baseUp, baseDown},
			{day.Add(time.Minute).Unix(), baseUp + 10, baseDown + 100},
			{day.Add(time.Hour + time.Minute).Unix(), baseUp + 30, baseDown + 300},
			{day.Add(2*time.Hour + time.Minute).Unix(), baseUp + 35, baseDown + 350},
		}
		for _, row := range rows {
			if _, err := database.Exec(`INSERT INTO samples (captured_at,client_id,inbound_id,email,label,up_bytes,down_bytes) VALUES (?,?,?,?,?,?,?)`, row[0], id, 1, email, label, row[1], row[2]); err != nil {
				t.Fatal(err)
			}
		}
		// 日汇总与分钟样本的累积增量一致，覆盖仪表盘的日粒度查询路径。
		if _, err := database.Exec(`INSERT INTO daily_usage (day,client_id,inbound_id,email,label,up_bytes,down_bytes) VALUES (?,?,?,?,?,?,?)`, day.Format("2006-01-02"), id, 1, email, label, 35, 350); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := database.Exec(`INSERT INTO collector_state (key, value) VALUES ('last_success', ?)`, strconv.FormatInt(SnapshotEpoch, 10)); err != nil {
		t.Fatal(err)
	}
	return path
}
