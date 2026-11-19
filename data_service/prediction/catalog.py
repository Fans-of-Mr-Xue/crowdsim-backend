"""Only executable forecasting methods appear in this catalog."""
METHODS = [
    {'id': 'conservation', 'name': '分区流量守恒', 'version': '1.1.0', 'recommended': True, 'engine': 'NetworkSolver',
     'description': '三角基本图、发送/接收能力与同步流量守恒，计算入口排队和路网积累。',
     'equation': 'Nᵢ(t+Δt)=Nᵢ(t)+ΣFⱼᵢ−ΣFᵢⱼ；q=min(v₀ρ,w(ρmax−ρ))',
     'source': 'https://doi.org/10.1016/0191-2615(94)90002-7',
     'boundary': '采用 CTM 的守恒离散思路构建行人路网模型，属于工程适配，不模拟人体接触。'},
    {'id': 'collision_free_speed', 'name': 'CFS 无碰撞速度模型', 'version': '1.1.0', 'recommended': True, 'engine': 'JuPedSim 1.4.2',
     'description': '在可行走面内逐人行走，根据前方间距调整速度，输出位置、密度与离场量。',
     'equation': 'v=min(v₀,max(0,(前方净间距)/T))；方向由目标与避让共同决定',
     'source': 'https://arxiv.org/abs/1512.05597',
     'implementation_source': 'https://www.jupedsim.org/stable/pedestrian_models/index.html',
     'boundary': '调用 JuPedSim CollisionFreeSpeedModel；支持单层连通场地，保留完整研究时段。采用缓存航点引导，身体运动步长 0.05 秒；按实际在场人数、计算量、运行耗时和输出量管理预算。'},
    {'id': 'social_force', 'name': '社会力模型 SFM', 'version': '1.2.0', 'recommended': True, 'engine': 'JuPedSim 1.4.2 + sfmfix2',
     'description': '逐人计算目标驱动和人与人、人与障碍物的排斥作用，模拟速度变化与绕行。',
     'equation': 'm·dv/dt=m(v₀e−v)/τ+Σf人与人+Σf人与墙',
     'source': 'https://doi.org/10.1103/PhysRevE.51.4282',
     'implementation_source': 'https://www.jupedsim.org/stable/pedestrian_models/index.html',
     'boundary': '调用 JuPedSim SocialForceModel，修复墙体摩擦方向并对接触摩擦采用半隐式积分，步长 0.01 秒；不用于计算踩踏、人体伤害或真实挤压力。资源预算同 CFS，参数尚未进行现场标定。'},
    {'id': 'weidmann', 'name': 'Weidmann 速度—密度模型', 'version': '1.0.0',
     'description': '非线性速度—密度关系驱动路网流量，显式计算对向流造成的速度折减。',
     'equation': 'v(ρ)=v₀[1−exp(−1.913(1/ρ−1/ρmax))]；q=ρv',
     'source': 'https://doi.org/10.3929/ethz-b-000242008',
     'boundary': '经验基本图不是场地标定结果；自由速度和拥挤密度须用现场数据校准。'},
    {'id': 'stochastic_cells', 'name': '随机容量元胞模型', 'version': '1.0.0',
     'description': '整数人数、随机移动、接收容量和汇流摩擦，逐步演化容量元胞。',
     'equation': 'M~Binomial(N,p)，p=min(1,v₀Δt/L)(1−ρ/ρmax)；汇流冲突按摩擦概率停步',
     'source': 'https://arxiv.org/abs/cond-mat/0209383',
     'boundary': '受元胞冲突机制启发的中观容量模型，不是原论文一人一格的地板场复现。'},
    {'id': 'knowledge_network', 'name': 'CrowdSim 领域知识增强', 'version': '1.0.0',
     'description': '在 Weidmann 路网模型上加入驻留、拥堵感知、出口认知和引导响应延迟。',
     'equation': 'Pr(route r)∝exp(−β·costᵣ)；cost=行程时间+拥堵等待+认知代价',
     'source': 'https://doi.org/10.1080/19475683.2012.669216',
     'boundary': '行为系数是可编辑情景假设；记录触发规则，尚未使用本地观测拟合。'},
]
from .consultation_catalog import MODEL_DETAILS
for item in METHODS:
    item.update(MODEL_DETAILS.get(item['id'], {}))

METHOD_BY_ID = {item['id']: item for item in METHODS}


def method_catalog(task=None, *, include_legacy=False):
    from .custom_formulas import normalize_custom_methods, custom_method_metadata
    value = (task or {}).get('components', {}).get('custom_methods', {'items': []})
    return [*(row for row in METHODS if include_legacy or row.get('recommended')), *(custom_method_metadata(item) for item in normalize_custom_methods(value)['items'])]
