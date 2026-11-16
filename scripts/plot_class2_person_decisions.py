"""Render archived local observations and decisions; never reruns the simulation."""
import base64
import csv
from collections import defaultdict
import html
import json
import math
from pathlib import Path
import textwrap
import zipfile

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Polygon

ROOT = Path(__file__).resolve().parents[1]
RUN = 'run-72ff104f89604245ac84370ae41750e9'
PERSON = 'class2.d8ce7291-85d7-4264-9ba0-638d1b44deb5.00018'
OUT = ROOT / 'outputs/class2_person_00018_decisions'
OUT.mkdir(parents=True, exist_ok=True)
data = json.loads((ROOT / 'config/during_presets/memorial_flow_network_v1.json').read_text())
origin = np.array(data['originXY'])
frames = defaultdict(dict)
with (ROOT / 'runs' / RUN / 'trajectory.csv').open() as handle:
    for row in csv.DictReader(handle):
        frames[float(row['time'])][row['person_id']] = row
assert len(frames) == 200 and all(len(frame) == 333 for frame in frames.values())
records = [json.loads(line) for line in (ROOT / 'docs/validation/class2_local_llm_integrated_20261114.decisions.jsonl').read_text().splitlines()]
records = [row for row in records if row['person_id'] == PERSON]
assert len(records) == 8

plt.rcParams.update({'font.family': ['Hiragino Sans GB', 'Arial Unicode MS', 'sans-serif'],
                     'axes.unicode_minus': False, 'font.size': 11})
ink, muted = '#18354b', '#61778a'
orange, blue, purple = '#ef6c35', '#257bbb', '#9251b5'
labels = {'continue': '继续前进', 'wait': '等待 2 秒', 'slow_down': '减速至 0.60 米/秒', 'choose_path': '选择局部路径'}
files, audits = [], []

def point(row):
    return np.array([float(row['x']), float(row['y'])])

def style(ax):
    ax.set_aspect('equal')
    ax.set_facecolor('#fafcfe')
    ax.tick_params(colors=muted, labelsize=9)
    ax.grid(color='#e5ebf0', linewidth=.6, zorder=0)
    for edge in ax.spines.values():
        edge.set_color('#d2dee7')

for index, record in enumerate(records, 1):
    timestamp, context = record['time'], record['context']
    frame = frames[timestamp]
    own = point(frame[PERSON])
    local = own-origin
    radius = context['perception']['radius_m']
    neighbours = context['nearby_people']
    # Check that plotted model observations match the same archived SUMO frame.
    actual = [point(row)-own for pid, row in frame.items()
              if pid != PERSON and np.linalg.norm(point(row)-own) <= radius]
    assert len(actual) == len(neighbours)
    unmatched = list(actual)
    for neighbour in neighbours:
        position = np.array(neighbour['relative_xy_m'])
        closest = min(range(len(unmatched)), key=lambda i: np.linalg.norm(unmatched[i]-position))
        assert np.linalg.norm(unmatched.pop(closest)-position) < .001
    assert not record['fallback_error'] and record['execution']['status'] == 'applied'
    fig = plt.figure(figsize=(16, 9), dpi=150, facecolor='white')
    fig.text(.045, .945, f'活动行人 00018 · 第 {index:02d} 次决策', fontsize=24, weight='bold', color=ink)
    fig.text(.045, .903, f'测试事中18  /  真实运行回放  /  T + {timestamp:.1f} 仿真秒  /  决策前快照', color=muted, fontsize=12)
    overview = fig.add_axes([.045, .20, .23, .62])
    detail = fig.add_axes([.32, .20, .36, .62])
    text_ax = fig.add_axes([.72, .17, .25, .65]); text_ax.axis('off')
    style(overview); style(detail)
    overview.add_patch(Polygon(data['scope'], facecolor='#eff3f6', edgecolor='#a4b6c5', lw=.8, zorder=0))
    for lane in data['lanes']:
        path = np.array(lane['path'])
        overview.plot(path[:, 0], path[:, 1], color='#bac9d4', lw=1.6, zorder=1)
        relative = path-local
        detail.plot(relative[:, 0], relative[:, 1], color='#d7e0e6', lw=2, zorder=1)
    everyone = np.array([point(row)-origin for row in frame.values()])
    overview.scatter(everyone[:, 0], everyone[:, 1], s=5, color='#b2bdc6', alpha=.65, zorder=2)
    history = np.array([point(frames[t][PERSON])-origin for t in sorted(frames) if t <= timestamp])
    overview.plot(history[:, 0], history[:, 1], color=orange, lw=2, zorder=4)
    overview.add_patch(Circle(local, radius, facecolor=blue, alpha=.13, zorder=3))
    overview.scatter(*local, s=65, color=orange, edgecolors='white', zorder=5)
    overview.set(xlim=(-10, 165), ylim=(-10, 270), xlabel='局部东西坐标 / 米', ylabel='局部南北坐标 / 米')
    overview.set_title('研究区定位 · 灰点为全场人员', loc='left', fontsize=11, color=ink, pad=12)
    overview.text(.5, -.14, '总览仅供读图定位，不作为模型全局输入', transform=overview.transAxes,
                  ha='center', color=muted, fontsize=9)
    detail.add_patch(Circle((0, 0), radius, facecolor='#edf6fc', edgecolor=blue,
                            linestyle=(0, (4, 3)), lw=1.4, zorder=0))
    for road in context['local_roads']:
        path = np.array(road['relative_path_m'])
        detail.plot(path[:, 0], path[:, 1], color=blue, lw=3, solid_capstyle='round', zorder=2)
    nearby = np.array([person['relative_xy_m'] for person in neighbours])
    detail.scatter(nearby[:, 0], nearby[:, 1], s=24, color='#28a6b4', edgecolors='white', linewidths=.5, zorder=4)
    moving = [p for p in neighbours if p['speed_mps'] > .05]
    if moving:
        positions = np.array([p['relative_xy_m'] for p in moving])
        angles = np.radians([p['heading_degrees'] for p in moving])
        detail.quiver(positions[:, 0], positions[:, 1], np.sin(angles), np.cos(angles),
                      angles='xy', scale_units='xy', scale=.7, color='#168d9b', width=.004, zorder=4)
    for option in context['local_options']:
        x, y = option['waypoint_relative_xy_m']
        detail.scatter(x, y, marker='D', s=65, color=purple, edgecolors='white', zorder=5)
        detail.annotate(option['id'], (x, y), xytext=(5, 7), textcoords='offset points', color=purple, fontsize=9)
    detail.scatter(0, 0, s=120, color=orange, edgecolors='white', linewidths=1.5, zorder=7)
    theta = math.radians(context['self']['heading_degrees'])
    detail.annotate('', (2.4*math.sin(theta), 2.4*math.cos(theta)), (0, 0),
                    arrowprops={'arrowstyle':'->', 'color':orange, 'lw':2}, zorder=6)
    dx, dy = context['goal']['direction_east_north']
    detail.annotate('', (radius*.72*dx, radius*.72*dy), (radius*.4*dx, radius*.4*dy),
                    arrowprops={'arrowstyle':'->', 'color':'#526371', 'lw':1.4, 'linestyle':'--'}, zorder=5)
    detail.text(radius*.78*dx, radius*.78*dy, '目标方向', color='#526371', fontsize=9, ha='center')
    span = radius*1.25
    detail.set(xlim=(-span, span), ylim=(-span, span), xlabel='相对东西距离 / 米', ylabel='相对南北距离 / 米')
    detail.set_title(f'局部感知 · 半径 {radius:.2f} 米', loc='left', fontsize=12, color=ink, pad=12)
    detail.text(.5, -.14, '邻居箭头仅表示行走方向；静止者不绘箭头', transform=detail.transAxes,
                ha='center', color=muted, fontsize=9)
    action = record['decision']['action']
    text_ax.text(0, 1, 'GLM 的选择', fontsize=13, color=muted, va='top')
    text_ax.text(0, .92, labels[action], fontsize=20, color=orange, weight='bold', va='top')
    text_ax.text(0, .80, '模型理由（原文）', fontsize=12, color=ink, weight='bold', va='top')
    text_ax.text(0, .74, '\n'.join(textwrap.wrap(record['decision']['reason'], width=21)),
                 fontsize=12, color=ink, linespacing=1.65, va='top')
    facts = [f'观测到附近行人：{len(neighbours)} 人',
             f'局部道路片段：{len(context["local_roads"])} 段',
             f'可选局部路径：{len(context["local_options"])} 个',
             f'当前实际速度：{context["self"]["speed_mps"]:.2f} 米/秒',
             f'个人模型请求耗时：{record["model_seconds"]:.2f} 秒',
             '执行结果：成功应用；未触发回退']
    text_ax.text(0, .36, '\n'.join(facts), fontsize=11, color=muted, linespacing=1.8, va='top')
    fig.legend(handles=[Line2D([], [], marker='o', color='none', markerfacecolor=orange, label='选中行人'),
                        Line2D([], [], marker='o', color='none', markerfacecolor='#28a6b4', label='模型观察到的行人'),
                        Line2D([], [], color=blue, lw=3, label='模型观察到的道路片段'),
                        Line2D([], [], marker='D', color='none', markerfacecolor=purple, label='局部路径候选（如有）')],
               loc='lower left', bbox_to_anchor=(.04, .065), ncol=4, frameon=False, fontsize=10)
    fig.text(.045, .035, '感知采用距离半径，未模拟建筑遮挡。模型理由是其输出，不等同于已验证的环境事实。图中不绘制未来轨迹。',
             fontsize=10, color=muted)
    name = f'decision_{index:02d}_t{timestamp:05.1f}.png'
    fig.savefig(OUT/name, dpi=150, facecolor='white'); plt.close(fig)
    files.append(name)
    audits.append({'image': name, 'time': timestamp, 'person_id': PERSON, 'position_xy': own.tolist(),
                   'neighbour_count': len(neighbours), 'matched_trajectory_frame': True,
                   'decision': record['decision'], 'execution': record['execution']})

fig, axes = plt.subplots(4, 2, figsize=(16, 18), dpi=120)
for ax, name in zip(axes.flat, files):
    ax.imshow(plt.imread(OUT/name)); ax.axis('off')
fig.subplots_adjust(left=.005, right=.995, top=.995, bottom=.005, hspace=.02, wspace=.015)
fig.savefig(OUT/'overview.png', facecolor='#e9eef2'); plt.close(fig)
(OUT/'audit.json').write_text(json.dumps({'run_id': RUN, 'trajectory_rows': 66600, 'frames': 200,
    'persons_per_frame':333, 'selected_person': PERSON, 'skipped_decision_times':[40.5,90.5], 'images':audits}, ensure_ascii=False, indent=2))
gallery = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>行人00018：逐次感知与决策</title>',
           '<style>body{margin:24px auto;max-width:1500px;background:#eef3f6;color:#18354b;font-family:system-ui}img{width:100%;background:white;margin:12px 0 28px}p{line-height:1.8}a{color:#257bbb}</style>',
           '<h1>活动行人00018 · 8次真实决策</h1><p>每张图对应模型决策前的同一仿真快照。40.5秒、90.5秒处于内部路口，未调用模型。灰色全图仅供定位；圆内道路、邻居及候选来自实际模型输入。感知未考虑建筑遮挡。</p>']
for name, row in zip(files, audits):
    encoded = base64.b64encode((OUT/name).read_bytes()).decode()
    gallery.append(f'<h2>T + {row["time"]:.1f} 秒 · {html.escape(labels[row["decision"]["action"]])}</h2><img alt="{name}" src="data:image/png;base64,{encoded}">')
gallery.append('</html>')
(OUT/'index.html').write_text('\n'.join(gallery))
with zipfile.ZipFile(OUT/'decision_images.zip', 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
    for name in files + ['index.html', 'overview.png', 'audit.json']:
        bundle.write(OUT/name, name)
print(json.dumps({'output': str(OUT), 'images': files, 'position_records': 66600, 'matching_audits':len(audits)},ensure_ascii=False))
