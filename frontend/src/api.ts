/** 仪表盘支持的预设和自选时间范围名称。 */
export type RangeName = 'today' | 'week' | 'month' | 'custom'
/** 由客户端与入站组合唯一确定的设备元数据。 */
export interface Device {
  /** 前后端共用的复合稳定标识。 */
  id: string
  clientId: number
  inboundId: number
  email: string
  label: string
}
/** 采集器健康度、下一次采集时刻及数据库文件实时状态。 */
export interface Status { lastSuccess: number; nextSync: number; databaseBytes: number; databaseAvailable: boolean; healthy: boolean }
/** 一个小时或一个自然日中的上传、下载流量增量。 */
export interface Point { label: string; upload: number; download: number }
/** 设备在当前范围内的累计流量，用于排行展示。 */
export interface DeviceUsage extends Device { upload: number; download: number }
/** 可在设备图表选项卡内独立开关的单台设备趋势。 */
export interface DeviceSeries extends Device { series: Point[] }
/** 同一采集快照下的仪表盘汇总、趋势和设备排行数据。 */
export interface Dashboard {
  range: RangeName
  start: string
  end: string
  /** 统一多接口读取结果的数据截止时间。 */
  snapshot: number
  selected: string
  upload: number
  download: number
  total: number
  currentRate: number
  series: Point[]
  devices: DeviceUsage[]
  deviceSeries: DeviceSeries[]
}

/** 向本地 Gin API 发起不使用缓存的请求，并将失败响应统一为前端错误。 */
async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
	// signal 由页面刷新流程传入，用于终止已过期的并发请求。
  const response = await fetch(path, { cache: 'no-store', signal })
  if (!response.ok) throw new Error(`请求统计数据失败（${response.status}）`)
  return response.json() as Promise<T>
}
/** 获取采集新鲜度、下次采集时刻和数据库文件状态。 */
export const getStatus = (signal?: AbortSignal) => request<Status>('/api/v1/status', signal)
/** 在固定采集快照下获取稳定设备列表。 */
export const getDevices = (snapshot: number, signal?: AbortSignal) => request<{ devices: Device[] }>(`/api/v1/devices?snapshot=${snapshot}`, signal)
/** 获取指定范围和可选设备的仪表盘数据；自选范围额外携带起止自然日。 */
export function getDashboard(range: RangeName, device: string, snapshot: number, start?: string, end?: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ range, snapshot: String(snapshot) })
  if (device !== 'all') query.set('device', device)
  // 只有自选模式传递日期，避免预设范围被客户端日期覆盖。
  if (range === 'custom' && start && end) { query.set('start', start); query.set('end', end) }
  return request<Dashboard>(`/api/v1/dashboard?${query}`, signal)
}
