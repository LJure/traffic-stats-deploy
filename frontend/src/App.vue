<script setup lang="ts">
import { LineChart } from 'echarts/charts'
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { graphic, init, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import appleIcon from '@lobehub/icons-static-svg/icons/apple.svg'
import googleIcon from '@lobehub/icons-static-svg/icons/google.svg'
import huaweiIcon from '@lobehub/icons-static-svg/icons/huawei.svg'
import microsoftIcon from '@lobehub/icons-static-svg/icons/microsoft.svg'
import { getDashboard, getDevices, getStatus, type Dashboard, type Device, type DeviceSeries, type RangeName, type Status } from './api'

type TrendMode = 'total' | 'devices'

// 顶部时间范围、设备选择与设备趋势图模式的即时交互状态。
const ranges: { key: Exclude<RangeName, 'custom'>; label: string }[] = [{ key: 'today', label: '今天' }, { key: 'week', label: '7 天' }, { key: 'month', label: '30 天' }]
const trendColors = ['#3478f6', '#17b7a0', '#9b6cf6', '#f59d42', '#ec5b8d', '#13a7c7']
const selectedRange = ref<RangeName>('today')
const selectedDevice = ref('all')
const trendMode = ref<TrendMode>('total')
const selectedDeviceCurves = ref<string[]>([])
// 自选日期只在用户点击“应用”后才成为实际请求参数。
const customStart = ref(dateInputValue(-6))
const customEnd = ref(dateInputValue(0))
const customError = ref('')
const customPickerOpen = ref(false)
// 后端响应、加载过程和页面时钟共同驱动卡片、遮罩与采集倒计时。
const status = ref<Status | null>(null)
const dashboard = ref<Dashboard | null>(null)
const devices = ref<Device[]>([])
const loading = ref(true)
const updating = ref(false)
const error = ref('')
const clock = ref(Date.now())
const chartElement = ref<HTMLDivElement | null>(null)
const rangeControl = ref<HTMLElement | null>(null)
// 按需注册 ECharts 模块，避免引入完整图表库。
use([LineChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

// 这些实例和定时器不参与 Vue 响应式渲染，仅用于管理外部资源与异步请求。
let chart: ECharts | null = null
let requestController: AbortController | null = null
let requestSequence = 0
let pollTimer: number | undefined
let clockTimer: number | undefined

const hasData = computed(() => dashboard.value !== null)
const selectedName = computed(() => !dashboard.value || dashboard.value.selected === 'all' ? '全部设备' : (dashboard.value.devices.find((device) => device.id === dashboard.value?.selected)?.email ?? '所选设备'))
const lastUpdated = computed(() => status.value ? formatDate(status.value.lastSuccess) : '等待采集数据')
const collectorAge = computed(() => status.value ? formatAge(Math.max(0, Math.floor(clock.value / 1000) - status.value.lastSuccess)) : '')
const collectionState = computed(() => updating.value ? '数据正在更新' : collectorAge.value)
const rangeUnit = computed(() => dashboard.value?.series[0]?.label.includes(':') ? '按小时' : '按日')
const todayDate = computed(() => dateInputValue(0))
const earliestDate = computed(() => dateInputValue(-29))
const isAllDevices = computed(() => selectedDevice.value === 'all')
const isDeviceTrend = computed(() => isAllDevices.value && trendMode.value === 'devices')
const chartSeries = computed(() => dashboard.value?.deviceSeries ?? [])
const databaseTooLarge = computed(() => (status.value?.databaseBytes ?? 0) > 10 * 1_000_000_000)
const databaseHealthy = computed(() => Boolean(status.value?.databaseAvailable) && !databaseTooLarge.value)

/** 将原始字节数转换为卡片和图表使用的十进制流量单位。 */
function formatBytes(value: number, compact = false) { const units = ['B', 'KB', 'MB', 'GB', 'TB']; let number = Number(value ?? 0); let index = 0; while (number >= 1000 && index < units.length - 1) { number /= 1000; index += 1 }; return `${number.toFixed(index === 0 ? 0 : compact ? 1 : 2)} ${units[index]}` }
/** 按北京时间显示采集时间，使前端与后端自然日聚合边界保持一致。 */
function formatDate(epoch: number) { return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(epoch * 1000)) }
/** 格式化实时采集间隔；新采集快照到达后计时会重新开始。 */
function formatAge(seconds: number) { return seconds < 60 ? `${seconds} 秒前` : `${Math.floor(seconds / 60)} 分钟前` }
/** 生成可选自然日窗口内、按北京时间计算的日期输入值。 */
function dateInputValue(offset: number) { const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date()); const values = Object.fromEntries(parts.filter((part) => part.type !== 'literal').map((part) => [part.type, part.value])); const date = new Date(Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day) + offset)); return date.toISOString().slice(0, 10) }
/** 在自选范围进入乐观选择器状态前，校验其首尾日期与 30 天上限。 */
function validCustomRange() { if (!customStart.value || !customEnd.value || customStart.value > customEnd.value) return false; const start = new Date(`${customStart.value}T00:00:00Z`).getTime(); const end = new Date(`${customEnd.value}T00:00:00Z`).getTime(); return start >= new Date(`${earliestDate.value}T00:00:00Z`).getTime() && end <= new Date(`${todayDate.value}T00:00:00Z`).getTime() && (end - start) / 86_400_000 + 1 <= 30 }
/** 立即更新预设范围选择器，关闭自选弹层并在后台继续加载数据。 */
function setRange(range: Exclude<RangeName, 'custom'>) { customError.value = ''; customPickerOpen.value = false; if (range === selectedRange.value) { loadDashboard(); return }; selectedRange.value = range }
/** 打开自选范围弹层；在用户应用前不改变当前图表。 */
function openCustomRange() { customError.value = ''; customPickerOpen.value = true }
/** 应用合法的自选自然日范围、刷新图表并关闭弹层。 */
function applyCustomRange() { if (!validCustomRange()) { customError.value = '请选择最近 30 个自然日内的有效日期范围'; return }; customError.value = ''; customPickerOpen.value = false; if (selectedRange.value === 'custom') loadDashboard(); else selectedRange.value = 'custom' }
/** 当鼠标点击落在时间范围控件之外时关闭自选范围弹层。 */
function closeCustomRangeOnOutsideClick(event: MouseEvent) { if (customPickerOpen.value && rangeControl.value && !rangeControl.value.contains(event.target as Node)) customPickerOpen.value = false }
/** 在全部设备趋势视图中切换一条可独立绘制的设备曲线。 */
function toggleDeviceCurve(id: string) { selectedDeviceCurves.value = selectedDeviceCurves.value.includes(id) ? selectedDeviceCurves.value.filter((item) => item !== id) : [...selectedDeviceCurves.value, id] }
/** 在用户缩小设备曲线范围后恢复显示所有可用曲线。 */
function selectAllDeviceCurves() { selectedDeviceCurves.value = chartSeries.value.map((series) => series.id) }
/** 仅将指定的四类设备平台映射到 Lobe 静态 SVG 品牌图标。 */
function platformIcon(device: Device | DeviceSeries) { const platform = `${device.label} ${device.email}`.toLowerCase(); if (platform.includes('windows')) return microsoftIcon; if (platform.includes('ios') || platform.includes('iphone') || platform.includes('ipad')) return appleIcon; if (platform.includes('android')) return googleIcon; if (platform.includes('harmony') || platform.includes('鸿蒙') || platform.includes('huawei')) return huaweiIcon; return '' }
/** 依据 API 返回的下次采集边界安排轮询；采集尚未完成时按较短间隔重试。 */
function schedulePolling(currentStatus: Status) { if (pollTimer) window.clearTimeout(pollTimer); const delay = updating.value ? 5_000 : Math.max(1_000, currentStatus.nextSync * 1000 - Date.now()); pollTimer = window.setTimeout(() => { updating.value = true; loadDashboard() }, delay) }
/** 获取同一不可变快照的状态、设备列表与仪表盘，并取消旧的范围或设备请求。 */
async function loadDashboard() {
  // 先终止较早请求，并用递增序号阻止网络较慢的旧响应覆盖最新选择。
  requestController?.abort(); const controller = new AbortController(); requestController = controller; const sequence = ++requestSequence
  loading.value = true; error.value = ''
  try {
    const currentStatus = await getStatus(controller.signal)
    // 后续两个接口使用同一采集成功时刻，确保设备和图表来自同一数据切面。
    const [devicePayload, result] = await Promise.all([getDevices(currentStatus.lastSuccess, controller.signal), getDashboard(selectedRange.value, selectedDevice.value, currentStatus.lastSuccess, customStart.value, customEnd.value, controller.signal)])
    // 仅允许当前序号写入响应式状态，避免快速切换时发生图表回退。
    if (sequence !== requestSequence) return
    const previousSuccess = status.value?.lastSuccess ?? 0
    status.value = currentStatus
    devices.value = devicePayload.devices
    if (selectedDevice.value !== 'all' && !devicePayload.devices.some((device) => device.id === selectedDevice.value)) selectedDevice.value = 'all'
    dashboard.value = result
    const availableCurves = result.deviceSeries.map((series) => series.id)
    // 保留用户仍可见设备的手动勾选；设备集合变化时才恢复为全部显示。
    const retainedCurves = selectedDeviceCurves.value.filter((id) => availableCurves.includes(id))
    selectedDeviceCurves.value = retainedCurves.length ? retainedCurves : availableCurves
    // 只有观测到新的采集记录后才结束“数据正在更新”提示。
    if (currentStatus.lastSuccess > previousSuccess || previousSuccess === 0) updating.value = false
    await nextTick(); renderChart(); schedulePolling(currentStatus)
  } catch (cause) { if (cause instanceof DOMException && cause.name === 'AbortError') return; if (sequence === requestSequence) { error.value = cause instanceof Error ? cause.message : '统计服务暂时不可用'; if (status.value) schedulePolling(status.value) } } finally { if (sequence === requestSequence) loading.value = false }
}
/** 创建或更新汇总趋势与可自由选择的设备趋势 ECharts 图表。 */
function renderChart() {
  if (!chartElement.value || !dashboard.value) return
  // 复用图表实例，避免每次轮询都创建新的 canvas 与事件监听。
  chart ??= init(chartElement.value)
  const labels = dashboard.value.series.map((point) => point.label)
  // 汇总模式分别展示上、下行；设备模式展示每台设备的总流量并遵从勾选状态。
  const totalSeries = [{ name: '下载', type: 'line', smooth: true, showSymbol: false, data: dashboard.value.series.map((point) => point.download), lineStyle: { width: 3, color: '#3478f6' }, itemStyle: { color: '#3478f6' }, areaStyle: { color: new graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(52,120,246,.36)' }, { offset: 1, color: 'rgba(52,120,246,.02)' }]) } }, { name: '上传', type: 'line', smooth: true, showSymbol: false, data: dashboard.value.series.map((point) => point.upload), lineStyle: { width: 2, color: '#17b7a0' }, itemStyle: { color: '#17b7a0' } }]
  const deviceSeries = dashboard.value.deviceSeries.filter((series) => selectedDeviceCurves.value.includes(series.id)).map((series, index) => ({ name: series.email, type: 'line', smooth: true, showSymbol: false, data: series.series.map((point) => point.upload + point.download), lineStyle: { width: 2.5, color: trendColors[index % trendColors.length] }, itemStyle: { color: trendColors[index % trendColors.length] } }))
  // 同时启用滚轮/手势缩放与底部滑块，覆盖小时和自然日两种时间粒度。
  chart.setOption({ tooltip: { trigger: 'axis', valueFormatter: (value: number) => formatBytes(value) }, grid: { left: 6, right: 12, top: 30, bottom: 54, containLabel: true }, legend: { right: 8, top: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: '#728097', fontSize: 12 } }, xAxis: { type: 'category', boundaryGap: false, data: labels, axisLine: { lineStyle: { color: '#e7edf5' } }, axisTick: { show: false }, axisLabel: { color: '#99a4b6', fontSize: 11, interval: labels.length > 12 ? Math.ceil(labels.length / 8) : 0 } }, yAxis: { type: 'value', splitLine: { lineStyle: { color: '#edf1f6', type: 'dashed' } }, axisLabel: { color: '#99a4b6', fontSize: 11, formatter: (value: number) => formatBytes(value, true) } }, dataZoom: [{ type: 'inside', xAxisIndex: 0 }, { type: 'slider', xAxisIndex: 0, bottom: 8, height: 18, borderColor: 'transparent', fillerColor: 'rgba(52,120,246,.16)', handleStyle: { color: '#3478f6' }, textStyle: { color: '#99a4b6' } }], series: isDeviceTrend.value ? deviceSeries : totalSeries }, { notMerge: true })
  chart.resize()
}
/** 在视口或仪表盘列布局变化后重新计算图表尺寸。 */
function resizeChart() { chart?.resize() }

watch(selectedRange, loadDashboard)
watch(selectedDevice, () => { trendMode.value = 'total'; loadDashboard() })
watch([trendMode, selectedDeviceCurves], async () => { await nextTick(); renderChart() }, { deep: true })
onMounted(() => { loadDashboard(); window.addEventListener('resize', resizeChart); document.addEventListener('mousedown', closeCustomRangeOnOutsideClick); clockTimer = window.setInterval(() => { clock.value = Date.now() }, 1_000) })
onBeforeUnmount(() => { requestController?.abort(); window.removeEventListener('resize', resizeChart); document.removeEventListener('mousedown', closeCustomRangeOnOutsideClick); if (pollTimer) window.clearTimeout(pollTimer); if (clockTimer) window.clearInterval(clockTimer); chart?.dispose() })
</script>

<template>
  <main class="shell">
    <nav class="topbar"><a class="brand" href="/" aria-label="Traffic 仪表盘"><span>↗</span>Traffic</a></nav>
    <section class="heading"><div><p class="eyebrow">自建节点 · 设备统计</p><h1>{{ selectedName }}流量概览</h1><p class="sub">最后采集：{{ lastUpdated }}（北京时间）<span v-if="collectionState"> · {{ collectionState }}</span></p></div><div class="controls" aria-label="统计范围和设备"><label class="select-wrap"><span class="sr-only">选择设备</span><select v-model="selectedDevice"><option value="all">全部设备（排行）</option><option v-for="device in devices" :key="device.id" :value="device.id">{{ device.email }} · {{ device.label }}</option></select></label><div ref="rangeControl" class="range-control"><div class="tabs" role="tablist"><button v-for="range in ranges" :key="range.key" type="button" :class="{ active: selectedRange === range.key && !customPickerOpen }" @click="setRange(range.key)">{{ range.label }}</button><button type="button" :class="{ active: selectedRange === 'custom' || customPickerOpen }" @click="openCustomRange">自选</button></div><section v-if="customPickerOpen" class="custom-popover" aria-label="自选日期范围"><b>自选范围</b><div><label><input v-model="customStart" type="date" :min="earliestDate" :max="todayDate"></label><span>至</span><label><input v-model="customEnd" type="date" :min="earliestDate" :max="todayDate"></label><button type="button" @click="applyCustomRange">应用</button></div><small v-if="customError">{{ customError }}</small></section></div></div></section>
    <p v-if="error" class="notice error">{{ error }} <button type="button" @click="loadDashboard">重试</button></p><p v-else-if="loading && !hasData" class="notice">正在读取流量统计…</p>
    <template v-if="dashboard"><section class="metrics"><article class="metric-card"><span>所选区间用量</span><strong>{{ formatBytes(dashboard.total) }}</strong><small>{{ dashboard.start }} 至 {{ dashboard.end }}</small></article><article class="metric-card"><span>上传 / 下载</span><strong class="split-value">↑ {{ formatBytes(dashboard.upload) }}<br>↓ {{ formatBytes(dashboard.download) }}</strong><small>仅统计自建节点转发流量</small></article><article class="metric-card hero-card"><div><span>当前节点速率</span><strong>{{ formatBytes(dashboard.currentRate) }}/s</strong><small>按最近两次分钟采样计算</small></div><span class="pulse">实时</span></article></section>
      <section class="layout" :class="{ single: !isAllDevices }"><article class="panel chart-panel"><header><div><h2>流量趋势</h2><p>{{ isDeviceTrend ? '各设备总流量 · ' : '下载与上传 · ' }}{{ rangeUnit }}</p></div><span>{{ dashboard.start }} 至 {{ dashboard.end }}</span></header><div v-if="isAllDevices" class="trend-tabs" role="tablist"><button type="button" :class="{ active: trendMode === 'total' }" @click="trendMode = 'total'">汇总</button><button type="button" :class="{ active: trendMode === 'devices' }" @click="trendMode = 'devices'">设备</button></div><div v-if="isDeviceTrend" class="curve-picker"><button type="button" class="all-curves" @click="selectAllDeviceCurves">全部显示</button><label v-for="series in chartSeries" :key="series.id"><input type="checkbox" :checked="selectedDeviceCurves.includes(series.id)" @change="toggleDeviceCurve(series.id)"><img v-if="platformIcon(series)" :src="platformIcon(series)" alt="">{{ series.email }}</label></div><div class="chart-wrap"><div ref="chartElement" class="chart" aria-label="流量趋势图"></div><div v-if="loading" class="chart-loading" aria-live="polite"><span></span>加载中</div></div></article><article v-if="isAllDevices" class="panel devices-panel"><header><div><h2>设备用量</h2><p>所选区间排行</p></div></header><div class="device-list"><div v-for="device in dashboard.devices" :key="device.id" class="device-row"><div class="device-icon"><img v-if="platformIcon(device)" :src="platformIcon(device)" alt=""><span v-else>—</span></div><div class="device-main"><b>{{ device.email }}</b><small>{{ device.label }}</small><i><em :style="{ width: `${dashboard.total ? Math.max(3, ((device.upload + device.download) / dashboard.total) * 100) : 0}%` }"></em></i></div><strong>{{ formatBytes(device.upload + device.download) }}<small>{{ dashboard.total ? (((device.upload + device.download) / dashboard.total) * 100).toFixed(0) : 0 }}%</small></strong></div></div></article></section>
      <section class="bottom"><article class="panel status-panel"><h2>服务状态</h2><div class="status-line"><i :class="{ bad: !status?.healthy }"></i><span>当前本机统计服务</span><b>{{ status?.healthy ? '正常' : '异常' }}</b></div><div class="status-line"><i :class="{ bad: !databaseHealthy }"></i><span>采集数据库文件</span><b>{{ status?.databaseAvailable ? formatBytes(status.databaseBytes) : '不可读取' }}</b></div><div class="status-line"><i :class="{ bad: !status?.healthy }"></i><span>采集状态</span><b :class="{ updating }">{{ updating ? '数据正在更新' : status?.healthy ? '采集正常' : '采集异常' }}</b></div></article><article class="panel help-panel"><h2>统计说明</h2><p>设备按 sing-box 认证用户稳定聚合；修改显示名称不会拆分历史流量。</p></article></section></template>
  </main>
</template>
