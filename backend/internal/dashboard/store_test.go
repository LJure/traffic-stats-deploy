package dashboard

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"traffic-stats/backend/internal/testfixture"
)

var snapshotEpoch = testfixture.SnapshotEpoch

// testStore 使用脱敏临时数据库和确定性的北京时间时钟，保证本地与 CI 断言可重复。
func testStore(t *testing.T) *Store {
	t.Helper()
	path := testfixture.CreateTrafficDatabase(t)
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	store.now = func() time.Time { return time.Unix(snapshotEpoch, 0).In(beijing) }
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// TestStatusReportsDatabaseMetadata 验证每次请求读取数据库文件大小，并在无存储写入的情况下参与健康判断。
func TestStatusReportsDatabaseMetadata(t *testing.T) {
	store := testStore(t)
	info, err := os.Stat(store.databaseFile)
	if err != nil {
		t.Fatal(err)
	}

	initial, err := store.Status(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if initial.LastSuccess == 0 || initial.DatabaseBytes != info.Size() || !initial.DatabaseAvailable {
		t.Fatalf("unexpected initial status: %#v", initial)
	}
	store.now = func() time.Time { return time.Unix(initial.LastSuccess+60, 0).In(beijing) }
	status, err := store.Status(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !status.Healthy || status.NextSync != nextMinute(store.now()) {
		t.Fatalf("unexpected healthy status: %#v", status)
	}
	store.databaseFile = filepath.Join(t.TempDir(), "missing.sqlite3")
	status, err = store.Status(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if status.DatabaseAvailable || status.Healthy || status.DatabaseBytes != 0 {
		t.Fatalf("unexpected missing-database status: %#v", status)
	}
}

// TestSnapshotDashboardMatchesFrozenDailyTotals 防止 API 汇总流量发生聚合回归。
func TestSnapshotDashboardMatchesFrozenDailyTotals(t *testing.T) {
	store := testStore(t)
	result, err := store.Dashboard(context.Background(), "today", "all", "", "", snapshotEpoch)
	if err != nil {
		t.Fatal(err)
	}
	if result.Upload != 140 || result.Download != 1400 || result.Total != 1540 {
		t.Fatalf("unexpected totals: upload=%d download=%d total=%d", result.Upload, result.Download, result.Total)
	}
	if len(result.Series) != 24 || result.Series[0].Label != "00:00" || result.Series[23].Label != "23:00" {
		t.Fatalf("unexpected series: %#v", result.Series)
	}
	if len(result.Devices) != 4 {
		t.Fatalf("expected four devices, got %d", len(result.Devices))
	}
	if len(result.DeviceSeries) != 4 {
		t.Fatalf("expected four device series, got %d", len(result.DeviceSeries))
	}
	for _, series := range result.DeviceSeries {
		if len(series.Series) != 24 || series.Series[0].Label != "00:00" || series.Series[23].Label != "23:00" {
			t.Fatalf("unexpected device series: %#v", series)
		}
	}
	for index, point := range result.Series {
		var upload, download int64
		for _, series := range result.DeviceSeries {
			upload += series.Series[index].Upload
			download += series.Series[index].Download
		}
		if point.Upload != upload || point.Download != download {
			t.Fatalf("hour %d does not match device totals: point=%#v upload=%d download=%d", index, point, upload, download)
		}
	}
}

// TestDevicesUseStableCompositeIdentity 确保重命名 3x-ui 元数据不会拆分设备身份。
func TestDevicesUseStableCompositeIdentity(t *testing.T) {
	store := testStore(t)
	devices, err := store.Devices(context.Background(), snapshotEpoch)
	if err != nil {
		t.Fatal(err)
	}
	if len(devices) != 4 {
		t.Fatalf("expected four devices, got %d", len(devices))
	}
	for _, device := range devices {
		if device.ID == "" || device.ClientID == 0 || device.InboundID == 0 {
			t.Fatalf("invalid device: %#v", device)
		}
	}
}

// TestUnknownDeviceFallsBack 验证未知设备仍会回退到全部设备仪表盘。
func TestUnknownDeviceFallsBack(t *testing.T) {
	store := testStore(t)
	result, err := store.Dashboard(context.Background(), "today", "missing:1", "", "", snapshotEpoch)
	if err != nil {
		t.Fatal(err)
	}
	if result.Range != "today" || result.Selected != "all" {
		t.Fatalf("unexpected fallback: %#v", result)
	}
}

// TestSingleDayCustomRangeUsesHourlyBuckets 验证单日自选范围保留与今天相同的小时粒度。
func TestSingleDayCustomRangeUsesHourlyBuckets(t *testing.T) {
	store := testStore(t)
	result, err := store.Dashboard(context.Background(), "custom", "all", "2026-07-14", "2026-07-14", snapshotEpoch)
	if err != nil {
		t.Fatal(err)
	}
	if result.Range != "custom" || len(result.Series) != 24 || result.Series[0].Label != "00:00" || result.Series[23].Label != "23:00" {
		t.Fatalf("unexpected custom series: %#v", result)
	}
	for _, series := range result.DeviceSeries {
		if len(series.Series) != 24 || series.Series[0].Label != "00:00" || series.Series[23].Label != "23:00" {
			t.Fatalf("unexpected custom device series: %#v", series)
		}
	}
}

// TestInvalidRangeIsRejected 验证不支持或超出窗口的自选日期范围会返回稳定的校验错误。
func TestInvalidRangeIsRejected(t *testing.T) {
	store := testStore(t)
	for _, request := range []struct {
		rangeKind string
		start     string
		end       string
	}{
		{rangeKind: "not-a-range"},
		{rangeKind: "custom", start: "2026-06-14", end: "2026-07-14"},
		{rangeKind: "custom", start: "2026-07-15", end: "2026-07-15"},
	} {
		_, err := store.Dashboard(context.Background(), request.rangeKind, "all", request.start, request.end, snapshotEpoch)
		if !errors.Is(err, ErrInvalidRange) {
			t.Fatalf("expected invalid range for %#v, got %v", request, err)
		}
	}
}
