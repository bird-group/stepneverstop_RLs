#!/usr/bin/env python3
# encoding: utf-8

import os
import numpy as np

from copy import deepcopy
from typing import List
from collections import defaultdict
from mlagents_envs.environment import UnityEnvironment
from mlagents_envs.side_channel.engine_configuration_channel import EngineConfigurationChannel
from mlagents_envs.side_channel.environment_parameters_channel import EnvironmentParametersChannel
# from mlagents_envs.base_env import (ActionTuple,
#                                     ActionSpec)  # TODO

from rls.common.yaml_ops import load_config
from rls.utils.np_utils import get_discrete_action_list
from rls.common.specs import (ObsSpec,
                              EnvGroupArgs,
                              ModelObservations,
                              SingleModelInformation,
                              generate_obs_dataformat)
from rls.envs.unity.wrappers.core import (ObservationWrapper,
                                          ActionWrapper)


class BasicUnityEnvironment(object):

    def __init__(self,
                 worker_id=0,
                 file_name=None,
                 port=5005,
                 render=False,
                 seed=42,
                 timeout_wait=60,
                 env_copys=12,
                 env_name='3DBall',
                 real_done=True,
                 initialize_config={},
                 engine_config={
                     'width': 84,
                     'height': 84,
                     'quality_level': 5,
                     'time_scale': 20,
                     'target_frame_rate': -1,
                     'capture_frame_rate': 60
                 },
                 **kwargs):
        # TODO: optimize
        self._n_copys = env_copys
        self._real_done = real_done

        self._side_channels = self.initialize_all_side_channels(initialize_config, engine_config)
        env_kwargs = dict(seed=seed,
                          worker_id=worker_id,
                          timeout_wait=timeout_wait,
                          side_channels=list(self._side_channels.values()))    # 注册所有初始化后的通讯频道
        if file_name is not None:
            env_dict = load_config('rls/configs/unity/env_dict.yaml')
            env_kwargs.update(file_name=file_name,
                              base_port=port,
                              no_graphics=not render,
                              additional_args=[
                                  '--scene', str(env_dict.get(env_name, 'None'))
                              ])
        self.env = UnityEnvironment(**env_kwargs)
        self.env.reset()
        self.initialize_environment()

    def initialize_all_side_channels(self, initialize_config, engine_config):
        '''
        初始化所有的通讯频道
        '''
        engine_configuration_channel = EngineConfigurationChannel()
        engine_configuration_channel.set_configuration_parameters(**engine_config)
        float_properties_channel = EnvironmentParametersChannel()
        float_properties_channel.set_float_parameter('env_copys', self._n_copys)
        for k, v in initialize_config.items():
            float_properties_channel.set_float_parameter(k, v)
        return dict(engine_configuration_channel=engine_configuration_channel,
                    float_properties_channel=float_properties_channel)

    def initialize_environment(self):
        '''
        初始化环境，获取必要的信息，如状态、动作维度等等
        '''

        self.behavior_names = list(self.env.behavior_specs.keys())
        self.fixed_behavior_names = [_str.replace('?', '_') for _str in self.behavior_names]

        self.behavior_agents = defaultdict(int)
        self.behavior_ids = defaultdict(dict)
        self.vector_idxs = defaultdict(list)
        self.vector_dims = defaultdict(list)
        self.visual_idxs = defaultdict(list)
        self.visual_dims = defaultdict(list)
        self.a_dim = defaultdict(int)
        self.discrete_action_lists = {}
        self.is_continuous = {}
        self.empty_actiontuples = {}

        self.vector_info_type = {}
        self.visual_info_type = {}

        self.env.reset()
        for bn, spec in self.env.behavior_specs.items():
            ds, ts = self.env.get_steps(bn)
            self.behavior_agents[bn] = len(ds)
            self.behavior_ids[bn] = ds.agent_id_to_index

            for i, obs_spec in enumerate(spec.observation_specs):   # TODO: optimize
                if len(obs_spec.shape) == 1:
                    self.vector_idxs[bn].append(i)
                    self.vector_dims[bn].append(obs_spec.shape[0])
                elif len(obs_spec.shape) == 3:
                    self.visual_idxs[bn].append(i)
                    self.visual_dims[bn].append(list(obs_spec.shape))
                else:
                    raise ValueError("shape of observation cannot be understood.")
            self.vector_info_type[bn] = generate_obs_dataformat(n_copys=self.behavior_agents[bn],
                                                                item_nums=len(self.vector_idxs[bn]),
                                                                name='vector')
            self.visual_info_type[bn] = generate_obs_dataformat(n_copys=self.behavior_agents[bn],
                                                                item_nums=len(self.visual_idxs[bn]),
                                                                name='visual')

            action_spec = spec.action_spec
            if action_spec.is_continuous():
                self.a_dim[bn] = action_spec.continuous_size
                self.discrete_action_lists[bn] = None
                self.is_continuous[bn] = True
            elif action_spec.is_discrete():
                self.a_dim[bn] = int(np.asarray(action_spec.discrete_branches).prod())
                self.discrete_action_lists[bn] = get_discrete_action_list(action_spec.discrete_branches)
                self.is_continuous[bn] = False
            else:
                raise NotImplementedError("doesn't support continuous and discrete actions simultaneously for now.")

            self.empty_actiontuples[bn] = action_spec.empty_action(n_agents=self.behavior_agents[bn])
        self.behavior_agents_percopy = {k: v//self._n_copys for k, v in self.behavior_agents.items()}
        # 拆分同一个behavior下的多个Agent状态，提前计算好序号
        self.batch_idx_for_behaviors = {k: np.arange(v*self._n_copys).reshape(self._n_copys, -1).T for k, v in self.behavior_agents_percopy.items()}

    def reset(self, reset_config):
        for k, v in reset_config.items():
            self._side_channels['float_properties_channel'].set_float_parameter(k, v)
        self.env.reset()
        return self.get_obs(only_obs=True)

    def step(self, actions, step_config):
        '''
        params: actions, type of dict or np.ndarray, if the type of actions is
                not dict, then set those actions for the first behavior controller.
        '''
        for k, v in step_config.items():
            self._side_channels['float_properties_channel'].set_float_parameter(k, v)

        actions = deepcopy(actions)
        idx = 0
        for k, v in self.behavior_agents_percopy.items():
            if self.is_continuous[k]:
                self.empty_actiontuples[k].add_continuous(np.hstack(actions[idx:idx+v]).reshape(-1, self.a_dim[k]))
            else:
                self.empty_actiontuples[k].add_discrete(self.discrete_action_lists[k][np.stack(actions[idx:idx+v]).T.flatten()])
            idx += v
            self.env.set_actions(k, self.empty_actiontuples[k])

        self.env.step()
        return self.get_obs()

    @property
    def GroupsSpec(self):
        ret = []
        for bn in self.behavior_names:
            ret.extend([
                EnvGroupArgs(
                    obs_spec=ObsSpec(
                       vector_dims=self.vector_dims[bn],
                       visual_dims=self.visual_dims[bn]),
                    a_dim=self.a_dim[bn],
                    is_continuous=self.is_continuous[bn],
                    n_copys=self._n_copys
                ) for i in range(self.behavior_agents_percopy[bn])
            ])
        return ret

    @property
    def n_agents(self):
        '''
        返回需要控制几个智能体
        '''
        return sum(self.behavior_agents_percopy.values())

    def get_obs(self, behavior_names=None, only_obs=False):
        '''
        解析环境反馈的信息，将反馈信息分为四部分：向量、图像、奖励、done信号
        '''
        behavior_names = behavior_names or self.behavior_names

        # TODO: optimization
        whole_done = np.full(self._n_copys, False)
        whole_info_max_step = np.full(self._n_copys, False)
        whole_info_real_done = np.full(self._n_copys, False)
        all_corrected_obs = []
        all_obs = []
        all_reward = []

        for bn in behavior_names:
            n = self.behavior_agents[bn]
            ids = self.behavior_ids[bn]
            ps = []

            while True:
                ds, ts = self.env.get_steps(bn)
                if len(ts):
                    ps.append(ts)

                if len(ds) == n:
                    break
                elif len(ds) == 0:
                    self.env.step()  # some of environments done, but some of not
                else:
                    raise ValueError(f'agents number error. Expected 0 or {n}, received {len(ds)}')

            corrected_obs, reward = ds.obs, ds.reward
            obs = deepcopy(corrected_obs)  # corrected_obs应包含正确的用于决策动作的下一状态
            done = np.full(n, False)
            info_max_step = np.full(n, False)
            info_real_done = np.full(n, False)

            for ts in ps:    # TODO: 有待优化
                _ids = np.asarray([ids[i] for i in ts.agent_id], dtype=int)
                info_max_step[_ids] = ts.interrupted    # 因为达到episode最大步数而终止的
                info_real_done[_ids[~ts.interrupted]] = True  # 去掉因为max_step而done的，只记录因为失败/成功而done的
                reward[_ids] = ts.reward
                done[_ids] = True
                # zip: vector, visual, ...
                for _obs, _tobs in zip(obs, ts.obs):
                    _obs[_ids] = _tobs

            if self._real_done:
                done = np.array(info_real_done)

            for idxs in self.batch_idx_for_behaviors[bn]:
                whole_done = np.logical_or(whole_done, done[idxs])
                whole_info_max_step = np.logical_or(whole_info_max_step, info_max_step[idxs])

                all_corrected_obs.append(ModelObservations(vector=self.vector_info_type[bn](*[corrected_obs[vi][idxs] for vi in self.vector_idxs[bn]]),
                                                           visual=self.visual_info_type[bn](*[corrected_obs[vi][idxs] for vi in self.visual_idxs[bn]])))
                all_obs.append(ModelObservations(vector=self.vector_info_type[bn](*[obs[vi][idxs] for vi in self.vector_idxs[bn]]),
                                                 visual=self.visual_info_type[bn](*[obs[vi][idxs] for vi in self.visual_idxs[bn]])))
                all_reward.append(reward[idxs])
                # all_info.append(dict(max_step=info_max_step[idxs]))
        if only_obs:
            return all_corrected_obs
        else:
            rets = []
            for corrected_obs, obs, reward in zip(all_corrected_obs, all_obs, all_reward):
                rets.append(
                    SingleModelInformation(
                        corrected_obs=corrected_obs,
                        obs=obs,
                        reward=reward,
                        done=whole_done,
                        info=dict(max_step=whole_info_max_step)
                    )
                )
            return rets

    def random_action(self):
        '''
        choose random action for each group and each agent.
        continuous: [-1, 1]
        discrete: [0-max, 0-max, ...] i.e. action dim = [2, 3] => action range from [0, 0] to [1, 2].
        '''
        actions = []
        for k, v in self.behavior_agents_percopy.items():
            if self.is_continuous[k]:
                actions.extend([
                    np.random.random((self._n_copys, self.a_dim[k])) * 2 - 1  # [-1, 1]
                    for _ in range(v)
                ])
            else:
                actions.extend([
                    np.random.randint(self.a_dim[k], size=(self._n_copys,), dtype=np.int32)
                    for _ in range(v)
                ])
        return actions

    def __getattr__(self, name):
        '''
        不允许获取BasicUnityEnvironment中以'_'开头的属性
        '''
        if name.startswith('_'):
            raise AttributeError("attempted to get missing private attribute '{}'".format(name))
        return getattr(self.env, name)


class ScaleVisualWrapper(ObservationWrapper):

    def observation(self, observation: List[SingleModelInformation]):

        def func(x): return np.asarray(x * 255).astype(np.uint8)

        for bn in self.behavior_names:
            observation[bn].obs.visual.convert_(func)
            observation[bn].obs_.visual.convert_(func)
        return observation