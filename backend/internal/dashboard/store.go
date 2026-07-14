package dashboard

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

var beijing = time.FixedZone("Asia/Shanghai", 8*60*60)

const maximumCollectorAge = 3 * time.Minute

// ErrInvalidRange 表示 API 请求的时间范围不受支持或不符合统计策略。
var ErrInvalidRange = errors.New("invalid range")

// Store 封装采集数据库的只读访问，以及生成统计数据所需的运行时依赖。
type Store struct {
	db           *sql.DB
	now          func() time.Time
	databaseFile string
}

// Device 表示由客户端 ID 与入站 ID 共同唯一确定的一台统计设备。
type Device struct {
	// ID 是供 API 和前端选择器使用的复合稳定标识。
	ID        string `json:"id"`
	ClientID  int64  `json:"clientId"`
	InboundID int64  `json:"inboundId"`
	Email     string `json:"email"`
	Label     string `json:"label"`
}

// Range 表示按北京时间计算、首尾均包含在内的自然日范围。
type Range struct {
	Kind  string
	Start time.Time
	End   time.Time
}

// Point 是一个时间桶内的上、下行流量增量。
type Point struct {
	Label    string `json:"label"`
	Upload   int64  `json:"upload"`
	Download int64  `json:"download"`
}

// DeviceUsage 是某台设备在选定范围内的累计流量，用于设备排行。
type DeviceUsage struct {
	Device
	Upload   int64 `json:"upload"`
	Download int64 `json:"download"`
}

// DeviceSeries 将设备信息与可单独显示的趋势序列关联起来。
type DeviceSeries struct {
	Device
	Series []Point `json:"series"`
}

// Dashboard 是一次仪表盘刷新所需的完整聚合结果。
type Dashboard struct {
	Range string `json:"range"`
	Start string `json:"start"`
	End   string `json:"end"`
	// Snapshot 是本次查询使用的采集快照截止时间，保证多接口读取同一批数据。
	Snapshot     int64          `json:"snapshot"`
	Selected     string         `json:"selected"`
	Upload       int64          `json:"upload"`
	Download     int64          `json:"download"`
	Total        int64          `json:"total"`
	CurrentRate  float64        `json:"currentRate"`
	Series       []Point        `json:"series"`
	Devices      []DeviceUsage  `json:"devices"`
	DeviceSeries []DeviceSeries `json:"deviceSeries"`
}

// ServiceStatus 描述采集服务的新鲜度以及采集数据库文件的当前状态。
type ServiceStatus struct {
	LastSuccess       int64 `json:"lastSuccess"`
	NextSync          int64 `json:"nextSync"`
	DatabaseBytes     int64 `json:"databaseBytes"`
	DatabaseAvailable bool  `json:"databaseAvailable"`
	Healthy           bool  `json:"healthy"`
}

// Open 为采集数据库创建连接数受限的 SQLite 只读连接池。
func Open(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("database path is required")
	}
	absolutePath, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("resolve database path: %w", err)
	}
	normalizedPath := filepath.ToSlash(absolutePath)
	if runtime.GOOS == "windows" {
		// Windows 盘符路径在 SQLite file URI 中需要以额外的斜线开头。
		normalizedPath = "/" + normalizedPath
	}
	// 只读模式避免仪表盘干扰采集；忙等待降低采集事务瞬间占用数据库时的失败概率。
	dsn := "file://" + normalizedPath + "?mode=ro&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(4)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db, now: time.Now, databaseFile: absolutePath}, nil
}

// Close 释放统计存储持有的全部数据库连接。
func (s *Store) Close() error { return s.db.Close() }

// Devices 返回每个稳定客户端与入站组合对应的最新展示记录。
func (s *Store) Devices(ctx context.Context, snapshot int64) ([]Device, error) {
	filter, args := "", []any{}
	if snapshot > 0 {
		// 与仪表盘快照共用截止点，避免设备列表领先于趋势数据。
		filter, args = "WHERE captured_at <= ?", []any{snapshot}
	}
	rows, err := s.db.QueryContext(ctx, fmt.Sprintf(`
		WITH latest AS (
			SELECT client_id, inbound_id, MAX(captured_at) captured_at
			FROM samples %s GROUP BY client_id, inbound_id
		)
		SELECT s.client_id, s.inbound_id, s.email, s.label
		FROM samples s JOIN latest l
		ON s.client_id=l.client_id AND s.inbound_id=l.inbound_id AND s.captured_at=l.captured_at
		ORDER BY s.label COLLATE NOCASE`, filter), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var devices []Device
	for rows.Next() {
		var d Device
		if err := rows.Scan(&d.ClientID, &d.InboundID, &d.Email, &d.Label); err != nil {
			return nil, err
		}
		d.ID = deviceID(d.ClientID, d.InboundID)
		devices = append(devices, d)
	}
	return devices, rows.Err()
}

// Status 返回采集新鲜度，以及不落盘的采集数据库文件实时大小。
func (s *Store) Status(ctx context.Context) (ServiceStatus, error) {
	now := s.now()
	status := ServiceStatus{NextSync: nextMinute(now)}
	var raw string
	err := s.db.QueryRowContext(ctx, "SELECT value FROM collector_state WHERE key='last_success'").Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		// 尚无成功采集记录时仍反馈文件状态，便于区分首次启动和文件不可用。
		status.DatabaseBytes, status.DatabaseAvailable = fileSize(s.databaseFile)
		return status, nil
	}
	if err != nil {
		return ServiceStatus{}, err
	}
	last, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return ServiceStatus{}, fmt.Errorf("invalid collector last_success: %w", err)
	}
	status.LastSuccess = last
	// 文件大小只读取元数据；健康状态同时要求采集新鲜且数据库文件可访问。
	status.DatabaseBytes, status.DatabaseAvailable = fileSize(s.databaseFile)
	status.Healthy = collectorFresh(last, now) && status.DatabaseAvailable
	return status, nil
}

// fileSize 仅读取文件元数据，以便返回当前数据库大小而不产生 SQLite 写入。
func fileSize(path string) (int64, bool) {
	if path == "" {
		return 0, false
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return 0, false
	}
	return info.Size(), true
}

// collectorFresh 判断最近一次成功采集是否仍在三分钟有效期内。
func collectorFresh(last int64, now time.Time) bool {
	age := now.Unix() - last
	return last > 0 && age >= 0 && age <= int64(maximumCollectorAge/time.Second)
}

// Dashboard 为一次仪表盘刷新构建汇总、总趋势、设备趋势、速率和排行数据。
func (s *Store) Dashboard(ctx context.Context, requestedRange, requestedDevice, requestedStart, requestedEnd string, snapshot int64) (Dashboard, error) {
	rangeValue, err := parseRange(requestedRange, requestedStart, requestedEnd, s.now().In(beijing))
	if err != nil {
		return Dashboard{}, err
	}
	devices, err := s.Devices(ctx, snapshot)
	if err != nil {
		return Dashboard{}, err
	}
	if len(devices) == 0 {
		return Dashboard{}, errors.New("no collected devices")
	}
	selected := devices
	selectedID := "all"
	if requestedDevice != "" && requestedDevice != "all" {
		for _, device := range devices {
			if device.ID == requestedDevice {
				selected = []Device{device}
				selectedID = device.ID
				break
			}
		}
	}

	// 已落盘的日汇总适用于完整自然日，当前快照所在日需按截止时刻重新计算。
	usage, err := s.dailyUsage(ctx, rangeValue, selected)
	if err != nil {
		return Dashboard{}, err
	}
	if snapshot > 0 {
		snapshotDay := time.Unix(snapshot, 0).In(beijing)
		if !snapshotDay.Before(rangeValue.Start) && !snapshotDay.After(rangeValue.End) {
			recalculated, err := s.snapshotDayUsage(ctx, snapshotDay, snapshot, selected)
			if err != nil {
				return Dashboard{}, err
			}
			usage[snapshotDay.Format("2006-01-02")] = recalculated
		}
	}

	points, byDevice, totalUpload, totalDownload := series(rangeValue, usage, selected)
	if usesHourlySeries(rangeValue) {
		// 今日和仅一天的自选范围保留小时粒度，覆盖先前生成的日粒度结果。
		points, byDevice, err = s.hourlySeries(ctx, rangeValue.Start, selected, snapshot)
		if err != nil {
			return Dashboard{}, err
		}
	}
	rate, err := s.currentRate(ctx, selected, snapshot)
	if err != nil {
		return Dashboard{}, err
	}
	deviceUsage := rankDevices(devices, usage)
	// 所有字段都基于同一个 snapshot，防止单次页面刷新出现前后不一致的数据。
	return Dashboard{
		Range: rangeValue.Kind, Start: rangeValue.Start.Format("2006-01-02"), End: rangeValue.End.Format("2006-01-02"),
		Snapshot: snapshot, Selected: selectedID, Upload: totalUpload, Download: totalDownload,
		Total: totalUpload + totalDownload, CurrentRate: rate, Series: points, Devices: deviceUsage, DeviceSeries: deviceSeries(selected, byDevice),
	}, nil
}

// usesHourlySeries 为今天和仅含一个自然日的自选范围保留小时粒度。
func usesHourlySeries(r Range) bool {
	return r.Kind == "today" || (r.Kind == "custom" && r.Start.Equal(r.End))
}

// counters 保存原始计数器计算得到的一组上、下行字节增量。
type counters struct{ upload, download int64 }

// dailyUsage 读取按日期和稳定设备标识分组的已落盘流量增量。
func (s *Store) dailyUsage(ctx context.Context, r Range, devices []Device) (map[string]map[string]counters, error) {
	clause, args := deviceClause(devices)
	args = append([]any{r.Start.Format("2006-01-02"), r.End.Format("2006-01-02")}, args...)
	rows, err := s.db.QueryContext(ctx, `SELECT day,client_id,inbound_id,SUM(up_bytes),SUM(down_bytes)
		FROM daily_usage WHERE day BETWEEN ? AND ? AND (`+clause+`) GROUP BY day,client_id,inbound_id`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]map[string]counters{}
	for rows.Next() {
		var day string
		var clientID, inboundID, up, down int64
		if err := rows.Scan(&day, &clientID, &inboundID, &up, &down); err != nil {
			return nil, err
		}
		if result[day] == nil {
			result[day] = map[string]counters{}
		}
		result[day][deviceID(clientID, inboundID)] = counters{up, down}
	}
	return result, rows.Err()
}

// snapshotDayUsage 用分钟采样重算指定快照截止时刻所在、尚未完成的自然日。
func (s *Store) snapshotDayUsage(ctx context.Context, day time.Time, snapshot int64, devices []Device) (map[string]counters, error) {
	start := time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, beijing)
	clause, args := deviceClause(devices)
	// 从当天开始前一小时读取，确保当天第一条样本也有可用于计算增量的前置计数器。
	args = append([]any{start.Add(-time.Hour).Unix(), snapshot}, args...)
	rows, err := s.db.QueryContext(ctx, `SELECT client_id,inbound_id,captured_at,up_bytes,down_bytes FROM samples
		WHERE captured_at >= ? AND captured_at <= ? AND (`+clause+`) ORDER BY client_id,inbound_id,captured_at`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]counters{}
	previous := map[string]counters{}
	for rows.Next() {
		var clientID, inboundID, capturedAt, up, down int64
		if err := rows.Scan(&clientID, &inboundID, &capturedAt, &up, &down); err != nil {
			return nil, err
		}
		key := deviceID(clientID, inboundID)
		if old, ok := previous[key]; ok && capturedAt >= start.Unix() {
			// 仅把落入目标日的相邻计数器差值计入结果，兼容设备侧计数器重置。
			value := result[key]
			value.upload += counterDelta(up, old.upload)
			value.download += counterDelta(down, old.download)
			result[key] = value
		}
		previous[key] = counters{up, down}
	}
	return result, rows.Err()
}

// currentRate 汇总每台已选设备最近两条样本之间的正向字节增量速率。
func (s *Store) currentRate(ctx context.Context, devices []Device, snapshot int64) (float64, error) {
	var total float64
	for _, device := range devices {
		query := "SELECT captured_at,up_bytes,down_bytes FROM samples WHERE client_id=? AND inbound_id=?"
		args := []any{device.ClientID, device.InboundID}
		if snapshot > 0 {
			query += " AND captured_at<=?"
			args = append(args, snapshot)
		}
		query += " ORDER BY captured_at DESC LIMIT 2"
		rows, err := s.db.QueryContext(ctx, query, args...)
		if err != nil {
			return 0, err
		}
		var values []struct{ at, up, down int64 }
		for rows.Next() {
			var value struct{ at, up, down int64 }
			if err := rows.Scan(&value.at, &value.up, &value.down); err != nil {
				rows.Close()
				return 0, err
			}
			values = append(values, value)
		}
		if err := rows.Close(); err != nil {
			return 0, err
		}
		if len(values) == 2 && values[0].at > values[1].at {
			// 使用采样实际间隔计算速率，而非假设固定采集周期。
			total += float64(counterDelta(values[0].up, values[1].up)+counterDelta(values[0].down, values[1].down)) / float64(values[0].at-values[1].at)
		}
	}
	return total, nil
}

// hourlySeries 将分钟样本转换为按北京时间分桶的总趋势和设备趋势。
func (s *Store) hourlySeries(ctx context.Context, day time.Time, devices []Device, snapshot int64) ([]Point, map[string][]Point, error) {
	end := day.AddDate(0, 0, 1)
	if snapshot > 0 && snapshot+1 < end.Unix() {
		// 历史快照查询不能读取截止点之后的新采集样本。
		end = time.Unix(snapshot+1, 0).In(beijing)
	}
	clause, args := deviceClause(devices)
	// 额外读取当天前一小时，用于补齐 00:00 后首个样本的计数器差值。
	args = append([]any{day.Add(-time.Hour).Unix(), end.Unix()}, args...)
	rows, err := s.db.QueryContext(ctx, `SELECT client_id,inbound_id,captured_at,up_bytes,down_bytes FROM samples
		WHERE captured_at >= ? AND captured_at < ? AND (`+clause+`) ORDER BY client_id,inbound_id,captured_at`, args...)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()

	hours := make([]counters, 24)
	deviceHours := map[string][]counters{}
	for _, device := range devices {
		deviceHours[device.ID] = make([]counters, 24)
	}
	previous := map[string]counters{}
	for rows.Next() {
		var clientID, inboundID, capturedAt, up, down int64
		if err := rows.Scan(&clientID, &inboundID, &capturedAt, &up, &down); err != nil {
			return nil, nil, err
		}
		key := deviceID(clientID, inboundID)
		if old, ok := previous[key]; ok && capturedAt >= day.Unix() {
			hour := time.Unix(capturedAt, 0).In(beijing).Hour()
			upload := counterDelta(up, old.upload)
			download := counterDelta(down, old.download)
			// 同时累加总序列和对应设备序列，确保两种图表数据来自同一批样本。
			hours[hour].upload += upload
			hours[hour].download += download
			deviceHours[key][hour].upload += upload
			deviceHours[key][hour].download += download
		}
		previous[key] = counters{up, down}
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	points := make([]Point, 24)
	byDevice := map[string][]Point{}
	for _, device := range devices {
		byDevice[device.ID] = make([]Point, 24)
	}
	for hour, value := range hours {
		points[hour] = Point{Label: fmt.Sprintf("%02d:00", hour), Upload: value.upload, Download: value.download}
		for _, device := range devices {
			deviceValue := deviceHours[device.ID][hour]
			byDevice[device.ID][hour] = Point{Label: points[hour].Label, Upload: deviceValue.upload, Download: deviceValue.download}
		}
	}
	return points, byDevice, nil
}

// parseRange 校验预设或自选范围，并返回首尾均包含在内的北京时间自然日边界。
func parseRange(kind, requestedStart, requestedEnd string, now time.Time) (Range, error) {
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, beijing)
	switch kind {
	case "week":
		return Range{Kind: kind, Start: today.AddDate(0, 0, -6), End: today}, nil
	case "month":
		return Range{Kind: kind, Start: today.AddDate(0, 0, -29), End: today}, nil
	case "today", "":
		return Range{Kind: "today", Start: today, End: today}, nil
	case "custom":
		start, err := time.ParseInLocation("2006-01-02", requestedStart, beijing)
		if err != nil {
			return Range{}, fmt.Errorf("%w: invalid custom start", ErrInvalidRange)
		}
		end, err := time.ParseInLocation("2006-01-02", requestedEnd, beijing)
		if err != nil {
			return Range{}, fmt.Errorf("%w: invalid custom end", ErrInvalidRange)
		}
		// 自选范围最多 30 个自然日，且只能落在含今天在内的最近 30 天内。
		if start.After(end) || start.Before(today.AddDate(0, 0, -29)) || end.After(today) || end.Sub(start)/time.Hour/24+1 > 30 {
			return Range{}, fmt.Errorf("%w: custom range must stay within the most recent 30 natural days", ErrInvalidRange)
		}
		return Range{Kind: kind, Start: start, End: end}, nil
	default:
		return Range{}, fmt.Errorf("%w: unsupported range", ErrInvalidRange)
	}
}

// series 补齐范围内每个自然日，并返回总趋势、设备趋势及累计值。
func series(r Range, usage map[string]map[string]counters, devices []Device) ([]Point, map[string][]Point, int64, int64) {
	var points []Point
	byDevice := map[string][]Point{}
	for _, device := range devices {
		byDevice[device.ID] = []Point{}
	}
	var totalUpload, totalDownload int64
	for day := r.Start; !day.After(r.End); day = day.AddDate(0, 0, 1) {
		// 即使某天没有采样也输出零值，保证前端横轴连续。
		point := Point{Label: day.Format("2006-01-02")}
		for _, device := range devices {
			value := usage[day.Format("2006-01-02")][device.ID]
			point.Upload += value.upload
			point.Download += value.download
			byDevice[device.ID] = append(byDevice[device.ID], Point{Label: point.Label, Upload: value.upload, Download: value.download})
		}
		totalUpload += point.Upload
		totalDownload += point.Download
		points = append(points, point)
	}
	return points, byDevice, totalUpload, totalDownload
}

// deviceSeries 将每台已选设备的元数据附加到可独立选择的趋势序列。
func deviceSeries(devices []Device, byDevice map[string][]Point) []DeviceSeries {
	result := make([]DeviceSeries, 0, len(devices))
	for _, device := range devices {
		result = append(result, DeviceSeries{Device: device, Series: byDevice[device.ID]})
	}
	return result
}

// rankDevices 汇总每日计数器并按总流量降序生成设备排行。
func rankDevices(devices []Device, usage map[string]map[string]counters) []DeviceUsage {
	result := make([]DeviceUsage, 0, len(devices))
	for _, device := range devices {
		item := DeviceUsage{Device: device}
		for _, day := range usage {
			value := day[device.ID]
			item.Upload += value.upload
			item.Download += value.download
		}
		result = append(result, item)
	}
	// 排行以总流量为准；总流量相同的设备不额外定义排序规则。
	sort.Slice(result, func(i, j int) bool { return result[i].Upload+result[i].Download > result[j].Upload+result[j].Download })
	return result
}

// deviceClause 为已知稳定设备标识构造参数化 SQL 条件，避免拼接外部输入。
func deviceClause(devices []Device) (string, []any) {
	terms := make([]string, 0, len(devices))
	args := make([]any, 0, len(devices)*2)
	for _, device := range devices {
		terms = append(terms, "(client_id=? AND inbound_id=?)")
		args = append(args, device.ClientID, device.InboundID)
	}
	return strings.Join(terms, " OR "), args
}

// deviceID 序列化 API 与前端选择器使用的复合数据库标识。
func deviceID(clientID, inboundID int64) string {
	return strconv.FormatInt(clientID, 10) + ":" + strconv.FormatInt(inboundID, 10)
}

// counterDelta 返回正向计数器增量，并将较小的新值视为来源端计数器重置。
func counterDelta(current, previous int64) int64 {
	if current >= previous {
		return current - previous
	}
	return current
}

// nextMinute 提供采集器下一次定时执行时刻的保守兜底值。
func nextMinute(now time.Time) int64 { return now.Truncate(time.Minute).Add(65 * time.Second).Unix() }
