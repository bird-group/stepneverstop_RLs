#!/usr/bin/env python3
# encoding: utf-8

import numpy as np
import torch as t

from torch import distributions as td
from dataclasses import dataclass

from rls.algorithms.base.on_policy import On_Policy
from rls.common.specs import (ModelObservations,
                              Data,
                              BatchExperiences)
from rls.nn.models import (ActorMuLogstd,
                           ActorDct,
                           CriticValue)
from rls.nn.utils import OPLR
from rls.common.decorator import iTensor_oNumpy


@dataclass(eq=False)
class TRPO_Store_BatchExperiences_CTS(BatchExperiences):
    value: np.ndarray
    log_prob: np.ndarray
    mu: np.ndarray
    log_std: np.ndarray


@dataclass(eq=False)
class TRPO_Train_BatchExperiences_CTS(Data):
    obs: ModelObservations
    action: np.ndarray
    log_prob: np.ndarray
    discounted_reward: np.ndarray
    gae_adv: np.ndarray
    mu: np.ndarray
    log_std: np.ndarray


@dataclass(eq=False)
class TRPO_Store_BatchExperiences_DCT(BatchExperiences):
    value: np.ndarray
    log_prob: np.ndarray
    mu: np.ndarray
    log_std: np.ndarray


@dataclass(eq=False)
class TRPO_Train_BatchExperiences_DCT(Data):
    obs: ModelObservations
    action: np.ndarray
    log_prob: np.ndarray
    discounted_reward: np.ndarray
    gae_adv: np.ndarray
    logp_all: np.ndarray


'''
Stole this from OpenAI SpinningUp. https://github.com/openai/spinningup/blob/master/spinup/algos/trpo/trpo.py
'''


def flat_concat(xs):
    return t.cat([x.flatten() for x in xs], 0)


def assign_params_from_flat(x, params):
    def flat_size(p): return int(np.prod(p.shape.as_list()))  # the 'int' is important for scalars
    splits = x.split([flat_size(p) for p in params])
    new_params = [p_new.view_as(p) for p, p_new in zip(params, splits)]
    [p.data.copy_(p_new) for p, p_new in zip(params, new_params)]


class TRPO(On_Policy):
    '''
    Trust Region Policy Optimization, https://arxiv.org/abs/1502.05477
    '''

    def __init__(self,
                 envspec,

                 beta=1.0e-3,
                 lr=5.0e-4,
                 delta=0.01,
                 lambda_=0.95,
                 cg_iters=10,
                 train_v_iters=10,
                 damping_coeff=0.1,
                 backtrack_iters=10,
                 backtrack_coeff=0.8,
                 epsilon=0.2,
                 critic_lr=1e-3,
                 network_settings={
                     'actor_continuous': {
                         'hidden_units': [64, 64],
                         'condition_sigma': False,
                         'log_std_bound': [-20, 2]
                     },
                     'actor_discrete': [32, 32],
                     'critic': [32, 32]
                 },
                 **kwargs):
        super().__init__(envspec=envspec, **kwargs)
        self.beta = beta
        self.delta = delta
        self.lambda_ = lambda_
        self.epsilon = epsilon
        self.cg_iters = cg_iters
        self.damping_coeff = damping_coeff
        self.backtrack_iters = backtrack_iters
        self.backtrack_coeff = backtrack_coeff
        self.train_v_iters = train_v_iters

        if self.is_continuous:
            self.actor = ActorMuLogstd(self.rep_net.h_dim,
                                       output_shape=self.a_dim,
                                       network_settings=network_settings['actor_continuous']).to(self.device)
        else:
            self.actor = ActorDct(self.rep_net.h_dim,
                                  output_shape=self.a_dim,
                                  network_settings=network_settings['actor_discrete']).to(self.device)
        self.critic = CriticValue(self.rep_net.h_dim,
                                  network_settings=network_settings['critic']).to(self.device)

        self.critic_oplr = OPLR([self.critic, self.rep_net], critic_lr)

        if self.is_continuous:
            self.initialize_data_buffer(store_data_type=TRPO_Store_BatchExperiences_CTS,
                                        sample_data_type=TRPO_Train_BatchExperiences_CTS)
        else:
            self.initialize_data_buffer(store_data_type=TRPO_Store_BatchExperiences_DCT,
                                        sample_data_type=TRPO_Train_BatchExperiences_DCT)

        self._worker_modules.update(rep_net=self.rep_net,
                                    actor=self.actor)

        self._trainer_modules.update(self._worker_modules)
        self._trainer_modules.update(critic=self.critic,
                                     critic_oplr=self.critic_oplr)

    def __call__(self, obs, evaluation=False):
        actions, self.next_cell_state, self._value, self._log_prob, ret = self.call(obs, cell_state=self.cell_state)
        if self.is_continuous:
            self._mu, self._log_std = ret
        else:
            self._logp_all = ret

    @iTensor_oNumpy
    def call(self, obs, cell_state):
        feat, cell_state = self.rep_net(obs, cell_state=cell_state)
        value = self.critic(feat)
        if self.is_continuous:
            mu, log_std = output
            dist = td.Independent(td.Normal(mu, log_std.exp()), 1)
            sample_op = dist.sample().clamp(-1, 1)
            log_prob = dist.log_prob(sample_op).unsqueeze(-1)
            ret = (mu, log_std)
        else:
            logits = output
            logp_all = logits.log_softmax(-1)
            ret = logp_all
            norm_dist = td.Categorical(logits=logp_all)
            sample_op = norm_dist.sample()
            log_prob = norm_dist.log_prob(sample_op)
        return sample_op, cell_state, value, log_prob+t.finfo().eps, ret

    def store_data(self, exps: BatchExperiences):
        # self._running_average()

        if self.is_continuous:
            self.data.add(TRPO_Store_BatchExperiences_CTS(*exps.astuple(), self._value, self._log_prob, self._mu, self._log_std))
        else:
            self.data.add(TRPO_Store_BatchExperiences_DCT(*exps.astuple(), self._value, self._log_prob, self._logp_all))
        if self.use_rnn:
            self.data.add_cell_state(tuple(cs.numpy() for cs in self.cell_state))
        self.cell_state = self.next_cell_state

    @iTensor_oNumpy
    def _get_value(self, obs):
        feat, _ = self.rep_net(obs, cell_state=self.cell_state)
        value = self.critic(feat)
        return value

    def calculate_statistics(self):
        init_value = self._get_value(self.data.get_last_date().obs_)
        self.data.cal_dc_r(self.gamma, init_value)
        self.data.cal_td_error(self.gamma, init_value)
        self.data.cal_gae_adv(self.lambda_, self.gamma)

    def learn(self, **kwargs):
        self.train_step = kwargs.get('train_step')

        def _train(data, cell_state):
            actor_loss, entropy, gradients = self.train_actor(data, cell_state)

            x = self.cg(self.Hx, gradients.numpy(), data, cell_state)
            alpha = np.sqrt(2 * self.delta / (np.dot(x, self.Hx(x, data, cell_state)) + np.finfo(np.float32).eps))
            for i in range(self.backtrack_iters):
                assign_params_from_flat(alpha * x * (self.backtrack_coeff ** i), self.actor)

            for _ in range(self.train_v_iters):
                critic_loss = self.train_critic(data, cell_state)

            summaries = dict([
                ['LOSS/actor_loss', actor_loss],
                ['LOSS/critic_loss', critic_loss],
                ['Statistics/entropy', entropy]
            ])
            return summaries

        self._learn(function_dict={
            'calculate_statistics': self.calculate_statistics,
            'train_function': _train,
            'summary_dict': dict([
                ['LEARNING_RATE/critic_lr', self.critic_oplr.lr]
            ])
        })

    @iTensor_oNumpy
    def train_actor(self, BATCH, cell_states):
        feat, _ = self.rep_net(BATCH.obs, cell_state=cell_states['obs'])
        output = self.actor(feat)
        if self.is_continuous:
            mu, log_std = output
            dist = td.Independent(td.Normal(mu, log_std.exp()), 1)
            new_log_prob = dist.log_prob(BATCH.action).unsqueeze(-1)
            entropy = dist.entropy().mean()
        else:
            logits = output
            logp_all = logits.log_softmax(-1)
            new_log_prob = (BATCH.action * logp_all).sum(1, keepdim=True)
            entropy = -(logp_all.exp() * logp_all).sum(1, keepdim=True).mean()
        ratio = (new_log_prob - BATCH.log_prob).exp()
        actor_loss = -(ratio * BATCH.gae_adv).mean()
        actor_grads = tape.gradient(actor_loss, self.actor)    # TODO
        gradients = flat_concat(actor_grads)
        self.global_step.add_(1)
        return actor_loss, entropy, gradients

    @iTensor_oNumpy
    def Hx(self, x, BATCH, cell_states):
        feat, _ = self.rep_net(BATCH.obs, cell_state=cell_states['obs'])
        output = self.actor(feat)
        if self.is_continuous:
            mu, log_std = output
            var0, var1 = (2 * log_std).exp(), (2 * BATCH.log_std).exp()
            pre_sum = 0.5 * (((BATCH.mu - mu)**2 + var0) / (var1 + t.finfo().eps) - 1) + BATCH.log_std - log_std
            all_kls = pre_sum.sum(1)
        else:
            logits = output
            logp_all = logits.log_softmax(-1)
            all_kls = (BATCH.logp_all.exp() * (BATCH.logp_all - logp_all)).sum(1)
        kl = all_kls.mean()
        g = flat_concat(tape.gradient(kl, self.actor))
        _g = (g * x).sum()
        hvp = flat_concat(tape.gradient(_g, self.actor))
        if self.damping_coeff > 0:
            hvp += self.damping_coeff * x
        return hvp

    @iTensor_oNumpy
    def train_critic(self, BATCH, cell_states):
        feat, _ = self.rep_net(BATCH.obs, cell_state=cell_states['obs'])
        value = self.critic(feat)
        td_error = BATCH.discounted_reward - value
        value_loss = td_error.square().mean()
        self.critic_oplr.step(value_loss)
        return value_loss

    @iTensor_oNumpy
    def cg(self, Ax, b, BATCH, cell_state):
        """
        Conjugate gradient algorithm
        (see https://en.wikipedia.org/wiki/Conjugate_gradient_method)
        """
        x = np.zeros_like(b)
        r = b.copy()  # Note: should be 'b - Ax(x)', but for x=0, Ax(x)=0. Change if doing warm start.
        p = r.copy()
        r_dot_old = np.dot(r, r)
        for _ in range(self.cg_iters):
            z = Ax(p, BATCH, cell_state)
            alpha = r_dot_old / (np.dot(p, z) + np.finfo(np.float32).eps)
            x += alpha * p
            r -= alpha * z
            r_dot_new = np.dot(r, r)
            p = r + (r_dot_new / r_dot_old) * p
            r_dot_old = r_dot_new
        return x