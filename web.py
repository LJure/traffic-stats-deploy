#!/usr/bin/env python3
"""Local-only read-only dashboard for collected 3x-ui device traffic."""

import html
import os
import subprocess
import json
import sqlite3
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

DB = "file:/var/lib/traffic-stats/traffic.sqlite3?mode=ro"
DB_FILE = "/var/lib/traffic-stats/traffic.sqlite3"
TZ = ZoneInfo("Asia/Shanghai")


def fmt(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000


def day_list(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def range_from_query(q, today):
    kind = q.get("range", ["today"])[0]
    if kind == "today":
        return kind, today, today
    if kind == "month":
        return kind, today - timedelta(days=29), today
    if kind == "week":
        return kind, today - timedelta(days=6), today
    if kind == "custom":
        try:
            start = date.fromisoformat(q.get("start", [""])[0])
            end = date.fromisoformat(q.get("end", [""])[0])
            minimum = today - timedelta(days=29)
            if minimum <= start <= end <= today:
                return kind, start, end
        except ValueError:
            pass
    # Invalid values also fall back to today's traffic across every device.
    return "today", today, today


def svg(values, color, fill):
    if not values:
        return ""
    width, height, pad = 700, 210, 16
    maximum = max(max(values), 1)
    points = []
    for i, value in enumerate(values):
        x = pad + (width - 2 * pad) * i / max(1, len(values) - 1)
        y = height - pad - (height - 2 * pad) * value / maximum
        points.append((x, y))
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
    area = f"{line} L {points[-1][0]:.1f} {height-pad} L {points[0][0]:.1f} {height-pad} Z"
    return f"<path d='{area}' fill='{fill}'/><path d='{line}' fill='none' stroke='{color}' stroke-width='3'/>"


def next_sync_epoch():
    """Read the timer's real next elapse time, including its randomized delay."""
    try:
        value = subprocess.check_output(
            ["systemctl", "show", "traffic-stats-collect.timer", "--value", "-p", "NextElapseUSecRealtime"],
            text=True, timeout=2,
        ).strip()
        return int(subprocess.check_output(["date", "-d", value, "+%s"], text=True, timeout=2).strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return int(datetime.now(TZ).timestamp() // 60 * 60 + 65)


def collected_data_size():
    """Current on-disk size of the collector database and its SQLite journals."""
    return sum(
        os.path.getsize(path) for path in (DB_FILE, DB_FILE + "-wal", DB_FILE + "-shm")
        if os.path.exists(path)
    )


def dashboard(query):
    now = datetime.now(TZ)
    today = now.date()
    kind, start, end = range_from_query(query, today)
    try:
        snapshot = max(0, int(query.get("snapshot", ["0"])[0]))
    except ValueError:
        snapshot = 0
    snapshot = min(snapshot, int(now.timestamp()))
    con = sqlite3.connect(DB, uri=True, timeout=5)
    try:
        sample_limit = "WHERE captured_at <= ?" if snapshot else ""
        sample_args = [snapshot] if snapshot else []
        latest = con.execute("""
            WITH l AS (SELECT client_id,inbound_id,MAX(captured_at) t FROM samples %s GROUP BY client_id,inbound_id)
            SELECT s.client_id,s.inbound_id,s.email,s.label,s.up_bytes,s.down_bytes,s.captured_at
            FROM samples s JOIN l ON l.client_id=s.client_id AND l.inbound_id=s.inbound_id AND l.t=s.captured_at
            ORDER BY s.label COLLATE NOCASE
        """ % sample_limit, sample_args).fetchall()
        devices = {f"{r[0]}:{r[1]}": r for r in latest}
        requested = query.get("device", ["all"])[0]
        selected = list(devices) if requested == "all" or requested not in devices else [requested]
        device = "all" if requested == "all" or requested not in devices else requested
        placeholders = ",".join("?" for _ in selected)
        pairs = [(int(k.split(":")[0]), int(k.split(":")[1])) for k in selected]
        where = " OR ".join("(client_id=? AND inbound_id=?)" for _ in pairs)
        args = [start.isoformat(), end.isoformat(), *[v for pair in pairs for v in pair]]
        daily = con.execute(f"""
            SELECT day,client_id,inbound_id,SUM(up_bytes),SUM(down_bytes)
            FROM daily_usage WHERE day BETWEEN ? AND ? AND ({where})
            GROUP BY day,client_id,inbound_id
        """, args).fetchall()
        snapshot_usage = {}
        snapshot_day = datetime.fromtimestamp(snapshot, TZ).date() if snapshot else None
        if snapshot_day and start <= snapshot_day <= end:
            day_start = datetime(snapshot_day.year, snapshot_day.month, snapshot_day.day, tzinfo=TZ)
            rows = con.execute(f"""
                SELECT client_id,inbound_id,captured_at,up_bytes,down_bytes FROM samples
                WHERE captured_at >= ? AND captured_at <= ? AND ({where})
                ORDER BY client_id,inbound_id,captured_at
            """, [int(day_start.timestamp()) - 3600, snapshot, *[v for pair in pairs for v in pair]]).fetchall()
            previous = {}
            for cid, iid, captured_at, up, down in rows:
                key = (cid, iid)
                if key in previous and captured_at >= int(day_start.timestamp()):
                    old_up, old_down = previous[key]
                    delta_up = up - old_up if up >= old_up else up
                    delta_down = down - old_down if down >= old_down else down
                    totals = snapshot_usage.setdefault(key, [0, 0])
                    totals[0] += delta_up; totals[1] += delta_down
                previous[key] = (up, down)
            daily = [row for row in daily if row[0] != snapshot_day.isoformat()]
            daily.extend((snapshot_day.isoformat(), cid, iid, values[0], values[1]) for (cid, iid), values in snapshot_usage.items())
        by_day = {d.isoformat(): [0, 0] for d in day_list(start, end)}
        by_device = {k: [0, 0] for k in selected}
        by_device_day = {k: {d.isoformat(): [0, 0] for d in day_list(start, end)} for k in selected}
        for day, cid, iid, up, down in daily:
            key = f"{cid}:{iid}"; by_day[day][0] += up; by_day[day][1] += down
            by_device[key][0] += up; by_device[key][1] += down
            by_device_day[key][day][0] += up; by_device_day[key][day][1] += down
        total_up = sum(v[0] for v in by_day.values()); total_down = sum(v[1] for v in by_day.values())
        if snapshot_day:
            prior_up, prior_down, collection_start = con.execute(f"""
                SELECT COALESCE(SUM(up_bytes),0), COALESCE(SUM(down_bytes),0), MIN(day)
                FROM daily_usage WHERE day < ? AND ({where})
            """, [snapshot_day.isoformat(), *[v for pair in pairs for v in pair]]).fetchone()
            collected_up = prior_up + sum(v[0] for v in snapshot_usage.values())
            collected_down = prior_down + sum(v[1] for v in snapshot_usage.values())
            collection_start = collection_start or (snapshot_day.isoformat() if snapshot_usage else None)
            lifetime = (collected_up, collected_down, collection_start)
        else:
            lifetime = con.execute(f"""
                SELECT COALESCE(SUM(up_bytes),0), COALESCE(SUM(down_bytes),0), MIN(day)
                FROM daily_usage WHERE {where}
            """, [v for pair in pairs for v in pair]).fetchone()
        collected_up, collected_down, collection_start = lifetime
        hourly_samples = []
        if start == end:
            day_start = datetime(start.year, start.month, start.day, tzinfo=TZ)
            hour_start_ts = int(day_start.timestamp())
            hour_end_ts = int((day_start + timedelta(days=1)).timestamp())
            sample_end = min(hour_end_ts, snapshot + 1) if snapshot and start == snapshot_day else hour_end_ts
            hourly_samples = con.execute(f"""
                SELECT client_id,inbound_id,captured_at,up_bytes,down_bytes
                FROM samples
                WHERE captured_at >= ? AND captured_at < ? AND ({where})
                ORDER BY client_id,inbound_id,captured_at
            """, [hour_start_ts - 3600, sample_end, *[v for pair in pairs for v in pair]]).fetchall()
        rates = []
        for cid, iid in pairs:
            rate_limit = "AND captured_at <= ?" if snapshot else ""
            rate_args = (cid, iid, snapshot) if snapshot else (cid, iid)
            rows = con.execute(f"SELECT captured_at,up_bytes,down_bytes FROM samples WHERE client_id=? AND inbound_id=? {rate_limit} ORDER BY captured_at DESC LIMIT 2", rate_args).fetchall()
            if len(rows) == 2 and rows[0][0] > rows[1][0]:
                dt = rows[0][0] - rows[1][0]; delta = max(0, rows[0][1]-rows[1][1]) + max(0, rows[0][2]-rows[1][2]); rates.append(delta / dt)
        rate = sum(rates)
        last_capture = max((r[6] for r in latest), default=0)
    finally:
        con.close()

    period_name = {"today": "今天（北京时间）", "week": "过去 7 天", "month": "过去 30 天"}.get(kind, f"{start:%m/%d} 至 {end:%m/%d}")
    if start == end:
        labels = [f"{hour:02d}:00" for hour in range(24)]
        hourly_totals = [[0, 0] for _ in range(24)]
        hourly_by_device = {k: [[0, 0] for _ in range(24)] for k in selected}
        previous = {}
        for client_id, inbound_id, captured_at, up, down in hourly_samples:
            key = f"{client_id}:{inbound_id}"
            if key in previous and captured_at >= hour_start_ts:
                old_up, old_down = previous[key]
                delta_up = up - old_up if up >= old_up else up
                delta_down = down - old_down if down >= old_down else down
                hour = datetime.fromtimestamp(captured_at, TZ).hour
                hourly_totals[hour][0] += delta_up; hourly_totals[hour][1] += delta_down
                hourly_by_device[key][hour][0] += delta_up; hourly_by_device[key][hour][1] += delta_down
            previous[key] = (up, down)
        ups = [value[0] for value in hourly_totals]
        downs = [value[1] for value in hourly_totals]
        device_series = [{"name": devices[k][2], "data": [sum(value) for value in hourly_by_device[k]]} for k in selected]
    else:
        labels = [d.strftime("%m/%d") for d in day_list(start, end)]
        ups = [by_day[d.isoformat()][0] for d in day_list(start, end)]
        downs = [by_day[d.isoformat()][1] for d in day_list(start, end)]
        device_series = [
            {"name": devices[k][2], "data": [sum(by_device_day[k][d.isoformat()]) for d in day_list(start, end)]}
            for k in selected
        ]
    options = "<option value='all'>全部设备（排行）</option>" + "".join(
        f"<option value='{html.escape(key)}' {'selected' if key == device else ''}>{html.escape(row[2])}</option>"
        for key, row in devices.items()
    )
    extra = ""
    if device == "all":
        ranked = sorted(devices, key=lambda k: sum(by_device[k]), reverse=True)
        maximum = max((sum(by_device[k]) for k in ranked), default=1)
        rows = "".join(f"<div class='device'><div><b>{html.escape(devices[k][2])}</b><small>{html.escape(devices[k][3])}</small><i><em style='width:{max(3,100*sum(by_device[k])/maximum):.1f}%'></em></i></div><strong>{fmt(sum(by_device[k]))}</strong></div>" for k in ranked)
        extra = f"<section class='panel device-panel'><header><h2>设备用量</h2><span>所选区间排行</span></header>{rows}</section>"
        distribution_total = sum(sum(by_device[k]) for k in ranked)
        colors = ("#3b82f6", "#14b8a6", "#8b5cf6", "#f59e0b", "#ec4899", "#06b6d4")
        segments = "".join(
            f"<span title='{html.escape(devices[k][2])}（{html.escape(devices[k][3])}）：{fmt(sum(by_device[k]))}' style='width:{(100 * sum(by_device[k]) / distribution_total) if distribution_total else 0:.1f}%;background:{colors[index % len(colors)]}'>{((100 * sum(by_device[k]) / distribution_total) if distribution_total else 0):.0f}%</span>"
            for index, k in enumerate(ranked)
        )
        legend = "".join(
            f"<span><i style='background:{colors[index % len(colors)]}'></i>{html.escape(devices[k][2])} <b>{fmt(sum(by_device[k]))}</b></span>"
            for index, k in enumerate(ranked)
        )
        distribution = f"<section class='panel distribution'><h2>设备分布</h2><p>所选区间总流量占比</p><div class='stacked-bar'>{segments}</div><div class='stacked-legend'>{legend}</div></section>"
    else:
        distribution = ""
    selected_name = "全部设备" if device == "all" else devices[device][2]
    last_text = datetime.fromtimestamp(last_capture, TZ).strftime("%Y-%m-%d %H:%M:%S") if last_capture else "尚未采集"
    card = lambda label, value, note: f"<article class='card'><span>{label}</span><b>{value}</b><small>{note}</small></article>"
    cards = "".join((
        card("所选区间用量", fmt(total_up + total_down), f"{period_name} · {selected_name}"),
        card("上传 / 下载", f"<span class='io-value'>上传：{fmt(total_up)}<br>下载：{fmt(total_down)}</span>", "仅统计自建节点转发流量"),
        card("采集后累计", f"<span class='io-value'>上传：{fmt(collected_up)}<br>下载：{fmt(collected_down)}</span>", f"自 {collection_start or '采集开始'} 起，与设备统计对齐"),
        card("当前速率", fmt(rate) + "/s", "按最近两次分钟采样计算"),
    ))
    controls = f"""<form class='controls' id='dashboard-form' method='get'><select name='device' onchange='this.form.submit()'>{options}</select>
    <input id='range-value' type='hidden' name='range' value='{kind}'><input type='hidden' name='snapshot' value='{last_capture}'><div class='tabs'>""" + "".join(
        f"<button type='button' data-range='{key}' class='{'on' if kind == key else ''}'>{label}</button>"
        for key, label in (("today", "今天"), ("week", "7 天"), ("month", "30 天"))) + f"""<button type='button' data-custom-toggle class='{'on' if kind == 'custom' else ''}'>自选</button><div id='custom-picker' class='custom-picker' hidden><b>自选范围</b><div><input name='start' type='date' value='{start}' min='{today-timedelta(days=29)}' max='{today}'><span>至</span><input name='end' type='date' value='{end}' min='{today-timedelta(days=29)}' max='{today}'><button type='submit' onclick=\"document.getElementById('range-value').value='custom'\">应用</button></div></div></div></form>"""
    next_sync = next_sync_epoch()
    storage_note = f"采集数据占用：{fmt(collected_data_size())}（{today:%Y-%m-%d} 统计）"
    page = PAGE.replace("{{controls}}", controls).replace("{{cards}}", cards).replace("{{period}}", period_name).replace("{{selected}}", html.escape(selected_name)).replace("{{last}}", last_text).replace("{{svg_up}}", "").replace("{{svg_down}}", "").replace("{{axis}}", "").replace("{{extra}}", extra).replace("{{distribution}}", distribution).replace("{{storage}}", storage_note).replace("{{single}}", "single" if device != "all" else "")
    page = page.replace("<meta http-equiv='refresh' content='60'>", "")
    page = page.replace(f"最后成功采集：{last_text}（北京时间）</p>", f"最后成功采集：{last_text}（北京时间） · <b id='next-sync'>下次同步 --:--</b><button id='manual-refresh' hidden title='检测到新数据，手动刷新'>↻</button></p>")
    old_chart = "<svg class='chart' viewBox='0 0 700 210' preserveAspectRatio='none'><path d='M0 20H700M0 70H700M0 120H700M0 170H700' stroke='#edf0f5' fill='none'/></svg><div class='axis'></div>"
    page = page.replace(old_chart, "<div id='traffic-chart' class='chart' aria-label='流量趋势图'></div>")
    chart_data = json.dumps({"labels": labels, "upload": ups, "download": downs, "devices": device_series, "allMode": device == "all"}, ensure_ascii=False)
    script = f"""<style>.layout{{grid-template-columns:minmax(0,1.7fr) minmax(280px,1fr)}}.layout>.panel{{min-width:0}}.chart{{width:100%;height:310px;min-width:0}}.card .io-value{{display:block;color:#172033;font-size:20px;line-height:1.55;letter-spacing:-.3px}}.distribution>p{{color:#7a8496;font-size:12px;margin:5px 0 16px}}.stacked-bar{{display:flex;height:34px;border-radius:9px;overflow:hidden;background:#edf0f5}}.stacked-bar span{{display:grid;place-items:center;min-width:0;color:#fff;font-size:12px;font-weight:750;text-shadow:0 1px 1px #0003;white-space:nowrap;overflow:hidden}}.stacked-legend{{display:flex;flex-wrap:wrap;gap:10px 18px;margin-top:15px;color:#64748b;font-size:12px}}.stacked-legend span{{white-space:nowrap}}.stacked-legend i{{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:5px}}.stacked-legend b{{color:#172033;font-weight:650;margin-left:3px}}.tabs{{position:relative;overflow:visible}}.custom-picker{{position:absolute;right:0;top:calc(100% + 9px);z-index:5;min-width:360px;padding:13px;background:#fff;border:1px solid #e7ebf2;border-radius:12px;box-shadow:0 14px 30px #94a3b833;color:#64748b}}.custom-picker b{{display:block;margin-bottom:9px;color:#172033;font-size:13px}}.custom-picker div{{display:flex;align-items:center;gap:7px}}.custom-picker input{{width:130px;padding:8px}}.custom-picker button{{flex:0;padding:8px 11px;background:#fff;color:#2563eb}}@media(max-width:820px){{.layout{{grid-template-columns:1fr}}.custom-picker{{right:auto;left:0;min-width:min(360px,calc(100vw - 32px))}}.custom-picker div{{flex-wrap:wrap}}}}</style>
<script src='/static/echarts.min.js'></script><script>
const chartData={chart_data};const unit=v=>{{const u=['B','KB','MB','GB','TB'];let i=0;while(v>=1000&&i<u.length-1){{v/=1000;i++}}return (i? v.toFixed(2):Math.round(v))+' '+u[i]}};window.trafficLastCapture={last_capture};window.trafficNextSyncAt={next_sync * 1000};window.trafficTick=()=>{{const el=document.getElementById('next-sync');if(!el)return;const left=Math.ceil((window.trafficNextSyncAt-Date.now())/1000);el.textContent=left>0?`下次同步 ${{String(Math.floor(left/60)).padStart(2,'0')}}:${{String(left%60).padStart(2,'0')}}`:'正在采集…'}};window.trafficTick();if(!window.trafficCountdownTimer)window.trafficCountdownTimer=setInterval(window.trafficTick,1000);
const chart=echarts.init(document.getElementById('traffic-chart'));chart.setOption({{animation:false,tooltip:{{trigger:'axis',valueFormatter:unit}},legend:{{data:['下载','上传'],top:4}},grid:{{left:55,right:18,top:48,bottom:58}},xAxis:{{type:'category',data:chartData.labels,boundaryGap:false,axisLabel:{{hideOverlap:true,interval:'auto'}}}},yAxis:{{type:'value',axisLabel:{{formatter:unit}},splitLine:{{lineStyle:{{color:'#edf0f5'}}}}}},dataZoom:[{{type:'inside'}},{{type:'slider',height:18,bottom:12,brushSelect:false,labelFormatter:v=>chartData.labels[Math.round(v)]||''}}],series:[{{name:'下载',type:'line',smooth:true,showSymbol:false,areaStyle:{{color:'#dbeafe'}},lineStyle:{{color:'#3b82f6',width:3}},data:chartData.download}},{{name:'上传',type:'line',smooth:true,showSymbol:false,areaStyle:{{color:'#ccfbf1'}},lineStyle:{{color:'#14b8a6',width:3}},data:chartData.upload}}]}});new ResizeObserver(()=>chart.resize()).observe(document.getElementById('traffic-chart'));
</script>"""
    mode_script = """<style>.chart-switch{display:flex;gap:3px;padding:3px;background:#eef2f7;border-radius:8px}.chart-switch button{border:0;border-radius:6px;background:transparent;padding:5px 8px;color:#64748b;font:12px system-ui;cursor:pointer}.chart-switch button.on{background:#fff;color:#172033;box-shadow:0 1px 3px #cbd5e1}</style><script>
if(chartData.allMode){const activeChart=echarts.getInstanceByDom(document.getElementById('traffic-chart'));const header=document.querySelector('.layout .panel header');const legend=document.querySelector('.legend');const switcher=document.createElement('div');switcher.className='chart-switch';switcher.innerHTML='<button class="on" data-mode="total">合计</button><button data-mode="device">按设备</button>';header.insertBefore(switcher,header.querySelector('span'));const colors=['#3b82f6','#14b8a6','#8b5cf6','#f59e0b','#ec4899','#06b6d4'];function setMode(mode){switcher.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.mode===mode));if(mode==='device'){legend.textContent='各设备总流量（上传 + 下载），可点击图例隐藏设备';activeChart.setOption({legend:{data:chartData.devices.map(d=>d.name),top:4},series:chartData.devices.map((d,i)=>({name:d.name,type:'line',smooth:true,showSymbol:false,lineStyle:{width:3,color:colors[i%colors.length]},data:d.data}))},{replaceMerge:['series']})}else{legend.innerHTML='<i></i>下载 <i style="background:#14b8a6"></i>上传';activeChart.setOption({legend:{data:['下载','上传'],top:4},series:[{name:'下载',type:'line',smooth:true,showSymbol:false,areaStyle:{color:'#dbeafe'},lineStyle:{color:'#3b82f6',width:3},data:chartData.download},{name:'上传',type:'line',smooth:true,showSymbol:false,areaStyle:{color:'#ccfbf1'},lineStyle:{color:'#14b8a6',width:3},data:chartData.upload}]},{replaceMerge:['series']})}}switcher.onclick=e=>{const b=e.target.closest('button');if(b)setMode(b.dataset.mode)}}
</script>"""
    ui_script = """<style>#manual-refresh{margin-left:7px;border:0;border-radius:7px;padding:2px 7px;background:#e8f1ff;color:#2563eb;font:15px system-ui;cursor:pointer}#manual-refresh[hidden]{display:none}.layout{position:relative}.layout.is-loading::before{content:'';position:absolute;z-index:10;left:50%;top:52%;width:32px;height:32px;margin:-16px;border:3px solid #dbeafe;border-top-color:#3b82f6;border-radius:50%;animation:traffic-spin .7s linear infinite}.layout.is-loading::after{content:'正在加载数据…';position:absolute;z-index:10;left:50%;top:calc(52% + 26px);transform:translateX(-50%);padding:3px 8px;border-radius:7px;background:#fff;color:#64748b;font-size:12px;box-shadow:0 2px 10px #94a3b833;white-space:nowrap}.layout.is-loading>.panel{opacity:.52;transition:opacity .12s}@keyframes traffic-spin{to{transform:rotate(360deg)}}.controls.is-loading .tabs{opacity:.92}</style><script id='app-ui'>(function(){
const selector=['.metrics','.layout','.bottom'];
const dashboardCache=new Map();
let refreshSequence=0;
function setLoading(on){const layout=document.querySelector('.layout');if(layout)layout.classList.toggle('is-loading',on);document.getElementById('dashboard-form')?.classList.toggle('is-loading',on)}
function cacheKey(url){return url.pathname+url.search}
async function dashboardDocument(url){const key=cacheKey(url);if(dashboardCache.has(key))return dashboardCache.get(key);const response=await fetch('/api/dashboard'+url.search,{cache:'no-store'});if(!response.ok)throw Error('refresh failed');const payload=await response.json();dashboardCache.set(key,payload.document);return payload.document}
async function refreshDashboard(target){const sequence=++refreshSequence;const url=new URL(target,location.origin);setLoading(true);try{const text=await dashboardDocument(url);if(sequence!==refreshSequence)return;const oldChart=document.getElementById('traffic-chart');if(oldChart&&window.echarts){const instance=echarts.getInstanceByDom(oldChart);if(instance)instance.dispose()}const doc=new DOMParser().parseFromString(text,'text/html');selector.forEach(s=>{const old=document.querySelector(s),next=doc.querySelector(s);if(old&&next)old.replaceWith(document.importNode(next,true))});document.querySelector('.head h1').textContent=doc.querySelector('.head h1').textContent;document.querySelector('.head .sub').innerHTML=doc.querySelector('.head .sub').innerHTML;const oldForm=document.getElementById('dashboard-form'),nextForm=doc.getElementById('dashboard-form');if(oldForm&&nextForm)oldForm.replaceWith(document.importNode(nextForm,true));const scripts=[...doc.querySelectorAll('script:not([src]):not(#app-ui)')].map(s=>s.textContent).join('\\n');new Function(scripts)();history.pushState({},'',url.pathname+url.search);bindControls()}finally{if(sequence===refreshSequence)setLoading(false)}}
function prefetchRanges(){const form=document.getElementById('dashboard-form');if(!form)return;['today','week','month'].forEach(range=>{const params=new URLSearchParams(new FormData(form));params.set('range',range);const url=new URL(location.pathname+'?'+params.toString(),location.origin);if(cacheKey(url)===location.pathname+location.search||dashboardCache.has(cacheKey(url)))return;dashboardDocument(url).catch(()=>{})})}
function formUrl(){const form=document.getElementById('dashboard-form');return location.pathname+'?'+new URLSearchParams(new FormData(form)).toString()}
function bindControls(){const form=document.getElementById('dashboard-form');if(!form)return;form.querySelector('select[name=device]').onchange=()=>refreshDashboard(formUrl());form.querySelectorAll('[data-range]').forEach(button=>button.onclick=()=>{form.elements.range.value=button.dataset.range;form.querySelectorAll('[data-range]').forEach(b=>b.classList.toggle('on',b===button));form.querySelector('[data-custom-toggle]')?.classList.remove('on');refreshDashboard(formUrl())});const customToggle=form.querySelector('[data-custom-toggle]'),customPicker=form.querySelector('#custom-picker');if(customToggle&&customPicker)customToggle.onclick=()=>{const opening=customPicker.hidden;customPicker.hidden=!opening;if(opening){form.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));customToggle.classList.add('on')}else{customToggle.classList.toggle('on',form.elements.range.value==='custom');form.querySelectorAll('[data-range]').forEach(b=>b.classList.toggle('on',b.dataset.range===form.elements.range.value))}};form.onsubmit=e=>{e.preventDefault();form.elements.range.value='custom';form.querySelectorAll('[data-range]').forEach(b=>b.classList.remove('on'));form.querySelector('[data-custom-toggle]')?.classList.add('on');refreshDashboard(formUrl())};const manual=document.getElementById('manual-refresh');if(manual)manual.onclick=()=>{const url=new URL(location.href);url.searchParams.delete('snapshot');refreshDashboard(url)}}
async function pollStatus(){try{const status=await fetch('/api/status',{cache:'no-store'}).then(r=>r.json());if(status.last_success>window.trafficLastCapture){window.trafficLastCapture=status.last_success;dashboardCache.clear();if(status.next_sync)window.trafficNextSyncAt=status.next_sync*1000;window.trafficTick();const button=document.getElementById('manual-refresh');if(button)button.hidden=false}}catch(_){}}
bindControls();setTimeout(prefetchRanges,300);if(window.trafficStatusTimer)clearInterval(window.trafficStatusTimer);pollStatus();window.trafficStatusTimer=setInterval(pollStatus,1000)})();</script>"""
    return page.replace("</html>", script + mode_script + ui_script + "</html>")


PAGE = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta http-equiv='refresh' content='60'><title>Traffic</title><style>
:root{--bg:#f5f7fb;--ink:#172033;--muted:#7a8496;--line:#e7ebf2;--blue:#3b82f6}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}.shell{max-width:1200px;margin:auto;padding:28px}nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:42px}.brand{font-size:17px;font-weight:800}.mark{display:inline-grid;place-items:center;width:30px;height:30px;margin-right:9px;border-radius:9px;background:#3b82f6;color:white}.ok{color:#059669;font-size:13px}h1{font-size:32px;letter-spacing:-1px;margin:5px 0}.eyebrow{color:var(--blue);font-size:12px;font-weight:800}.sub,small,header span{color:var(--muted)}.head{display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:24px}.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.controls select,.controls input{border:1px solid var(--line);border-radius:9px;background:#fff;padding:9px;color:var(--ink);font:inherit}.tabs{display:flex;gap:3px;padding:4px;border-radius:10px;background:#e9edf5}.tabs button,.tabs summary{border:0;background:transparent;padding:7px 10px;border-radius:7px;color:#667085;font:inherit;cursor:pointer;list-style:none}.tabs .on{background:white;color:var(--ink);box-shadow:0 1px 4px #d8dee9;font-weight:700}details{position:relative}details[open]{position:absolute;right:0;top:40px;z-index:2;background:white;border:1px solid var(--line);border-radius:10px;padding:8px;box-shadow:0 12px 28px #cbd5e166;white-space:nowrap}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card,.panel{background:white;border:1px solid var(--line);border-radius:16px;box-shadow:0 2px 5px #1e293908}.card{padding:18px}.card span{color:var(--muted)}.card b{display:block;font-size:25px;letter-spacing:-.8px;margin:7px 0}.layout{display:grid;grid-template-columns:1.7fr 1fr;gap:14px;margin-top:14px}.layout.single{grid-template-columns:1fr}.panel{padding:18px}.panel header{display:flex;justify-content:space-between;align-items:center}.panel h2{margin:0;font-size:16px}.legend{color:var(--muted);font-size:12px;margin:8px 0}.legend i{display:inline-block;width:7px;height:7px;border-radius:9px;margin:0 4px 0 12px;background:#3b82f6}.legend i+span{}.chart{width:100%;height:245px}.axis{display:flex;justify-content:space-between;color:#a0a8b8;font-size:11px}.device{display:flex;justify-content:space-between;gap:15px;padding:14px 0;border-top:1px solid var(--line)}.device small{display:block}.device i{display:block;width:220px;max-width:100%;height:5px;background:#edf0f5;border-radius:5px;margin-top:7px}.device em{display:block;height:100%;border-radius:5px;background:var(--blue)}.bottom{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.bottom.single{grid-template-columns:1fr}.split{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:15px}.split div{background:#f8faff;border-radius:11px;padding:12px}.split b{display:block;font-size:17px}.status div{margin-top:14px;color:#4b5563}.status i{color:#059669;font-style:normal;margin-right:8px}@media(max-width:820px){.shell{padding:20px 16px}.head{align-items:start;flex-direction:column}nav{margin-bottom:28px}.metrics,.layout,.bottom{grid-template-columns:1fr}.controls{width:100%}.controls select,.tabs{width:100%}.tabs button{flex:1}.card b{font-size:22px}}
</style><main class='shell'><nav><div class='brand'><span class='mark'>↗</span>Traffic</div><div class='ok'>● 本机统计服务正常</div></nav><section class='head'><div><div class='eyebrow'>自建节点 · 设备统计</div><h1>{{selected}}流量概览</h1><p class='sub'>最后成功采集：{{last}}（北京时间）</p></div>{{controls}}</section><section class='metrics'>{{cards}}</section><section class='layout {{single}}'><section class='panel'><header><h2>流量趋势</h2><span>{{period}}</span></header><div class='legend'><i></i>下载 <i style='background:#14b8a6'></i>上传</div><svg class='chart' viewBox='0 0 700 210' preserveAspectRatio='none'><path d='M0 20H700M0 70H700M0 120H700M0 170H700' stroke='#edf0f5' fill='none'/>{{svg_up}}{{svg_down}}</svg><div class='axis'>{{axis}}</div></section>{{extra}}</section><section class='bottom {{single}}'>{{distribution}}<section class='panel status'><h2>服务状态</h2><div><i>✓</i>3x-ui 本地数据源可用</div><div><i>✓</i>分钟采集与日/5分钟聚合正常</div><div><i>✓</i>页面仅监听 127.0.0.1</div><div><i>✓</i>{{storage}}</div></section></section></main><script>document.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>{document.getElementById('range-value').value=b.dataset.range;document.getElementById('dashboard-form').submit()})</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/dashboard":
            try:
                query = parse_qs(urlparse(self.path).query)
                body = json.dumps({"document": dashboard(query)}, ensure_ascii=False).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            except Exception:
                self.send_error(503, "Dashboard data is unavailable")
            return
        if path == "/api/status":
            try:
                con = sqlite3.connect(DB, uri=True, timeout=5)
                row = con.execute("SELECT value FROM collector_state WHERE key='last_success'").fetchone()
                con.close()
                body = json.dumps({"last_success": int(row[0]) if row else 0, "next_sync": next_sync_epoch()}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            except Exception:
                self.send_error(503, "Collector status is unavailable")
            return
        if path == "/static/echarts.min.js":
            try:
                body = open("/usr/local/lib/traffic-stats/static/echarts.min.js", "rb").read()
                self.send_response(200); self.send_header("Content-Type", "application/javascript"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "public, max-age=31536000, immutable"); self.end_headers(); self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(503, "Chart component is unavailable")
            return
        if path != "/":
            self.send_error(404); return
        try:
            body = dashboard(parse_qs(urlparse(self.path).query)).encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)
        except Exception:
            self.send_error(503, "Statistics are temporarily unavailable")
    def log_message(self, *_): pass


if __name__ == "__main__": ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()
