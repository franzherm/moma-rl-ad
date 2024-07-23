import gymnasium as gym
from gymnasium.experimental.wrappers import RecordVideoV0
import torch
from torch import nn
from torch import device
import numpy as np
from typing import Tuple, Sequence
from src.utils import ReplayBuffer, random_objective_weights, DataLogger, LinearScalarisation
from torch.nn.modules.loss import _Loss
from tqdm import trange
from typing import List
from pymoo.util.ref_dirs import get_reference_directions
from DQN_Network import DQN_Network
from observations import AugmentedMultiAgentObservation
import pandas as pd
from copy import deepcopy
from src.utils import calc_hypervolume

class MOMA_DQN:
    """ 
    Implements multi-objective multi-agent DQN by extending the code in MO_DQN.py
    It works by sharing the parameters between agents, adjusting the reward function to include a social and egoistisc term
    and sharing experiences using a shared replay buffer. The DQN network has access to the objective weights of other autonomous agents
    and uses a fixed weight of human agents.
    """
    
    OBSERVATION_SPACE_LIST = ["Kinematics", "OccupancyGrid"]

    def __init__(self, env: gym.Env | None, device: device = None, seed: int | None = None, 
        num_objectives: int = 2, num_actions: int = 5, 
        replay_enabled: bool = True, replay_buffer_size: int = 1000, batch_ratio: float = 0.2, 
        objective_weights: Sequence[float] = None, loss_criterion: _Loss = nn.SmoothL1Loss, 
        objective_names: List[str] = ["speed_reward", "energy_reward"], scalarisation_method = LinearScalarisation, 
        scalarisation_argument_list: List = [], reward_structure: str = "mean_reward", 
        use_double_q_learning: bool = True, observation_space_name: str = "Kinematics") -> None:
        
        if objective_names is None:
            objective_names = [f"reward_{x}" for x in range(num_objectives)]
        assert len(objective_names) == num_objectives, "The number of elements in the objective_names list must be equal to the number of objectives!"
        assert observation_space_name in MOMA_DQN.OBSERVATION_SPACE_LIST

        self.objective_names = objective_names
        self.reward_structure = reward_structure
        self.env = env
        self.num_controlled_vehicles = len(self.env.unwrapped.controlled_vehicles)
        self.use_double_q_learning = use_double_q_learning
        self.rng = np.random.default_rng(seed)
        torch.manual_seed(seed)

        self.env.unwrapped.configure({"rng": self.rng})

        self.device = device
        if self.device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.num_objectives = num_objectives
        self.objective_weights = objective_weights
        if self.objective_weights is None:
            self.objective_weights = random_objective_weights(self.num_objectives, self.rng, self.device)

        self.num_actions = num_actions

        #set proper observation space
        self.__configure_observation_space(observation_space_name, self.reward_structure)
        
        #determine observation space length
        obs, _ = self.env.reset()
        obs = torch.tensor(obs[0], device=self.device) #reshape observations and
        obs = obs[~torch.isnan(obs)].reshape(-1)       #remove nan values
        self.observation_space_length = obs.shape[0]

        (self.policy_net, self.target_net) = \
        self.__create_network(self.observation_space_length, self.num_actions, self.num_objectives)

        self.replay_enabled = replay_enabled
        self.rb_size = replay_buffer_size
        self.batch_ratio = batch_ratio

        self.loss_criterion = loss_criterion

        #initialise replay buffer
        self.buffer = ReplayBuffer(self.rb_size, self.observation_space_length, self.num_objectives, self.device, self.rng, importance_sampling=True)

        #initialise reward logger
        feature_names = ["episode"]
        feature_names.extend(self.objective_names)
        self.reward_logger = DataLogger("reward_logger",feature_names)

        #initialise scalarisation function
        self.scalarisation_method = scalarisation_method(*scalarisation_argument_list)


    def __create_network(self, num_observations, num_actions, num_objectives) -> Tuple[nn.Module, nn.Module]:
            #create one network for each objective
            policy_net = DQN_Network(num_observations, num_actions, num_objectives).to(self.device)
            target_net = DQN_Network(num_observations, num_actions, num_objectives).to(self.device)
            target_net.load_state_dict(policy_net.state_dict())

            return policy_net, target_net
    
    def __configure_observation_space(self, observation_space_name, reward_structure):
        # default observation dictionary to configure the environment with
        config_dict= {
            "observation": {
                "type": "AugmentedMultiAgentObservation",
                "observation_config": {
                    "see_behind": True,
                    "vehicles_count": 8,
                    "type": "Kinematics",
                    "features": ['presence', 'x', 'y', 'vx', 'vy', "lane_info"]
                    }
            }
        }
        obs_dict = config_dict["observation"]
        # update the dictionary based on the selected observation space type
        if observation_space_name == "OccupancyGrid":
            obs_dict["observation_config"]["type"] = "OccupancyGrid"
            obs_dict["observation_config"]["features"] = ['presence', 'vx', 'vy', 'on_road']

        # update the dictionary based on the selected reward structure
        if reward_structure == "mean_reward":
            obs_dict["observation_config"]["features"].extend(["obj_weights", "is_controlled"])
        
        #configure the environment using the constructed observation dictionary
        self.env.unwrapped.configure(config_dict)


    def train(self, num_episodes: int = 5_000, inv_optimisation_frequency: int = 1, inv_target_update_frequency: int = 5, 
                gamma: float = 0.9, epsilon_start: float = 0.9, epsilon_end: float = 0, epsilon_end_time: float = 1, num_evaluations: int = 0, eval_seed: int = 11) :
        '''
        Runs the training procedure for num_iterations iterations. The inv_optimisation_frequency specifies 
        the number of iterations after which a weight update occurs.The inv_target_update_frequency specifies 
        the number of weight updates of the policy net, after which the target net weights are adjusted.
        Gamma is the discount factor for the rewards. Epsilon is the probability of a random action being selected during training.
        Its value is linearly reduced during the training procedure from epsilon_start to epsilon_end.
        '''

        #compute evaluation interval
        if num_evaluations != 0:
            eval_interval = round(num_episodes/num_evaluations)
        
        #initialise loss logger
        feature_names = ["episode", "loss"]
        self.loss_logger = DataLogger("loss_logger",feature_names)

        #initialise hv_logger
        feature_names = ["episode", "hypervolume"]
        hv_logger = DataLogger("hv_logger", feature_names)

        self.gamma = gamma
        self.epsilon = epsilon_start
        self.optimiser = torch.optim.AdamW(self.policy_net.parameters(), lr=1e-4, amsgrad=True)

        self.loss_func = self.loss_criterion()
        episode_nr = 0
        num_of_conducted_optimisation_steps = 0
        max_eps_iteration = round(num_episodes * epsilon_end_time)

        #training loop
        for episode_nr in trange(num_episodes, desc="Training episodes", mininterval=2, position=3):
            #reset environment
            self.terminated = False
            self.truncated = False
            self.obs, _ = self.env.reset()
            self.obs = [torch.tensor(single_obs, device=self.device) for single_obs in self.obs] #reshape observations and
            self.obs = [single_obs[~torch.isnan(single_obs)].reshape(1,-1) for single_obs in self.obs] #remove nan values
            accumulated_rewards = np.zeros(self.num_objectives)

            # currently every controlled vehicle has the same objective weights
            self.objective_weights = random_objective_weights(self.num_objectives, self.rng, self.device)
            for v in self.env.unwrapped.controlled_vehicles:
                v.objective_weights = self.objective_weights     

            while not (self.terminated or self.truncated):
                self.actions = self.act(self.obs, eps_greedy=True)
                (
                    self.next_obs,
                    self.rewards,
                    self.terminated,
                    self.truncated,
                    info,
                ) = self.env.step(self.actions)
                
                self.crashed = info["crashed"]
                vehicle_obj_weights = info["vehicle_objective_weights"]
                
                self.next_obs = [torch.tensor(single_obs, device=self.device) for single_obs in self.next_obs] #reshape observations and
                self.next_obs = [single_obs[~torch.isnan(single_obs)].reshape(1,-1) for single_obs in self.next_obs] #remove nan values
                
                reward_summary = self.compute_reward_summary(self.rewards, vehicle_obj_weights)

                self.buffer.push(self.obs, self.actions, self.next_obs, reward_summary, self.crashed, episode_nr, num_samples=self.num_controlled_vehicles)
                
                #use next_obs as obs during the next iteration
                self.obs = self.next_obs
            #update the weights every optimisation_frequency steps and only once the replay buffer is filled
            if ((episode_nr % inv_optimisation_frequency) == 0) and (self.buffer.num_elements == self.rb_size):

                self.__update_weights(episode_nr, num_of_conducted_optimisation_steps, inv_target_update_frequency)
                num_of_conducted_optimisation_steps += 1
                
            #run evaluation
            if (num_evaluations != 0) and (episode_nr % eval_interval == 0):
                _,_, hv = self.evaluate(num_repetitions= 5, num_points= 10, hv_reference_point=np.array([0,0]),
                                        seed = eval_seed)
                hv_logger.add(episode=episode_nr, hypervolume=hv)

            #update logger, reduce epsilon
            self.reduce_epsilon(max_eps_iteration, epsilon_start, epsilon_end) #linearly reduce the value of epsilon
            self.reward_logger.add(episode_nr, *list(accumulated_rewards))

        #prepare logger data
        df = self.loss_logger.to_dataframe()
        leading_nans = pd.DataFrame(data = np.full(shape=(df["episode"].min(), len(df.columns)), fill_value=np.nan),columns=df.columns)
        df = pd.concat([leading_nans, df], ignore_index=True)

        #add hypervolume information if applicable
        if num_evaluations != 0:
            hv_df = hv_logger.to_dataframe()
            df["hypervolume"] = np.nan
            df.loc[df.index.isin(hv_df["episode"]),"hypervolume"] = hv_df["hypervolume"].to_numpy()

        return df
    
    def compute_reward_summary(self, rewards, obj_weights):
        reward_summary = []
        self.num_close_vehicles = rewards[0].shape[0]

        #iterate over each controlled vehicle
        for i in range(self.num_controlled_vehicles):
            r = torch.from_numpy(rewards[i]).to(self.device) #fetch associated rewards

            #in case of a crash, use crash penalty
            if self.crashed[i]:
                reward_summary.append(r[0]) #ego penalty for crash
                continue

            weights = torch.stack(obj_weights[i], dim=0).to(self.device) #fetch associated weights
            weights[torch.all(weights == 0, dim=1)] = 1/self.num_objectives #where vehicles are not controlled, assume equal weights
            
            #when some of the closest vehicles are too far away from ego, they are not included in the weights
            #thus we need to bring the rewards tensor to the same shape as the weights tensor
            if r.shape[0] > weights.shape[0]:
                assert torch.isnan(r[weights.shape[0]:]).all() #make sure all rewards beyond this point are nan
                r = r[:weights.shape[0]] #remove all excess rows from r
            
            weighted_reward = r * weights

            #summarise the list of rewards to a single reward value using the specified reward structure
            summary = None
            match self.reward_structure:
                case "mean_reward":
                    summary = torch.mean(weighted_reward, dim=0) #mean of ego and social rewards
                case "ego_reward":
                    summary = weighted_reward[0,:] #only select reward of ego vehicle
                case _:
                    raise ValueError('reward_structure argument not in list of available reward structures')
            
            
            reward_summary.append(summary)
        
        return reward_summary
            

    def __update_weights(self, current_iteration, current_optimisation_iteration, inv_target_update_frequency):
        #fetch samples from replay buffer
        batch_samples = self.buffer.sample(round(self.buffer.num_elements*self.batch_ratio))
        observations = self.buffer.get_observations(batch_samples)
        next_obs = self.buffer.get_next_obs(batch_samples)
        actions = self.buffer.get_actions(batch_samples)
        term_flags = self.buffer.get_termination_flag(batch_samples)
        rewards  = self.buffer.get_rewards(batch_samples)

        #fetch Q values of the current observation and action from all the objectives Q-networks
        state_action_values = self.policy_net(observations)
        state_action_values = state_action_values.gather(2, actions)
        state_action_values = state_action_values.reshape(observations.shape[0],self.num_objectives)

        with torch.no_grad():
            #code taken from https://github.com/eleurent/rl-agents/blob/master/rl_agents/agents/deep_q_network/pytorch.py
            if self.use_double_q_learning:
                best_actions_policy_net = self.policy_net(next_obs).argmax(2).unsqueeze(2)
                target_net_estimate = self.target_net(next_obs)
                next_state_values = target_net_estimate.gather(2, best_actions_policy_net).squeeze(2)
            else:
                next_state_values = self.target_net(next_obs).max(2).values

        next_state_values[term_flags] = 0 #set to 0 in case of a crash

        exp_state_action_values = next_state_values * self.gamma + rewards

        #compute loss between estimates and actual values
        loss = self.loss_func(state_action_values, exp_state_action_values)
        #store loss in loss logger
        self.loss_logger.add(episode=current_iteration, loss=loss.item())

        #backpropagate loss
        self.optimiser.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_value_(self.policy_net.parameters(), 100)
        self.optimiser.step()

        #update the target networks
        if (current_optimisation_iteration % inv_target_update_frequency) == 0:
                self.target_net.load_state_dict(self.policy_net.state_dict())


    def act(self, obs, eps_greedy: bool = False):
        '''select a list of actions, one element for each autonomously controlled agent'''
        joint_action = []
        for single_obs in obs:
            r = self.rng.random()
            action = None

            #select best action according to policy
            if not eps_greedy or r > self.epsilon:
                with torch.no_grad():
                    self.policy_net.eval()
                    q_values = self.policy_net(single_obs)
                    q_values = q_values.reshape(self.num_objectives, self.num_actions)
                    scalarised_values = self.scalarisation_method.scalarise_actions(q_values, self.objective_weights)
                    action = torch.argmax(scalarised_values).item()

            else: # choose random action
                action = self.rng.choice(self.num_actions)

            joint_action.append(action)

        return tuple(joint_action)

    def evaluate(self, num_repetitions: int = 5, num_points: int = 20, hv_reference_point: np.ndarray = None, seed: int = None, episode_recording_interval: int = None, video_name_prefix: str = "MOMA_DQN", video_location: str = "videos", render_episodes: bool = False):
        """ Evaluates the performance of the trained network by conducting num_repetitions episodes for each objective weights tuple. 
            the parameter num_points determines how many points in the objective-weight space are being explored. These weights
            are spaced equally according to the pymoo implementation: https://pymoo.org/misc/reference_directions.html.
            The recorded rewards for a specific tuple of objective weights divided by the maximum number of iterations within the episode
            to have an upper bound of 1. Each of the num_repetitions runs is returned but it is recommended to report on the average 
            to obtain a less biased result.
            The hv_reference_point is a vector specifying the best possible vectorial reward vector."""
        
        self.eval_env = deepcopy(self.env) #TODO: test whether deepcopy works
        self.eval_env.unwrapped.configure({"rng": self.rng})

        #TODO: change render function to include additional information
        if episode_recording_interval is not None:
            self.eval_env = RecordVideoV0(self.eval_env, video_folder= video_location, name_prefix= video_name_prefix, 
                                                episode_trigger=lambda x: x % episode_recording_interval == 0, fps=30)
        
        self.rng = np.random.default_rng(seed)
        #get equally spaced objective weights
        objective_weights = get_reference_directions("energy", n_dim = self.num_objectives, n_points = num_points, seed=seed)
        objective_weights = torch.from_numpy(objective_weights).to(self.device)
        
        #instantiate data loggers
        #for summary information
        feature_names = ["repetition_number", "weight_index","weight_tuple", "num_iterations", "vehicle_id"]
        feature_names.extend([f"normalised_{x}" for x in self.objective_names])
        feature_names.extend([f"raw_{x}" for x in self.objective_names])
        eval_logger = DataLogger("evaluation_logger",feature_names)

        #for more detailed information on the individual vehicles
        #target and actual speeds are only useful for uncontrolled vehicles, while weights are only applicable to controlled vehicles
        feature_names = ["repetition_number", "weight_index", "weight_tuple", "iteration", "vehicle_id", "controlled_flag", "action", "target_speed", "curr_speed", "acc", "lane"]
        feature_names.extend([f"curr_{x}" for x in self.objective_names])
        vehicle_logger = DataLogger("vehicle_logger", feature_names)
        
        for tuple_index in trange(objective_weights.shape[0], desc="Weight tuple", mininterval=1, position=3):
            weight_tuple = objective_weights[tuple_index]
            self.objective_weights = weight_tuple

            for repetition_nr in range(num_repetitions):
                self.terminated = False
                self.truncated = False
                self.obs, _ = self.eval_env.reset()     
                # explicitly set objective weights in the environment object as well
                # so that observations are correct
                # currently every controlled vehicle has the same objective weights
                for v in self.eval_env.unwrapped.controlled_vehicles:
                    v.objective_weights = self.objective_weights           
                accumulated_reward = np.zeros(shape=(self.num_controlled_vehicles, self.num_objectives))
                curr_num_iterations = 0
                while not (self.terminated or self.truncated):
                    if render_episodes:
                        self.eval_env.render()

                    #select action based on obs. Execute action, add up reward, next iteration
                    self.obs = [torch.tensor(single_obs, device=self.device) for single_obs in self.obs] #reshape observations and
                    self.obs = [single_obs[~torch.isnan(single_obs)].reshape(1,-1) for single_obs in self.obs] #remove nan values
                    self.action = self.act(self.obs)
                    (
                    self.obs,
                    self.reward,
                    self.terminated,
                    self.truncated,
                    info,
                    ) = self.eval_env.step(self.action)
                    
                    #accumulate rewards for summary logger
                    for vehicle_id in range(self.num_controlled_vehicles):
                        #select only the ego rewards for a specific controlled vehicle
                        vehicle_rewards = self.reward[vehicle_id][0]
                        accumulated_reward[vehicle_id] += vehicle_rewards
                    

                    #populate vehicle logger
                    controlled_vehicles_count = 0
                    for vehicle_id, vehicle in enumerate(self.eval_env.unwrapped.road.vehicles):
                        action = np.nan
                        lane = vehicle.lane_index[2]
                        acc = vehicle.action["acceleration"]
                        reward = np.full(self.num_objectives, fill_value=np.nan)
                        if vehicle.is_controlled:
                            action = self.action[controlled_vehicles_count]
                            reward = self.reward[controlled_vehicles_count][0]
                            controlled_vehicles_count += 1
                        vehicle_logger.add(repetition_nr, tuple_index, weight_tuple.tolist(), curr_num_iterations, 
                                           vehicle_id, vehicle.is_controlled, action, vehicle.target_speed, vehicle.speed, 
                                           acc, lane, *reward.tolist())

                    curr_num_iterations += 1

                #episode ended
                normalised_reward = accumulated_reward / curr_num_iterations
                for vehicle_id in range(self.num_controlled_vehicles):
                    eval_logger.add(repetition_nr, tuple_index, weight_tuple.tolist(), curr_num_iterations, vehicle_id, *normalised_reward[vehicle_id].tolist(), *accumulated_reward[vehicle_id].tolist())
        
        #compute hypervolume if reference point is given
        if hv_reference_point is not None:
            df = eval_logger.to_dataframe()
            mean_df = df.groupby("weight_index")[["normalised_speed_reward", "normalised_energy_reward"]].mean()
            reward_vector = mean_df.to_numpy()
            hypervolume = calc_hypervolume(hv_reference_point, reward_vector)
            return df, vehicle_logger.to_dataframe(), hypervolume

        return eval_logger.to_dataframe(), vehicle_logger.to_dataframe()

    def reduce_epsilon(self, max_iteration, eps_start, eps_end):
        self.epsilon = max(eps_end, self.epsilon - (eps_start-eps_end)/max_iteration)

    def set_objective_weights(self, weights: torch.Tensor):
        self.objective_weights = weights.to(self.device)

    def store_network_weights(self, model_path: str, model_name: str):
        torch.save(self.policy_net.state_dict(), f"{model_path}_{model_name}")

    def store_network(self, model_path: str, model_name: str):
        torch.save(self.policy_net, f"{model_path}{model_name}")

    def load_network_weights(self, model_path: str):
        self.policy_net.load_state_dict(torch.load(model_path))
        self.target_net.load_state_dict(torch.load(model_path))