"""Restore common scene information into the isolated portrait, without solver settings."""
import json
import urllib.request

TARGET = '6f816721c0bf4e1396a3683ee8c434f2'
SCENE_SOURCE = '4d9b7e94c91d4eb7a1d52cd722ba5c43'
POPULATION_SOURCE = '6f3fb29cda9646bd9599aceeab3d72fc'
BASE = 'http://127.0.0.1:8768/api/crowdSim/analysis-tasks/'


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(identifier, payload=None):
        request = urllib.request.Request(BASE + identifier, data=json.dumps(payload).encode() if payload else None,
                                         headers={'Content-Type': 'application/json'}, method='PUT' if payload else 'GET')
        with opener.open(request, timeout=60) as response:
            return json.load(response)['data']
    task, source, population_source = (api(identifier) for identifier in (TARGET, SCENE_SOURCE, POPULATION_SOURCE))
    portrait = task['components']['domain_portrait']
    if portrait.get('mode') != 'preview' or TARGET in (SCENE_SOURCE, POPULATION_SOURCE):
        raise SystemExit('Only the independent preview may be updated.')
    if 'scene' in portrait:
        print('Scene already restored; keeping user edits.'); return
    keys = ('initial_crowd', 'arrival_rate_per_minute', 'dwell_minutes', 'free_speed_mps', 'path_width_m',
            'warning_density_p_m2', 'attraction_point', 'exit_points', 'exit_labels', 'exit_points_note', 'entrance_shares')
    portrait['scene'] = {
        'settings': {key: source['components']['forecast_setup'][key] for key in keys if key in source['components']['forecast_setup']},
        'population': population_source['population'], 'observations': [],
        'provenance': {'settings_task_id': SCENE_SOURCE, 'settings_revision': source['revision'],
                       'population_task_id': POPULATION_SOURCE, 'population_revision': population_source['revision']},
        'note': '从原任务保留的通用场景假设，非实测。仅保存于当前临时领域模型，合并时再供实验读取。',
        'population_note': '原任务保存的人群画像假设；没有现场统计依据，不据此推算空间人数。',
    }
    saved = api(TARGET + '/components/domain_portrait', {'data': portrait, 'revision': task['revision']})
    print(json.dumps({'task_id': TARGET, 'revision': saved['revision'], 'scene_fields': len(portrait['scene']['settings'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
