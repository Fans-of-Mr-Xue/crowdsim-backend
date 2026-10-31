"""Render the original MADRAS case as a portable, single-file HTML attachment.

Run: python -m data_service.services.case_detail_html
Structured project data remains in case_profiles; this renderer only publishes its view.
"""
import html
import json
from pathlib import Path


def esc(value):
    return html.escape(str(value), quote=True)


def links(rows):
    return ''.join(f'<a href="{esc(row["url"])}" target="_blank" rel="noopener noreferrer">{esc(row.get("label") or row.get("title") or row.get("name"))} ↗</a>' for row in rows)


def render_case_html(case):
    p = case['projectProfile']
    series = p['densitySeries']
    threshold = p['densityMeta']['thresholdLines']
    width, height = 920, 300
    def points(column):
        return ' '.join(f'{50 + i * 840 / (len(series) - 1):.2f},{270 - row[column] * 55:.2f}' for i, row in enumerate(series))
    grid = ''.join(f'<line x1="50" y1="{270 - value * 55}" x2="890" y2="{270 - value * 55}" stroke="#cbd5e1" stroke-dasharray="5 5"/><text x="8" y="{275 - value * 55}" fill="#64748b">{value}</text>' for value in threshold)
    chart = f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="热点窗口与观测区平均密度曲线">{grid}<polyline points="{points(2)}" fill="none" stroke="#94a3b8" stroke-width="3"/><polyline points="{points(1)}" fill="none" stroke="#2563eb" stroke-width="3"/><text x="50" y="298">{esc(series[0][0])}</text><text x="800" y="298">{esc(series[-1][0])}</text></svg>'
    cards = ''.join(f'<div class="metric"><small>{esc(row["label"])}</small><strong>{esc(row["value"])}</strong><span>{esc(row.get("unit", ""))}</span></div>' for row in p['heroStats'])
    datasets = ''.join(f'<article><h3>{esc(row["title"])}</h3><strong>{esc(row["value"])}</strong><p>{esc(row["body"])}</p></article>' for row in p['datasetCards'])
    signals = ''.join(f'<article><small>{esc(row["label"])}</small><h3>{esc(row["value"])}</h3><p>{esc(row["note"])}</p></article>' for row in p['signalHighlights'])
    cameras = ''.join(f'<details id="{esc(row["id"])}"><summary>{esc(row["name"])} · {esc(row["category"])}</summary><p>{esc(row["role"])}</p><p>坐标：{esc(row["coord"])}</p><div class="links">' + ''.join(f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">视频 {i + 1} ↗</a>' for i, url in enumerate(row['videos'])) + '</div></details>' for row in p['cameras'])
    evidence = ''.join(f'<figure><img src="{esc(row["src"])}" alt="{esc(row["title"])}" loading="lazy"><figcaption><strong>{esc(row["title"])}</strong><p>{esc(row["caption"])}</p></figcaption></figure>' for row in p['evidenceImages'])
    notes = ''.join(f'<li>{esc(note)}</li>' for note in p['analyticNotes'])
    rows = ''.join(f'<tr><td>{esc(row[0])}</td><td>{row[1]}</td><td>{row[2]}</td></tr>' for row in series)
    network = ''.join(f'<tr><td>{esc(row["name"])}</td><td>{esc(row["coord"])}</td><td>{esc(row["role"])}</td></tr>' for row in p['fieldNetwork'])
    data = json.dumps({'cameras': p['cameras'], 'mapConfig': p['mapConfig']}, ensure_ascii=False).replace('<', '\\u003c')
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(p['heroTitle'])}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fc;color:#1e293b;font:15px/1.75 system-ui,sans-serif}}main{{max-width:1280px;margin:auto;padding:32px}}header,section{{background:white;border:1px solid #e2e8f0;border-radius:18px;padding:28px;margin-bottom:22px}}header{{background:linear-gradient(120deg,#eff6ff,white)}}h1{{font-size:32px;margin:10px 0}}h2{{font-size:22px;margin:0 0 12px}}h3{{margin:6px 0;font-size:17px}}p{{color:#64748b}}small{{color:#64748b}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}}.grid.three{{grid-template-columns:repeat(3,minmax(0,1fr))}}.metric,article{{background:#f8fafc;padding:18px;border-radius:12px}}.metric strong{{display:block;font-size:28px;color:#2563eb}}.hero-image{{width:100%;height:360px;object-fit:contain;margin-top:24px;background:#f8fafc;border-radius:12px}}.links{{display:flex;gap:14px;flex-wrap:wrap}}a{{color:#2563eb;text-decoration:none}}a:hover{{text-decoration:underline}}svg{{width:100%;font:13px system-ui}}.chart-legend{{display:flex;gap:24px;color:#64748b}}.chart-legend span:first-child{{color:#2563eb}}#map{{height:440px;border-radius:12px;background:#f1f5f9;margin-bottom:16px}}details{{border-top:1px solid #e2e8f0;padding:14px 0}}summary{{cursor:pointer;font-weight:600}}.evidence{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}figure{{margin:0}}figure img{{height:340px;width:100%;object-fit:contain;background:#f8fafc}}figcaption{{padding:12px 0}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{border-bottom:1px solid #e2e8f0;padding:10px;text-align:left}}.table-wrap{{overflow:auto}}@media(max-width:760px){{main{{padding:14px}}header,section{{padding:18px}}.grid,.grid.three{{grid-template-columns:1fr 1fr}}.evidence{{grid-template-columns:1fr}}h1{{font-size:26px}}}}
</style></head><body><main>
<header><small>{esc(p['eyebrow'])}</small><h1>{esc(p['heroTitle'])}</h1><p>{esc(p['heroLead'])}</p><p>{esc(p['locationLabel'])} · {esc(p['observationPeriod'])}</p><div class="grid">{cards}</div><img class="hero-image" src="{esc(p['overviewImage'])}" alt="里昂灯光节观测场域"></header>
<section><h2>多源观测数据</h2><div class="grid three">{datasets}</div><div class="grid" style="margin-top:16px">{signals}</div></section>
<section><h2>{esc(p['densityMeta']['title'])}</h2><p>{esc(p['densityMeta']['subtitle'])}</p><div class="chart-legend"><span>● {esc(p['densityMeta']['hotspotLabel'])}</span><span>● {esc(p['densityMeta']['globalLabel'])}</span><span>单位：人/m²</span></div>{chart}<ul>{notes}</ul><details><summary>查看完整密度时序（{len(series)} 条）</summary><div class="table-wrap"><table><thead><tr><th>时间</th><th>热点窗口</th><th>观测区平均</th></tr></thead><tbody>{rows}</tbody></table></div></details></section>
<section><h2>11 机位观测网络</h2><p>点击地图机位或展开列表查看观测范围与原始视频。底图加载失败时仍可查看下方全部机位。</p><div id="map"></div>{cameras}<details><summary>主要观测断面</summary><div class="table-wrap"><table>{network}</table></div></details></section>
<section><h2>现场证据图</h2><div class="evidence">{evidence}</div></section><section><h2>原始来源</h2><div class="links">{links(p['sourceLinks'])}</div></section>
</main><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const data = {data};
if (window.L) {{
const map = L.map('map').setView(data.mapConfig.center, data.mapConfig.zoom);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{attribution:'© OpenStreetMap contributors',maxZoom:20}}).addTo(map);
const bounds=[];
for (const camera of data.cameras) {{
bounds.push(camera.location);
const label=document.createElement('strong');label.textContent=camera.name;
L.circleMarker(camera.location, {{radius:7,color:'#2563eb',fillOpacity:.8}}).addTo(map).bindTooltip(label).on('click',()=>{{const detail=document.getElementById(camera.id);detail.open=true;detail.scrollIntoView({{behavior:'smooth',block:'center'}})}});
if(camera.field?.length) L.polygon(camera.field,{{color:'#2563eb',weight:1,fillOpacity:.12}}).addTo(map);
}}
map.fitBounds(bounds,{{padding:[35,35],maxZoom:18}});
}}
</script></body></html>'''


if __name__ == '__main__':
    resources = Path(__file__).resolve().parents[1] / 'resources'
    case = next(c for c in json.loads((resources / 'case_profiles.json').read_text()) if c['id'] == 'madras-lyon-2022')
    target = resources / 'case_pages' / 'madras-lyon-2022.html'
    target.parent.mkdir(exist_ok=True)
    target.write_text(render_case_html(case), encoding='utf-8')
    print(f'Created HTML detail page ({target.stat().st_size} bytes)')
