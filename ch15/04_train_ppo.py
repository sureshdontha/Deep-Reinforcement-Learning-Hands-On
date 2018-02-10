#!/usr/bin/env python3
import os
import math
import ptan
import time
import gym
import roboschool
import argparse
from tensorboardX import SummaryWriter

from lib import model, common

import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F


ENV_ID = "RoboschoolHalfCheetah-v1"
GAMMA = 0.99
REWARD_STEPS = 5
BATCH_SIZE = 32
LEARNING_RATE_ACTOR = 1e-5
LEARNING_RATE_CRITIC = 1e-4
ENTROPY_BETA = 1e-4
ENVS_COUNT = 32

PPO_EPS = 0.2
PPO_EPOCHES = 10

TEST_ITERS = 1000


def test_net(net, env, count=10, cuda=False):
    rewards = 0.0
    steps = 0
    for _ in range(count):
        obs = env.reset()
        while True:
            obs_v = ptan.agent.float32_preprocessor([obs], cuda)
            mu_v = net(obs_v)[0]
            action = mu_v.squeeze(dim=0).data.cpu().numpy()
            action = np.clip(action, -1, 1)
            obs, reward, done, _ = env.step(action)
            rewards += reward
            steps += 1
            if done:
                break
    return rewards / count, steps / count


def calc_logprob(mu_v, var_v, actions_v):
    p1 = - ((mu_v - actions_v) ** 2) / (2*var_v.clamp(min=1e-3))
    p2 = - torch.log(torch.sqrt(2 * math.pi * var_v))
    return p1 + p2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action='store_true', help='Enable CUDA')
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    save_path = os.path.join("saves", "ppo-" + args.name)
    os.makedirs(save_path, exist_ok=True)

    envs = [gym.make(ENV_ID) for _ in range(ENVS_COUNT)]
    test_env = gym.make(ENV_ID)

    net_act = model.ModelActor(envs[0].observation_space.shape[0], envs[0].action_space.shape[0])
    net_crt = model.ModelCritic(envs[0].observation_space.shape[0])
    if args.cuda:
        net_act.cuda()
        net_crt.cuda()
    print(net_act)
    print(net_crt)

    writer = SummaryWriter(comment="-ppo_" + args.name)
    agent = model.AgentA2C(net_act, cuda=args.cuda)
    exp_source = ptan.experience.ExperienceSourceFirstLast(envs, agent, GAMMA, steps_count=REWARD_STEPS)

    opt_act = optim.Adam(net_act.parameters(), lr=LEARNING_RATE_ACTOR)
    opt_crt = optim.Adam(net_crt.parameters(), lr=LEARNING_RATE_CRITIC)

    batch = []
    best_reward = None
    with ptan.common.utils.RewardTracker(writer) as tracker:
        with ptan.common.utils.TBMeanTracker(writer, batch_size=100) as tb_tracker:
            for step_idx, exp in enumerate(exp_source):
                rewards_steps = exp_source.pop_rewards_steps()
                if rewards_steps:
                    rewards, steps = zip(*rewards_steps)
                    tb_tracker.track("episode_steps", np.mean(steps), step_idx)
                    tracker.reward(np.mean(rewards), step_idx)

                if step_idx % TEST_ITERS == 0:
                    ts = time.time()
                    rewards, steps = test_net(net_act, test_env, cuda=args.cuda)
                    print("Test done is %.2f sec, reward %.3f, steps %d" % (
                        time.time() - ts, rewards, steps))
                    writer.add_scalar("test_reward", rewards, step_idx)
                    writer.add_scalar("test_steps", steps, step_idx)
                    if best_reward is None or best_reward < rewards:
                        if best_reward is not None:
                            print("Best reward updated: %.3f -> %.3f" % (best_reward, rewards))
                            name = "best_%+.3f_%d.dat" % (rewards, step_idx)
                            fname = os.path.join(save_path, name)
                            torch.save(net_act.state_dict(), fname)
                        best_reward = rewards

                batch.append(exp)
                if len(batch) < BATCH_SIZE:
                    continue

                states_v, actions_v, vals_ref_v = \
                    common.unpack_batch_a2c(batch, net_crt, last_val_gamma=GAMMA ** REWARD_STEPS, cuda=args.cuda)
                batch.clear()

                opt_crt.zero_grad()
                value_v = net_crt(states_v)
                loss_value_v = F.mse_loss(value_v, vals_ref_v)
                loss_value_v.backward()
                opt_crt.step()

                adv_v = vals_ref_v.unsqueeze(dim=-1) - value_v.detach()
                logprob_old_pi_v = None
                sum_loss_policy = 0.0

                for _ in range(PPO_EPOCHES):
                    opt_act.zero_grad()
                    mu_v, var_v = net_act(states_v)
                    logprob_pi_v = calc_logprob(mu_v, var_v, actions_v)
                    if logprob_old_pi_v is None:
                        logprob_old_pi_v = logprob_pi_v.detach()
                    surr_obj_v = adv_v * torch.exp(logprob_pi_v - logprob_old_pi_v)
                    clipped_surr_v = torch.clamp(surr_obj_v, 1.0 - PPO_EPS, 1.0 + PPO_EPS)
                    loss_policy_v = -torch.min(surr_obj_v, clipped_surr_v).mean()
                    loss_policy_v.backward()
                    sum_loss_policy += loss_policy_v.data.cpu().numpy()[0]
                    opt_act.step()
#                entropy_loss_v = ENTROPY_BETA * (-(torch.log(2*math.pi*var_v) + 1)/2).mean()

#                loss_v = loss_policy_v + entropy_loss_v
#                loss_v.backward()
#                optimizer.step()

                tb_tracker.track("advantage", adv_v, step_idx)
                tb_tracker.track("values", value_v, step_idx)
                tb_tracker.track("batch_rewards", vals_ref_v, step_idx)
#                tb_tracker.track("loss_entropy", entropy_loss_v, step_idx)
                tb_tracker.track("loss_policy", sum_loss_policy / PPO_EPOCHES, step_idx)
                tb_tracker.track("loss_value", loss_value_v, step_idx)
#                tb_tracker.track("loss_total", loss_v, step_idx)

