import copy
import glob
import os
import time
from collections import deque

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from a2c_ppo_acktr import algo, utils
from a2c_ppo_acktr.algo import gail
from a2c_ppo_acktr.arguments import get_args
from a2c_ppo_acktr.envs import make_vec_envs
from a2c_ppo_acktr.model import Policy
from a2c_ppo_acktr.storage import RolloutStorage
from evaluation import evaluate

from utils.make_env import make_env                         
from utils.env_wrappers import SubprocVecEnv, DummyVecEnv
import pdb

def make_parallel_env(env_id, n_rollout_threads, seed, discrete_action):
    def get_env_fn(rank):
        def init_env():
            env = make_env(env_id, discrete_action=discrete_action)
            env.seed(seed + rank * 1000)
            np.random.seed(seed + rank * 1000)
            # pdb.set_trace()
            return env
        return init_env
    if n_rollout_threads == 1:
        return DummyVecEnv([get_env_fn(0)])
    else:
        # pdb.set_trace()
        return SubprocVecEnv([get_env_fn(i) for i in range(n_rollout_threads)])


def main():
    args = get_args()

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    if args.cuda and torch.cuda.is_available() and args.cuda_deterministic:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    log_dir = os.path.expanduser(args.log_dir)
    eval_log_dir = log_dir + "_eval"
    utils.cleanup_log_dir(log_dir)
    utils.cleanup_log_dir(eval_log_dir)

    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    #envs = make_vec_envs(args.env_name, args.seed, args.num_processes,
    #                    args.gamma, args.log_dir, device, False)

    envs = make_parallel_env(args.env_name, args.num_processes, args.seed, True)

    '''
    actor_critic = Policy(
        envs.observation_space[0].shape,
        envs.action_space[0],
        agent_num=args.agent_num, 
        base_kwargs={'recurrent': args.recurrent_policy})
    actor_critic.to(device)
    '''
    actor_critic = []
    for i in range(args.agent_num):
        ac = Policy(
            envs.observation_space[0].shape,
            envs.action_space[0],
            agent_num=args.agent_num, 
            agent_i = i,
            base_kwargs={'recurrent': args.recurrent_policy})
        ac.to(device)
        actor_critic.append(ac)

    
    if args.algo == 'a2c':
        agent = algo.A2C_ACKTR(
            actor_critic,
            args.value_loss_coef,
            args.entropy_coef,
            lr=args.lr,
            eps=args.eps,
            alpha=args.alpha,
            max_grad_norm=args.max_grad_norm)
    elif args.algo == 'ppo':
        '''
        agent = algo.PPO(
            actor_critic,
            args.clip_param,
            args.ppo_epoch,
            args.num_mini_batch,
            args.value_loss_coef,
            args.entropy_coef,
            lr=args.lr,
            eps=args.eps,
            max_grad_norm=args.max_grad_norm)
        '''
        agent = []
        for i in range(args.agent_num):
            agent.append(algo.PPO(
                actor_critic[i],
                i,
                args.clip_param,
                args.ppo_epoch,
                args.num_mini_batch,
                args.value_loss_coef,
                args.entropy_coef,
                lr=args.lr,
                eps=args.eps,
                max_grad_norm=args.max_grad_norm,
                model_dir = args.model_dir))
    elif args.algo == 'acktr':
        agent = algo.A2C_ACKTR(
            actor_critic, args.value_loss_coef, args.entropy_coef, acktr=True)

    if args.gail:
        assert len(envs.observation_space.shape) == 1
        discr = gail.Discriminator(
            envs.observation_space.shape[0] + envs.action_space.shape[0], 100,
            device)
        file_name = os.path.join(
            args.gail_experts_dir, "trajs_{}.pt".format(
                args.env_name.split('-')[0].lower()))
        
        expert_dataset = gail.ExpertDataset(
            file_name, num_trajectories=4, subsample_frequency=20)
        drop_last = len(expert_dataset) > args.gail_batch_size
        gail_train_loader = torch.utils.data.DataLoader(
            dataset=expert_dataset,
            batch_size=args.gail_batch_size,
            shuffle=True,
            drop_last=drop_last)
    '''   
    rollouts = RolloutStorage(args.num_steps, args.num_processes,
                              envs.observation_space[0].shape, envs.action_space[0],
                              actor_critic.recurrent_hidden_state_size)

    obs = envs.reset()
    rollouts.obs[0].copy_(torch.tensor(obs[:,0,:]))
    rollouts.to(device)
    '''

    rollouts = []
    for i in range(args.agent_num):
        rollout = RolloutStorage(args.num_steps, args.num_processes,
                              envs.observation_space[0].shape, envs.action_space[0],
                              actor_critic[i].recurrent_hidden_state_size,
                              args.agent_num, i)
        rollouts.append(rollout)

    obs = envs.reset()
    # pdb.set_trace()
    
    for i in range(args.agent_num):
        rollouts[i].share_obs[0].copy_(torch.tensor(obs.reshape(args.num_processes, -1)))
        rollouts[i].obs[0].copy_(torch.tensor(obs[:,i,:]))
        rollouts[i].to(device)
        
    episode_rewards = deque(maxlen=10)

    num_updates = int(
        args.num_env_steps) // args.num_steps // args.num_processes
    print(num_updates)
    for j in range(num_updates):
        #pdb.set_trace()
        if args.use_linear_lr_decay:
            # decrease learning rate linearly
            for i in range(args.agent_num):
                utils.update_linear_schedule(agent[i].optimizer, j, num_updates, agent[i].optimizer.lr if args.algo == "acktr" else args.lr)

        for step in range(args.num_steps):
            # Sample actions
            value_list, action_list, action_log_prob_list, recurrent_hidden_states_list = [], [], [], []
            with torch.no_grad():
                for i in range(args.agent_num):
                    #pdb.set_trace()
                    value, action, action_log_prob, recurrent_hidden_states = actor_critic[i].act(
                        rollouts[i].share_obs[step],
                        rollouts[i].obs[step], rollouts[i].recurrent_hidden_states[step],
                        rollouts[i].masks[step])
                    # import pdb; pdb.set_trace()
                    value_list.append(value)
                    action_list.append(action)
                    action_log_prob_list.append(action_log_prob)
                    recurrent_hidden_states_list.append(recurrent_hidden_states)
            # Obser reward and next obs
            action = []
            for i in range(args.num_processes):
                one_env_action = []
                for k in range(args.agent_num):
                    one_hot_action = np.zeros(envs.action_space[0].n)
                    one_hot_action[action_list[k][i]] = 1
                    one_env_action.append(one_hot_action)
                action.append(one_env_action)
            #start = time.time()
            #pdb.set_trace()            
            obs, reward, done, infos = envs.step(action)
            # print(obs[0][0])
            # pdb.set_trace()
            #end = time.time()
            #print("step time: ", end-start)
            for info in infos:
                if 'episode' in info.keys():
                    episode_rewards.append(info['episode']['r'])

            # If done then clean the history of observations.
            '''
            masks = torch.FloatTensor(
                [[0.0] if done_ else [1.0] for done_ in done[0]])
            bad_masks = torch.FloatTensor(
                [[0.0] if 'bad_transition' in info.keys() else [1.0]
                 for info in infos[0]])
            '''
            masks = torch.ones(args.num_processes, 1)
            bad_masks = torch.ones(args.num_processes, 1)
            '''
            rollouts.insert(obs, recurrent_hidden_states, action,
                            action_log_prob, value, reward, masks, bad_masks)
            '''
            #import pdb; pdb.set_trace()
            for i in range(args.agent_num):
                rollouts[i].insert(torch.tensor(obs.reshape(args.num_processes, -1)), torch.tensor(obs[:,i,:]), 
                            recurrent_hidden_states, action_list[i],
                            action_log_prob_list[i], value_list[i], torch.tensor(reward[:, i].reshape(-1,1)), masks, bad_masks)
        #import pdb; pdb.set_trace()
        with torch.no_grad():
            next_value_list = []
            for i in range(args.agent_num):
                next_value = actor_critic[i].get_value(
                    rollouts[i].share_obs[-1],
                    rollouts[i].obs[-1], rollouts[i].recurrent_hidden_states[-1],
                    rollouts[i].masks[-1]).detach()
                next_value_list.append(next_value)

        if args.gail:
            if j >= 10:
                envs.venv.eval()

            gail_epoch = args.gail_epoch
            if j < 10:
                gail_epoch = 100  # Warm up
            for _ in range(gail_epoch):
                discr.update(gail_train_loader, rollouts,
                             utils.get_vec_normalize(envs)._obfilt)

            for step in range(args.num_steps):
                rollouts.rewards[step] = discr.predict_reward(
                    rollouts.obs[step], rollouts.actions[step], args.gamma,
                    rollouts.masks[step])
        for i in range(args.agent_num):
            rollouts[i].compute_returns(next_value_list[i], args.use_gae, args.gamma,
                                    args.gae_lambda, args.use_proper_time_limits)

        #import pdb; pdb.set_trace()
        for i in range(args.agent_num):
            value_loss, action_loss, dist_entropy = agent[i].update(rollouts[i])
            if (i == 0):
                print("value loss: " + str(value_loss))
        # print(value_loss)
            # pdb.set_trace()

        #rollouts.after_update()
        obs = envs.reset()
        # pdb.set_trace()
        for i in range(args.agent_num):
            rollouts[i].share_obs[0].copy_(torch.tensor(obs.reshape(args.num_processes, -1)))
            rollouts[i].obs[0].copy_(torch.tensor(obs[:,i,:]))
            rollouts[i].to(device)

        # save for every interval-th episode or for the last epoch     
        #pdb.set_trace()   
        if (j % args.save_interval == 0
                or j == num_updates - 1) and args.save_dir != "":
            save_path = os.path.join(args.save_dir, args.algo)
            if not os.path.exists(save_path + args.model_dir):
                os.makedirs(save_path + args.model_dir)
            for i in range(args.agent_num):
                torch.save([
                    actor_critic[i],
                    getattr(utils.get_vec_normalize(envs), 'ob_rms', None)
                ], save_path + args.model_dir + '/agent_%i' % (i+1) + ".pt")
        '''
        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            total_num_steps = (j + 1) * args.num_processes * args.num_steps
            end = time.time()
            print(
                "Updates {}, num timesteps {}, FPS {} \n Last {} training episodes: mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}\n"
                .format(j, total_num_steps,
                        int(total_num_steps / (end - start)),
                        len(episode_rewards), np.mean(episode_rewards),
                        np.median(episode_rewards), np.min(episode_rewards),
                        np.max(episode_rewards), dist_entropy, value_loss,
                        action_loss))
        '''
        '''
        if (args.eval_interval is not None and len(episode_rewards) > 1
                and j % args.eval_interval == 0):
            ob_rms = utils.get_vec_normalize(envs).ob_rms
            evaluate(actor_critic, ob_rms, args.env_name, args.seed,
                     args.num_processes, eval_log_dir, device)
        '''

if __name__ == "__main__":
    main()
