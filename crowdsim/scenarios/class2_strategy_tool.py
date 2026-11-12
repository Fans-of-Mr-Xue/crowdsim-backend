"""Compact model input for the read-only second-class strategy preview."""

MAX_ACTIONS = 16
INTERVAL_ACTIONS = {'set_inflow_rate', 'set_edge_capacity', 'publish_guidance'}
ACTION_GUIDE = {
    'release_flow': '开始秒 人流 每批人数 间隔秒 | 仅限起点仍有人的人流，开始秒为第一批，最后一批须在结束前放行',
    'set_inflow_rate': '开始秒 结束秒 入口 inbound或outbound 放行比例 人流 | 入口放行能力倍率0～1，*代表所有人流',
    'set_edge_capacity': '开始秒 结束秒 路段 限速倍率 人流 | 倍率0.1～1，*代表所有人流',
    'reroute_group': '开始秒 人流 路线规则 | 保留原终点',
    'set_route_distribution': '开始秒 人流 比例=路线规则 比例=路线规则 ... | 2～4个分流项，至少两项大于0，比例合计为1',
    'publish_guidance': '开始秒 结束秒 人流 路段 引导消息 | 人流、路段可填*表示不限，发送给开始时匹配的人，消息1～500字',
    'observe_only': '开始秒 人流 路段 | 人流、路段可填*，记录一帧全场已选指标、匹配人数及可用的目标平均速度',
}
ACTIONS = frozenset(ACTION_GUIDE)


def strategy_tool(available, flow_aliases):
    effects = {
        'publish_guidance': '仅发送和记录消息；不会按文字自动靠右、分侧、改变路宽、速度或路线。',
        'set_edge_capacity': '仅设置行人速度上限倍率，不改变路宽或物理容量，不保证降低密度；已有速度更低时可能不起约束作用。',
        'reroute_group': '按指定避让或代价规则从各人当前位置重算路线，保留原终点；不保证存在替代道路。',
        'set_route_distribution': '按比例分配路线规则，保留原终点，不扩容道路。',
        'release_flow': '暂缓并分批起步，会在起点形成等待；不消除人数。',
        'set_inflow_rate': '按方向控制入口放行能力，会形成上游等待；不减少总人数。',
        'observe_only': '只记录观测，不改变运动。',
    }
    return {
        'name': 'submit_class2_strategy',
        'flowAliases': flow_aliases.copy(),
        'commands': {name: guide for name, guide in ACTION_GUIDE.items() if name in available},
        'effects': {name: effect for name, effect in effects.items() if name in available},
        'routePolicy': 'keep 保留原路线（仅分流可用），avoid:路段,路段 避让，risk:路段=权重,路段=权重 调整选路代价（0.1～10）。避让与权重可用+连接。',
    }


def _policy(value):
    if value == 'keep':
        return {'keepRoute': True}
    result = {}
    for part in value.split('+'):
        kind, values = part.split(':', 1)
        if kind == 'avoid' and 'avoidEdgeIds' not in result:
            result['avoidEdgeIds'] = values.split(',')
        elif kind == 'risk' and 'riskWeights' not in result:
            result['riskWeights'] = {}
            for pair in values.split(','):
                edge, weight = pair.split('=', 1)
                if edge in result['riskWeights']:
                    raise ValueError('风险权重路段重复')
                result['riskWeights'][edge] = float(weight)
        else:
            raise ValueError('路线规则应使用 keep、avoid: 或 risk:')
    return result


def _action(line, index, aliases):
    if not isinstance(line, str) or not 1 <= len(line) <= 2048 or '\n' in line or '\r' in line:
        raise ValueError('动作必须是长度不超过2048的单行文字')
    command, rest = line.split(maxsplit=1)
    if command not in ACTIONS:
        raise ValueError(f'未知动作 {command}')
    start, *words = rest.split(maxsplit=4 if command == 'publish_guidance' else -1)
    action = {'actionId': f'a{index}', 'actionType': command, 'startSeconds': float(start)}
    if command in INTERVAL_ACTIONS:
        end, *words = words
        action['endSeconds'] = float(end)
    target, params = {}, {}
    if command == 'release_flow':
        flow, size, interval = words
        params = {'batchSize': int(size), 'intervalSeconds': float(interval)}
    elif command == 'set_inflow_rate':
        entry, direction, rate, flow = words
        target = {'entryId': entry, 'direction': direction}
        params = {'rate': float(rate)}
    elif command == 'set_edge_capacity':
        edge, multiplier, flow = words
        target = {'edgeId': edge}
        params = {'multiplier': float(multiplier)}
    elif command == 'reroute_group':
        flow, rule = words
        params = _policy(rule)
    elif command == 'set_route_distribution':
        flow, *rules = words
        distributions = []
        for rule in rules:
            ratio, policy = rule.split('=', 1)
            distributions.append({'ratio': float(ratio), **_policy(policy)})
        params = {'distributions': distributions}
    elif command == 'publish_guidance':
        flow, edge, message = words
        target = {} if edge == '*' else {'edgeId': edge}
        params = {'message': message}
    else:
        flow, edge = words
        target = {} if edge == '*' else {'edgeId': edge}
    if flow != '*':
        target['flowId'] = aliases.get(flow, flow)
    return {**action, 'target': target, 'parameters': params}


def parse_action_lines(lines, aliases):
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_ACTIONS:
        raise ValueError(f'候选须包含 1～{MAX_ACTIONS} 条动作')
    actions = []
    for index, line in enumerate(lines, 1):
        try:
            actions.append(_action(line, index, aliases))
        except ValueError as error:
            raise ValueError(f'第 {index} 条动作的命令、参数顺序或数值有误，请按动作目录填写') from error
    return actions
