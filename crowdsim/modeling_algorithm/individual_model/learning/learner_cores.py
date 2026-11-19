"""Independent learner cores. Not registered as live CrowdSim runtime methods."""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
import hashlib
import math
import random
import numpy as np

from .core_contracts import (CoreBinding, LegalChoice, LearningSample, LearningUpdate,
    canonical, digest, finite, discrete_state)


def _sample_public(sample):
    """Read-only immutable projection of the sample this update consumed.

    Only encoded state/feature vectors and attribution fields are carried;
    no API key, raw prompt or model credential is included.
    """
    return {
        "sample_id": sample.sample_id,
        "action": list(sample.action),
        "target_id": sample.target_id,
        "candidate_id": sample.candidate_id,
        "state": list(sample.state),
        "features": list(sample.features),
        "next_state": list(sample.next_state),
        "next_actions": [list(a) for a in sample.next_actions],
        "reward": sample.reward,
        "duration_seconds": sample.duration_seconds,
        "evidence_id": sample.evidence_id,
        "feedback_rule_id": sample.feedback_rule_id,
        "feedback_version": sample.feedback_version,
        "predicted_cost_seconds": sample.predicted_cost_seconds,
        "observed_cost_seconds": sample.observed_cost_seconds,
        "cost_complete": sample.cost_complete,
        "terminated": sample.terminated,
        "truncated": sample.truncated,
    }


class LearnerCore:
    method_id = ''

    def __init__(self, config, identity, **options):
        self.binding = CoreBinding(config, identity, **options)
        if self.binding.config['method_id'] != self.method_id:
            raise ValueError('learner method/config mismatch')
        self._parameters = deepcopy(self.binding.config['parameters'])
        for name in ('minimum_samples', 'update_every', 'update_budget', 'table_capacity'):
            if name in self._parameters:
                self._parameters[name] = int(self._parameters[name])
        self.samples = deque()
        self._seen = set()
        self.accepted = 0
        self.updates = 0
        self.frozen = False
        self.last_updates = ()
        self._update_tokens = set()

    def observe(self, sample):
        self.binding.validate(sample)
        self._validate_method_sample(sample)
        if self.frozen:
            return 'frozen'
        if sample.sample_id in self._seen:
            return 'duplicate'
        # Full-run deduplication within explicit storage capacity; no silent eviction.
        if len(self._seen) >= self.binding.capacity:
            raise BufferError('learner sample/dedup capacity reached')
        self._seen.add(sample.sample_id)
        self.samples.append(sample)
        self.accepted += 1
        return 'accepted'

    def _validate_method_sample(self, sample):
        pass

    def update(self, *, token):
        from .core_contracts import text
        text(token, 'update token')
        if self.frozen or token in self._update_tokens or self.accepted < self._parameters['minimum_samples'] or self.accepted % self._parameters['update_every']:
            self.last_updates = ()
            return ()
        batch = self._batch(token)
        if batch and len(self._update_tokens) >= self.binding.capacity:
            raise BufferError('learner update token capacity reached')
        changes=[]
        checkpoint=deepcopy(self.__dict__)
        try:
            for sample in batch:
                before = digest(self._state())
                detail=self._learn(sample)
                self.updates += 1
                changes.append(LearningUpdate(canonical(dict(method_id=self.method_id,
                    binding_fingerprint=self.binding.fingerprint, sample_id=sample.sample_id,
                    evidence_id=sample.evidence_id, update_version=self.updates,
                    update_token=token, sample_digest=digest(asdict(sample)),
                    sample=_sample_public(sample),
                    feedback=dict(rule_id=sample.feedback_rule_id, version=sample.feedback_version,
                        reward=sample.reward, duration_seconds=sample.duration_seconds,
                        terminated=sample.terminated, truncated=sample.truncated),
                    replay=bool(getattr(self,'replay',{}).get('enabled',False)),
                    before=before, after=digest(self._state()), detail=detail))))
                self._consume(sample)
        except Exception:
            self.__dict__.clear();self.__dict__.update(checkpoint)
            raise
        if changes:
            self._update_tokens.add(token)
        self.last_updates=tuple(changes)
        return self.last_updates

    def _batch(self, token):
        return tuple(self.samples)[:self._parameters['update_budget']]

    def _consume(self, sample):
        if not self.samples or self.samples[0] != sample:
            raise ValueError('learner queue mismatch')
        self.samples.popleft()

    def choose(self, state, choices, *, token):
        from .core_contracts import text
        discrete_state(state);text(token,'choice token')
        values=self.binding.choices(choices)
        scores=self.score(state,values)
        if set(scores) != {c.key for c in values}:
            raise ValueError('learner score/action mismatch')
        for value in scores.values():finite(value,'score',lo=-1e12,hi=1e12)
        return min(values,key=lambda c:(-scores[c.key],c.key)).key

    def freeze(self):
        self.frozen=True

    def _state(self):
        raise NotImplementedError

    def snapshot(self):
        return deepcopy(dict(method_id=self.method_id, binding_fingerprint=self.binding.fingerprint,
            accepted=self.accepted, updates=self.updates, frozen=self.frozen, state=self._state()))

    def export_checkpoint(self):
        # In-memory only; no automatic file/URL loading or cross-run inheritance.
        data = dict(format='learner-core-checkpoint-v1', method_id=self.method_id,
            binding_fingerprint=self.binding.fingerprint, state=self._state(),
            accepted=self.accepted, updates=self.updates, frozen=self.frozen,
            samples=[asdict(s) for s in self.samples], seen=sorted(self._seen),
            update_tokens=sorted(self._update_tokens),
            replay_tokens=sorted(getattr(self,'_replay_tokens',set())))
        import json
        data=json.loads(canonical(data))
        return {**data, 'checksum':digest(data)}

    def load_checkpoint(self, payload):
        from .core_contracts import text
        if not isinstance(payload,dict) or set(payload) != {'format','method_id','binding_fingerprint',
                'state','accepted','updates','frozen','samples','seen','update_tokens','replay_tokens','checksum'}:
            raise ValueError('invalid learner checkpoint fields')
        data=deepcopy(payload);checksum=data.pop('checksum')
        if checksum != digest(data) or data['format']!='learner-core-checkpoint-v1':
            raise ValueError('learner checkpoint checksum/format mismatch')
        if data['method_id']!=self.method_id or data['binding_fingerprint']!=self.binding.fingerprint:
            raise ValueError('learner checkpoint binding mismatch')
        cap=self.binding.capacity
        for name in ('accepted','updates'):
            if type(data[name]) is not int or data[name]<0:
                raise ValueError('invalid checkpoint counters')
        if type(data['frozen']) is not bool:
            raise ValueError('invalid checkpoint freeze flag')
        for name in ('samples','seen','update_tokens','replay_tokens','state'):
            if not isinstance(data[name],list) or len(data[name])>cap:
                raise ValueError('checkpoint list capacity/type mismatch')
        for name in ('seen','update_tokens','replay_tokens'):
            for value in data[name]:text(value,name)
            if len(set(data[name]))!=len(data[name]):raise ValueError('duplicate checkpoint keys')
        if data['accepted']!=len(data['seen']) or len(data['update_tokens'])>data['updates']:
            raise ValueError('checkpoint counter/key mismatch')
        b=self.binding
        fresh=create_learner_core(b.config,b.identity,feature_dimension=b.dimension,
            time_unit_seconds=b.time_unit,bootstrap_on_truncation=b.bootstrap_on_truncation,storage_capacity=cap)
        fresh._restore_state(data['state'])
        restored=[]
        for raw in data['samples']:
            if not isinstance(raw,dict) or set(raw)!=set(LearningSample.__dataclass_fields__):
                raise ValueError('invalid checkpoint sample fields')
            item=deepcopy(raw)
            for name in ('action','state','features','next_state'):
                if not isinstance(item[name],list):raise ValueError('invalid checkpoint sample vector')
                item[name]=tuple(item[name])
            if not isinstance(item['next_actions'],list) or any(not isinstance(x,list) for x in item['next_actions']):
                raise ValueError('invalid checkpoint next actions')
            item['next_actions']=tuple(tuple(x) for x in item['next_actions'])
            sample=LearningSample(**item);fresh.binding.validate(sample);fresh._validate_method_sample(sample)
            if sample.sample_id not in data['seen']:raise ValueError('checkpoint sample not seen')
            restored.append(sample)
        if len({s.sample_id for s in restored})!=len(restored):raise ValueError('duplicate checkpoint samples')
        if self.method_id=='tabular_q_v1' and fresh.replay['enabled']:
            if len(restored)>fresh.replay['capacity'] or not set(data['replay_tokens']).issubset(data['update_tokens']):
                raise ValueError('checkpoint replay mismatch')
        elif data['replay_tokens']:
            raise ValueError('unexpected checkpoint replay tokens')
        fresh.samples=deque(restored);fresh._seen=set(data['seen'])
        fresh._update_tokens=set(data['update_tokens']);fresh.accepted=data['accepted']
        fresh.updates=data['updates'];fresh.frozen=data['frozen']
        if hasattr(fresh,'_replay_tokens'):fresh._replay_tokens=set(data['replay_tokens'])
        self.__dict__.clear();self.__dict__.update(fresh.__dict__)

    def _restore_state(self, state):
        raise NotImplementedError

    def diagnostics(self):
        return dict(method_id=self.method_id, binding_fingerprint=self.binding.fingerprint,
            accepted_samples=self.accepted, update_version=self.updates,
            pending_or_replay_samples=len(self.samples), frozen=self.frozen,
            core_implemented=True, runtime_ready=False)


class BoundedAdaptationCore(LearnerCore):
    method_id='bounded_adaptation_v1'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.corrections={}

    def _validate_method_sample(self,sample):
        if not sample.cost_complete or sample.truncated or sample.predicted_cost_seconds is None or sample.observed_cost_seconds is None:
            raise ValueError('bounded adaptation requires explicit completed predicted/observed cost')
        if sample.action[0] not in {'reroute','change_goal'}:
            raise ValueError('bounded cost adaptation only supports goal/route actions')

    def _learn(self,sample):
        key=sample.action
        if key not in self.corrections and len(self.corrections)>=self.binding.capacity:
            raise BufferError('adaptation action capacity reached')
        old=self.corrections.get(key,0.0)
        residual=sample.observed_cost_seconds-sample.predicted_cost_seconds
        p=self._parameters
        value=(1-p['learning_rate'])*p['forgetting_factor']*old+p['learning_rate']*residual
        value=max(-p['correction_limit'],min(p['correction_limit'],value))
        finite(value,'cost correction')
        self.corrections[key]=value
        return dict(action=list(key), old=old, new=value, residual_seconds=residual)

    def score(self,state,choices):
        result={}
        for c in self.binding.choices(choices):
            if c.key[0] not in {'reroute','change_goal'}:
                raise ValueError('bounded cost adaptation only supports goal/route actions')
            if c.predicted_cost_seconds is None:
                raise ValueError('cost adaptation choice needs predicted cost')
            corrected=max(0,c.predicted_cost_seconds+self.corrections.get(c.key,0))
            result[c.key]=-corrected
        return result

    def _restore_state(self,state):
        from .core_contracts import action_key
        result={}
        for row in state:
            if not isinstance(row,dict) or set(row)!={'action','correction'} or not isinstance(row['action'],list):
                raise ValueError('invalid adaptation checkpoint state')
            key=action_key(tuple(row['action']));self.binding.validate_target(key)
            if key[0] not in {'reroute','change_goal'}:raise ValueError('adaptation checkpoint action mismatch')
            value=finite(row['correction'],'checkpoint correction',lo=-self._parameters['correction_limit'],hi=self._parameters['correction_limit'])
            if key in result:raise ValueError('duplicate adaptation checkpoint action')
            result[key]=value
        self.corrections=result

    def _state(self):
        return [dict(action=list(k),correction=v) for k,v in sorted(self.corrections.items())]


class LinUCBCore(LearnerCore):
    method_id='linucb_v1'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.models={}

    def _prior(self):
        return np.eye(self.binding.dimension)*self._parameters['regularization'],np.zeros(self.binding.dimension)

    def _learn(self,sample):
        if sample.action not in self.models and len(self.models)>=self.binding.capacity:
            raise BufferError('LinUCB arm capacity reached')
        existed=sample.action in self.models
        A,b=self.models.get(sample.action,self._prior())
        x=np.asarray(sample.features,dtype=float)
        with np.errstate(all='raise'):
            # A first arm has no solved prior theta; ``prior`` marks the initial
            # state (zeroed reward vector) so a prior zero and a genuinely solved
            # zero are not conflated. previous_b keeps the zeroed prior vector.
            previous_theta = np.linalg.solve(A,b).tolist() if existed else None
            previous_b=b.copy()
            new_A=A+np.outer(x,x);new_b=b+sample.reward*x
            theta=np.linalg.solve(new_A,new_b)
        if not np.isfinite(new_A).all() or not np.isfinite(new_b).all() or not np.isfinite(theta).all():
            raise ValueError('nonfinite LinUCB update')
        self.models[sample.action]=(new_A,new_b)
        return dict(action=list(sample.action), prior=not existed,
            previous_theta=previous_theta, theta=theta.tolist(),
            previous_b=previous_b.tolist(), new_b=new_b.tolist())

    def score(self,state,choices):
        scores={}
        for c in self.binding.choices(choices):
            A,b=self.models.get(c.key,self._prior());x=np.asarray(c.features,dtype=float)
            with np.errstate(all='raise'):
                theta=np.linalg.solve(A,b);variance=float(x @ np.linalg.solve(A,x))
                value=float(x@theta)+self._parameters['exploration_coefficient']*math.sqrt(max(0,variance))
            finite(value,'LinUCB score',lo=-1e12,hi=1e12)
            scores[c.key]=value
        return scores

    def _restore_state(self,state):
        from .core_contracts import action_key
        result={};d=self.binding.dimension
        for row in state:
            if not isinstance(row,dict) or set(row)!={'action','A','b'} or not isinstance(row['action'],list):
                raise ValueError('invalid LinUCB checkpoint state')
            key=action_key(tuple(row['action']));self.binding.validate_target(key)
            if (not isinstance(row['A'],list) or len(row['A'])!=d
                    or any(not isinstance(values,list) or len(values)!=d for values in row['A'])
                    or not isinstance(row['b'],list) or len(row['b'])!=d):
                raise ValueError('invalid LinUCB checkpoint dimensions')
            for value in row['b'] + [value for values in row['A'] for value in values]:
                finite(value,'checkpoint matrix value',lo=-1e15,hi=1e15)
            try:
                matrix=np.asarray(row['A'],dtype=float);response=np.asarray(row['b'],dtype=float)
            except (ValueError,TypeError) as exc:raise ValueError('invalid LinUCB checkpoint matrix') from exc
            if matrix.shape!=(d,d) or response.shape!=(d,) or not np.isfinite(matrix).all() or not np.isfinite(response).all():
                raise ValueError('invalid LinUCB checkpoint dimensions/finite values')
            if not np.allclose(matrix,matrix.T,rtol=0,atol=1e-10) or np.linalg.eigvalsh(matrix).min()<self._parameters['regularization']-1e-10:
                raise ValueError('invalid LinUCB checkpoint positive matrix')
            if key in result:raise ValueError('duplicate LinUCB checkpoint arm')
            result[key]=(matrix.copy(),response.copy())
        self.models=result

    def _state(self):
        return [dict(action=list(k),A=A.tolist(),b=b.tolist()) for k,(A,b) in sorted(self.models.items())]


class TabularQCore(LearnerCore):
    method_id='tabular_q_v1'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.values={}
        self.replay=deepcopy(self.binding.config.get('replay') or {'enabled':False})
        for name in ('capacity', 'batch_size'):
            if name in self.replay:
                self.replay[name] = int(self.replay[name])
        self._replay_tokens=set()

    def observe(self,sample):
        self.binding.validate(sample)
        if self.frozen:return 'frozen'
        if sample.sample_id in self._seen:return 'duplicate'
        if len(self._seen)>=self.binding.capacity:raise BufferError('Q sample/dedup capacity reached')
        self._seen.add(sample.sample_id);self.samples.append(sample);self.accepted+=1
        if self.replay['enabled']:
            while len(self.samples)>self.replay['capacity']:self.samples.popleft()
        return 'accepted'

    def _batch(self,token):
        if not self.replay['enabled']:return super()._batch(token)
        if token in self._replay_tokens:return ()
        if len(self._replay_tokens)>=self.binding.capacity:raise BufferError('replay token capacity reached')
        size=min(len(self.samples),self.replay['batch_size'],self._parameters['update_budget'])
        randomizer=random.Random(int(hashlib.sha256(token.encode()).hexdigest(),16))
        return tuple(randomizer.sample(list(self.samples),size))

    def update(self,*,token):
        changes=super().update(token=token)
        if self.replay['enabled'] and changes:self._replay_tokens.add(token)
        return changes

    def _consume(self,sample):
        if not self.replay['enabled']:super()._consume(sample)

    def _learn(self,sample):
        key=(sample.state,sample.action)
        if key not in self.values and len(self.values)>=self._parameters['table_capacity']:
            raise BufferError('Q state/action table capacity reached')
        old=self.values.get(key,0.0)
        bootstrap=not sample.terminated and (not sample.truncated or self.binding.bootstrap_on_truncation)
        next_value=max((self.values.get((sample.next_state,a),0.0) for a in sample.next_actions),default=0.0) if bootstrap else 0.0
        gamma=self._parameters['discount_factor']**(sample.duration_seconds/self.binding.time_unit)
        target=sample.reward+gamma*next_value
        value=old+self._parameters['learning_rate']*(target-old)
        finite(value,'Q value',lo=-1e12,hi=1e12)
        self.values[key]=value
        return dict(action=list(sample.action),state=list(sample.state),old=old,new=value,
                    target=target,bootstrap=bootstrap,effective_discount=gamma)

    def score(self,state,choices):
        discrete_state(state)
        return {c.key:self.values.get((state,c.key),0.0) for c in self.binding.choices(choices)}

    def choose(self,state,choices,*,token):
        from .core_contracts import text
        text(token,'choice token');discrete_state(state)
        values=tuple(sorted(self.binding.choices(choices),key=lambda c:c.key))
        randomizer=random.Random(int(hashlib.sha256(token.encode()).hexdigest(),16))
        if not self.frozen and randomizer.random()<self._parameters['epsilon']:
            return randomizer.choice(values).key
        return super().choose(state,values,token=token)

    def _restore_state(self,state):
        from .core_contracts import action_key
        if len(state)>self._parameters['table_capacity']:raise ValueError('Q checkpoint table capacity exceeded')
        result={}
        for row in state:
            if not isinstance(row,dict) or set(row)!={'state','action','value'} or not isinstance(row['state'],list) or not isinstance(row['action'],list):
                raise ValueError('invalid Q checkpoint state')
            key=(discrete_state(tuple(row['state'])),action_key(tuple(row['action'])))
            self.binding.validate_target(key[1])
            if key in result:raise ValueError('duplicate Q checkpoint key')
            result[key]=finite(row['value'],'checkpoint Q value',lo=-1e12,hi=1e12)
        self.values=result

    def _state(self):
        return [dict(state=list(s),action=list(a),value=v) for (s,a),v in sorted(self.values.items(),key=lambda row:canonical(row[0]))]


FACTORIES={c.method_id:c for c in (BoundedAdaptationCore,LinUCBCore,TabularQCore)}


def create_learner_core(config,identity,**options):
    if not isinstance(config,dict) or not isinstance(config.get('method_id'),str) or config['method_id'] not in FACTORIES:
        raise ValueError('unknown learner core method')
    return FACTORIES[config['method_id']](config,identity,**options)
