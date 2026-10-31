"""Publish pedestrian assets to the environment library, then pin task bindings."""
from shapely.geometry import shape
from ..schemas.environments import FIELDS, validate_roads
from ..repositories.knowledge_documents import now
from .pedestrian_network import BUILDER_VERSION, clip_network


def publish_network(repository, environment_id, network, filename, *, task_boundary=None):
    if not isinstance(network, dict) or not isinstance(network.get('features'), list):
        raise ValueError('请提供有效的GeoJSON道路FeatureCollection')
    environment = repository.get(environment_id)
    if environment is None:
        raise ValueError('请先在环境库创建或选择区域，再导入该区域路网')
    for feature in network.get('features', []):
        if not isinstance(feature, dict):
            raise ValueError('道路要素必须为对象')
        p = feature.get('properties') or {}
        if not isinstance(p, dict):
            raise ValueError('道路属性必须为对象')
        if not p.get('highway') and not p.get('foot'):
            p['highway'] = 'footway'
        feature['properties'] = p
    network = validate_roads(network)
    source = {**network.get('source', {}), 'provider': 'user_import', 'label': '环境库导入步行路网',
              'filename': filename, 'coordinateSystem': 'WGS84', 'retrievedAt': now(), 'builder_version': BUILDER_VERSION}
    network.update(source=source, temporal=network.get('temporal') or {'historical_verified': False, 'note': '用户导入，适用日期与现场情况待核对。'})
    network = clip_network(network, environment['geometry'])
    if not network['features']:
        raise ValueError('导入文件没有落在所选环境库区域内的步行通道')
    if task_boundary and not any(shape(feature['geometry']).intersects(shape(task_boundary)) for feature in network['features']):
        raise ValueError('所选环境库路网与任务范围不相交，请重新选择区域或文件')
    payload = {key: value for key, value in environment.items() if key in FIELDS and key != 'polygon'}
    payload.update(roads=network, roadsStatus='available', roadsMessage=f"已导入 {len(network['features'])} 条步行通道；未提供的宽度保留为缺失")
    return repository.save(payload, environment['id'], environment['revision'])
