"""Safe single-mechanism counterfactual contrast on one frozen boundary.

A contrast holds the profile, state, observation and supplied candidate set
fixed, changes exactly one selected mechanism, and reports how the projected
goal intent, desire, game option table and learning advisory scores differ.

Isolation guarantees:

- All inputs are deep-cloned before any variant computation. The formal
  runtime state, learner, SUMO connection, RNG and counters are never touched.
- Variant recomputation uses fresh, unbound model instances; no formal engine
  boundary context, learner sample/observation/update/choose call, TraCI call
  or reseed is performed.
- If the boundary is unavailable, inputs cannot be cloned safely, the
  mechanism is not whitelisted, or a learner-state variant would be required,
  the contrast is reported ``available: False`` with a reason instead of being
  forced to run.

Whitelisted mechanisms (single change each):

- ``emotion``: replace measured PAD with the neutral PAD baseline; desire and
  the game table are recomputed on that neutral affect.
- ``desire``: keep PAD; replace the affect-driven desire with the ordinary
  rule-oriented fallback scores; the game table is recomputed on that desire.
- ``game``: leave affect/desire/learning as-is; the bounded game option table
  is dropped from the variant and only the confirmed rule/owner baseline arm
  is reported (no second game draw).
- ``learning``: keep affect/desire/game; drop advisory learner scores for the
  variant. No learner state is copied, observed or updated.

Not supported (reported unavailable, never approximated): shared policy,
frozen/inherit cohort, MARL, evolutionary game, observation imitation, DQN/PPO,
or any variant that would require a learner-state clone.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import math
from typing import Any

from crowdsim.modeling_algorithm.contracts import PADState, DesireState
from crowdsim.modeling_algorithm.individual_model.emotion.pad_continuous import PADContinuousEmotionModel
from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import EmotionDrivenDesireModel
from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
from crowdsim.modeling_algorithm.individual_model.desire_goal.runtime_goal import goal_intent

VERSION = "counterfactual-v1"
MECHANISMS = ("emotion", "desire", "game", "learning")

_UNSUPPORTED = {
    "shared", "frozen", "inherit", "marl", "evolutionary", "imitation",
    "dqn", "ppo", "policy_clone",
}


def _fallback_desire(state: Any, observation: Any, profile: Any) -> DesireState:
    scores = {
        "safety": max(state.perceived_risk, observation.perceived_risk),
        "avoid_crowding": observation.perceived_crowding,
        "goal_progress": 1.0 - observation.perceived_risk,
        "group_cohesion": profile.group_cohesion,
        "follow_guidance": profile.authority_compliance,
        "rest": state.fatigue,
    }
    return DesireState(scores, max(scores, key=scores.get))


def _goal_targets(candidates: tuple[Any, ...]) -> list[dict[str, str]]:
    return [{"id": c.target_id, "kind": c.target_kind} for c in candidates]


def _local_action_allowed(runtime: Any, pid: str, state: Any, obs: Any) -> bool:
    """Mirror the production local-arm gate read-only; never widen it to a blanket allow.

    Production keeps a local alternative only when no external/hold constraint
    blocks the agent, no hotspot itinerary is active, the agent is not dwelling
    or departing, and the current motion is the walking stage.
    """
    post = getattr(runtime, "_post_action_constraints", None)
    constraints: dict[str, Any] = {}
    if callable(post):
        try:
            constraints = post(pid) or {}
        except Exception:
            constraints = {}
    if (constraints.get("control_limits") or constraints.get("activity_hold")
            or constraints.get("policy_wait") or constraints.get("flow_hold")):
        return False
    if getattr(state, "poi_plan_active", False):
        return False
    if getattr(state, "activity_state", None) in {"hotspot_dwelling", "hotspot_departing"}:
        return False
    motion = getattr(obs, "own_motion", None)
    if motion is not None and getattr(motion, "stage_type", None) != 2:
        return False
    return True


def _top_option(options: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not options:
        return None
    best = max(options, key=lambda option: (option.get("probability") or 0.0, option.get("utility") or -math.inf))
    return {"action": best.get("action"), "target_id": best.get("target_id"),
            "candidate_id": best.get("candidate_id"), "utility": best.get("utility"),
            "probability": best.get("probability")}


def _intent_view(intent: Any) -> dict[str, Any] | None:
    return intent.as_dict() if intent is not None else None


def build_counterfactual(runtime: Any, profile: Any, state: Any, mechanism: str,
                         *, baseline_trace: dict[str, Any] | None = None,
                         before: dict[str, Any] | None = None) -> dict[str, Any]:
    """Produce an isolated contrast; never mutate the formal runtime."""
    pid = profile.person_id
    obs = runtime.observations.get(pid)
    motion = runtime.current.persons.get(pid) if getattr(runtime, "current", None) else None

    # Pre-decision boundary copy: when its identity matches the current
    # boundary it is cloned instead of the possibly post-execution state/obs,
    # so the contrast reads the same decision-before boundary as the trace.
    state_eff = state
    obs_eff = obs
    candidates_eff = None
    if before is not None:
        b_state = before.get("state")
        b_obs = before.get("observation")
        b_snap = before.get("snapshot_id")
        b_time = before.get("time_seconds")
        if (b_state is not None and b_snap == runtime.snapshot_id
                and isinstance(b_time, (int, float)) and math.isfinite(b_time)
                and b_time == runtime.time_seconds
                and (b_obs is None or b_obs.person_id == pid)):
            state_eff, obs_eff = b_state, (b_obs if b_obs is not None else obs)
            candidates_eff = before.get("candidates")

    def unavailable(reason: str) -> dict[str, Any]:
        return {"version": VERSION, "available": False, "mechanism": mechanism,
                "run_id": getattr(runtime, "run_id", None), "person_id": pid,
                "snapshot_id": getattr(runtime, "snapshot_id", None),
                "time_seconds": getattr(runtime, "time_seconds", None), "reason": reason}

    if mechanism not in MECHANISMS:
        return unavailable(f"mechanism {mechanism!r} is not whitelisted for counterfactual contrast")
    if obs_eff is None:
        return unavailable("missing observation: boundary cannot be cloned safely")
    if state_eff is None or state_eff.person_id != pid or obs_eff.person_id != pid:
        return unavailable("person/state/observation binding mismatch")
    if obs_eff.snapshot_id != runtime.snapshot_id or not math.isfinite(obs_eff.time_seconds) or obs_eff.time_seconds != runtime.time_seconds:
        return unavailable("stale observation boundary: counterfactual is read against the current boundary only")

    if candidates_eff is not None:
        candidates = tuple(candidates_eff)
    else:
        candidates = tuple(runtime.decision_candidates.get(pid, ())) if getattr(runtime, "decision_candidates", None) else ()

    # A frozen baseline trace that carries boundary identity must belong to the
    # current boundary; a stale latest snapshot cannot be projected as baseline.
    # Legacy traces without identity fields fall through to the existing guards.
    if baseline_trace is not None:
        trace_snapshot = baseline_trace.get("snapshot_id")
        trace_time = baseline_trace.get("time_seconds")
        if trace_snapshot is not None or trace_time is not None:
            if (trace_snapshot != runtime.snapshot_id or not isinstance(trace_time, (int, float))
                    or not math.isfinite(trace_time) or trace_time != runtime.time_seconds):
                return unavailable("baseline trace is a stale snapshot; contrast needs the current boundary")

    # Isolation clone: compare identity and field equality before computation.
    try:
        profile_clone = deepcopy(profile)
        state_clone = deepcopy(state_eff)
        obs_clone = deepcopy(obs_eff)
        candidates_clone = deepcopy(candidates)
    except Exception as exc:  # pragma: no cover - defensive, cloning should not fail
        return unavailable(f"boundary clone failed: {type(exc).__name__}: {exc}")

    isolation = {
        "cloned_profile": profile_clone is not profile,
        "cloned_state": state_clone is not state_eff,
        "cloned_observation": obs_clone is not obs_eff,
        "cloned_candidates": all(a is not b for a, b in zip(candidates_clone, candidates)),
        "formal_state_touched": False,
        "formal_learner_touched": False,
        "sumo_touched": False,
        "rng_reseeded": False,
    }
    if not (isolation["cloned_profile"] and isolation["cloned_state"] and isolation["cloned_observation"]):
        return unavailable("boundary clone did not produce independent objects")

    engine = getattr(runtime, "decision_engine", None)
    locked = pid in getattr(runtime.population, "locked_itinerary_ids", set())

    # Baseline side, projected from the frozen boundary trace or recomputed on
    # clones with fresh instances (still no formal engine call).
    pad_model = PADContinuousEmotionModel()
    desire_model = EmotionDrivenDesireModel()
    game_model = BoundedRationalGameModel()

    baseline_pad = PADState()
    baseline_desire = _fallback_desire(state_clone, obs_clone, profile_clone)
    if baseline_trace is not None:
        affect = baseline_trace.get("affect") or {}
        if affect.get("pad"):
            baseline_pad = PADState(**affect["pad"])
        if affect.get("desire"):
            baseline_desire = DesireState(affect["desire"]["scores"], affect["desire"]["primary"])
    else:
        measured = getattr(engine, "emotional_states", {}).get(pid) if engine is not None else None
        if measured is not None:
            baseline_pad = deepcopy(measured)
        boundary = getattr(engine, "_boundary_contexts", {}).get(pid) if engine is not None else None
        if boundary and boundary[0] == (obs.snapshot_id, obs.time_seconds):
            baseline_desire = deepcopy(boundary[1][1])

    frozen = (baseline_trace or {}).get("game") or {}
    frozen_options = frozen.get("options") if frozen.get("selection_source") == "production_frozen" else None
    frozen_bias = deepcopy(frozen.get("action_bias") or {})
    baseline_scores = deepcopy((baseline_trace or {}).get("candidate_scores") or {})
    local_allowed = _local_action_allowed(runtime, pid, state_clone, obs_clone)

    try:
        baseline_intent = goal_intent(baseline_desire, state_clone, _goal_targets(candidates_clone),
                                      locked=locked, allow_local_actions=local_allowed)
        if frozen_options is not None:
            baseline_options = frozen_options
            baseline_selected = frozen.get("selected")
            baseline_rationality = frozen.get("rationality")
        else:
            # The mirror game model omits the production replay bias. If the
            # boundary carried a bias, a bias-less mirror is not the production
            # decision table; refuse rather than claim a comparable contrast.
            if frozen_bias:
                return unavailable("production replay action_bias present; mirror contrast omits bias, not comparable")
            baseline_game = game_model.evaluate_options(profile_clone, state_clone, obs_clone,
                                                        baseline_pad, baseline_desire, candidates_clone)
            baseline_options = baseline_game.get("options", [])
            baseline_selected = None
            baseline_rationality = baseline_game.get("rationality")
    except Exception as exc:
        return unavailable(f"baseline evaluation failed: {type(exc).__name__}: {exc}")

    variant_pad = baseline_pad
    variant_desire = baseline_desire
    variant_game_options = baseline_options
    variant_scores = baseline_scores
    variant_intent = baseline_intent
    variant_selected = baseline_selected

    try:
        if mechanism in ("emotion", "desire") and frozen_bias:
            return unavailable("production replay action_bias present; mirror contrast omits bias, not comparable")
        if mechanism == "emotion":
            variant_pad = PADState()
            variant_desire = desire_model.generate(variant_pad, profile_clone, state_clone, obs_clone)
            variant_game = game_model.evaluate_options(profile_clone, state_clone, obs_clone,
                                                        variant_pad, variant_desire, candidates_clone)
            variant_game_options = variant_game.get("options", [])
            variant_intent = goal_intent(variant_desire, state_clone, _goal_targets(candidates_clone),
                                        locked=locked, allow_local_actions=local_allowed)
        elif mechanism == "desire":
            variant_desire = _fallback_desire(state_clone, obs_clone, profile_clone)
            variant_game = game_model.evaluate_options(profile_clone, state_clone, obs_clone,
                                                        variant_pad, variant_desire, candidates_clone)
            variant_game_options = variant_game.get("options", [])
            variant_intent = goal_intent(variant_desire, state_clone, _goal_targets(candidates_clone),
                                        locked=locked, allow_local_actions=local_allowed)
        elif mechanism == "game":
            # No second bounded draw; the variant reports only the owner baseline arm.
            variant_game_options = []
            variant_selected = None
        elif mechanism == "learning":
            variant_scores = {}
    except Exception as exc:
        return unavailable(f"variant evaluation failed: {type(exc).__name__}: {exc}")

    def side(pad: PADState, desire: DesireState, intent: Any, options: list[dict[str, Any]],
             scores: dict[str, Any], selected: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "pad": pad.as_dict(),
            "desire": desire.as_dict(),
            "goal_intent": _intent_view(intent),
            "selected": deepcopy(selected) if selected else None,
            "top_game_option": _top_option(options),
            "game_option_count": len(options),
            "candidate_scores": deepcopy(scores),
        }

    baseline_side = side(baseline_pad, baseline_desire, baseline_intent, baseline_options,
                         baseline_scores, baseline_selected)
    variant_side = side(variant_pad, variant_desire, variant_intent, variant_game_options,
                        variant_scores, variant_selected)

    diff = {
        "desire_primary_changed": baseline_side["desire"]["primary"] != variant_side["desire"]["primary"],
        "goal_intent_changed": baseline_side["goal_intent"] != variant_side["goal_intent"],
        "top_option_changed": baseline_side["top_game_option"] != variant_side["top_game_option"],
        "learning_scores_dropped": mechanism == "learning" and bool(baseline_scores),
    }

    return {
        "version": VERSION,
        "available": True,
        "mechanism": mechanism,
        "run_id": runtime.run_id,
        "person_id": pid,
        "role_id": profile.crowd_role,
        "snapshot_id": runtime.snapshot_id,
        "time_seconds": runtime.time_seconds,
        "boundary": {"locked_itinerary": locked, "candidate_count": len(candidates_clone)},
        "isolation": isolation,
        "baseline": baseline_side,
        "variant": variant_side,
        "diff": diff,
        "scope_note": "single-mechanism contrast on a frozen clone; no SUMO, learner update or RNG reseed.",
    }
