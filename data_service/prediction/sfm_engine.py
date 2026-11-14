"""Verify corrected wall friction and semi-implicit integration before a long SFM run."""
from functools import lru_cache
from importlib.metadata import version
import math


@lru_cache(maxsize=1)
def verified_sfm_engine():
    import jupedsim as jps
    # With driving acceleration and normal forces zero, wall friction must
    # reduce tangential velocity. Semi-implicit contact: 1/(1+dt*kappa*overlap/m).
    sim = jps.Simulation(model=jps.SocialForceModel(body_force=0),
                         geometry=[(-5, 0), (5, 0), (5, 5), (-5, 5)], dt=.001)
    stage = sim.add_direct_steering_stage()
    journey = sim.add_journey(jps.JourneyDescription([stage]))
    identifier = sim.add_agent(jps.SocialForceModelAgentParameters(
        position=(0, .15), velocity=(1, 0), radius=.2, desired_speed=1,
        obstacle_scale=0, journey_id=journey, stage_id=stage))
    sim.agent(identifier).target = (4, .15)
    sim.iterate()
    vx, vy = sim.agent(identifier).model.velocity
    if not (math.isclose(vx, 1 / 1.15, abs_tol=1e-9) and abs(vy) < 1e-9):
        raise ValueError('社会力引擎的墙体摩擦校验失败；请用当前后端 Python 执行 scripts/install_sfm_wallfix.py 安装修复后重启数据服务。')
    return {'distribution_version': version('jupedsim'), 'wall_friction_check': 'passed',
            'contact_friction_integration': 'semi_implicit_local_velocity',
            'wall_friction_velocity_after_mps': vx}
