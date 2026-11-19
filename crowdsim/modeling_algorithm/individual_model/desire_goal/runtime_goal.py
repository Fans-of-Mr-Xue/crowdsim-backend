"""Goal intentions over known targets only; no physical route or facility invention."""
from dataclasses import dataclass
from crowdsim.modeling_algorithm.contracts import DesireState


@dataclass(frozen=True)
class GoalIntent:
    primary: str
    target_ids: tuple[str, ...]
    local_action: str | None
    reason: str
    locked: bool = False

    def as_dict(self):
        return {'primary': self.primary, 'target_ids': list(self.target_ids),
                'local_action': self.local_action, 'reason': self.reason, 'locked': self.locked}


def goal_intent(desire: DesireState, state, targets, *, locked=False, allow_local_actions=False):
    ids = tuple(target['id'] for target in targets)
    if locked or state.poi_plan_active:
        local = ({'rest': 'wait', 'avoid_crowding': 'slow_down', 'safety': 'slow_down'}.get(desire.primary)
                 if allow_local_actions and not state.poi_plan_active else None)
        reason = (f'emotional desire prioritizes {desire.primary}; preserve hotspot itinerary'
                  if local else 'existing itinerary or active activity is protected')
        return GoalIntent(desire.primary, ids, local, reason, True)
    if desire.primary == 'rest':
        return GoalIntent(desire.primary, ids, 'wait', 'rest has no configured facility; wait without changing destination')
    preferred = ()
    if desire.primary == 'safety':
        preferred = tuple(target['id'] for target in targets if target.get('kind') == 'exit')
    elif desire.primary == 'group_cohesion' and state.rendezvous_id in ids:
        preferred = (state.rendezvous_id,)
    elif desire.primary == 'follow_guidance':
        preferred = tuple(target_id for target_id in ids if target_id in state.known_events
                          and state.known_events[target_id].get('command') in {'reroute', 'evacuate', 'disperse'})
    elif desire.primary == 'goal_progress' and state.current_goal in ids:
        preferred = (state.current_goal,)
    ordered = (*preferred, *(target_id for target_id in ids if target_id not in preferred))
    action = 'slow_down' if desire.primary == 'avoid_crowding' else None
    if desire.primary in {'group_cohesion', 'follow_guidance'} and not preferred:
        return GoalIntent(desire.primary, ids, None, 'no known target matching this desire; retain existing goal', True)
    return GoalIntent(desire.primary, tuple(ordered), action, f'{desire.primary} ranks known goal intentions before reachability')
