"""Server resource policy, separate from the user's research scenario."""
import os


def positive_int(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


MAX_PEOPLE = positive_int('CROWDSIM_MICRO_MAX_ACTIVE', 3000)
MAX_AGENT_STEPS = positive_int('CROWDSIM_MICRO_MAX_AGENT_STEPS', 2_000_000_000)
MAX_RUNTIME_SECONDS = positive_int('CROWDSIM_MICRO_MAX_SECONDS', 1800)
MAX_SAVED_POSITIONS = positive_int('CROWDSIM_MICRO_MAX_SAVED_POSITIONS', 500_000)

RUNTIME_NOTE = (f'逐人模型沿用完整研究时间窗（平台支持 1 分钟至 12 小时），CFS 积分步长 0.05 秒，社会力 0.01 秒。'
                f'当前服务预算：同时在场最多 {MAX_PEOPLE} 人，单模型每次最多运行 {MAX_RUNTIME_SECONDS / 60:g} 分钟；'
                '另按实际在场人数累计计算量和保存的位置数量控制资源。离场人员不再占用后续计算预算；超预算会说明原因，不自动缩短时段或减少人数。')


def runtime_budget():
    return dict(max_active_agents=MAX_PEOPLE, max_agent_steps=MAX_AGENT_STEPS,
                max_runtime_seconds=MAX_RUNTIME_SECONDS, max_saved_positions=MAX_SAVED_POSITIONS)
