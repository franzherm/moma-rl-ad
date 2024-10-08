import torch
import numpy as np
from collections import namedtuple
from pymoo.indicators.hv import HV
import pandas as pd
from typing import List, TypeVar
import gymnasium as gym
from gymnasium.wrappers.normalize import RunningMeanStd

ObsType = TypeVar("ObsType")
ActType = TypeVar("ActType")

class ChebyshevScalarisation:
    """ This class computes the chebyshev scalarisation for a vectorial Q-value and corresponding utopian point z*
        as described in Scalarized Multi-Objective Reinforcement Learning: Novel Design Techniques
        https://www.researchgate.net/publication/235698665_Scalarized_Multi-Objective_Reinforcement_Learning_Novel_Design_Techniques
        
        It acts as a non-linear alternative to linear scaling to choose actions based on vectorial Q-value estimates.
        It is implemented as a class due to the dynamic nature of the utopian point."""
    
    def __init__(self, initial_utopian: torch.Tensor, threshold_value: float, device = torch.device("cuda" if torch.cuda.is_available() else "cpu")) -> None:
        self.device = device
        self.z_star = initial_utopian.to(device) #initialise utopian point z*. It is a vector with the same dimensions as the vectorial Q-values
        self.threshold = threshold_value

    def scalarise_actions(self, action_q_estimates: torch.Tensor, objective_weights: torch.Tensor) -> torch.Tensor:
        action_q_estimates = torch.swapaxes(action_q_estimates,0,1) #swap axes so that rows represent q estimates of one action for all objectives
        #action_q_estimates = action_q_estimates.flatten(start_dim=0, end_dim=1)
        self.update_utopian(action_q_estimates)
        z_final = (self.z_star + self.threshold)#.reshape(-1,1)
        diffs = action_q_estimates - z_final
        abs_diffs = torch.abs(diffs)
        weighted_diffs = objective_weights * abs_diffs#.reshape(-1,1) * abs_diffs
        sq_values = torch.max(weighted_diffs, dim=1)[0]
        return sq_values

    def update_utopian(self, update_vector: torch.Tensor) -> None:
        comparison_tensor = torch.vstack([update_vector, self.z_star])
        self.z_star = torch.max(comparison_tensor, dim=0)[0]

class LinearScalarisation:

    def scalarise_actions(self, action_q_estimates, objective_weights):
        utility_values = action_q_estimates * objective_weights.reshape(-1,1)
        utility_values = torch.sum(utility_values, dim=0)
        
        return utility_values


class ReplayBuffer:
        
    def __init__(self, buffer_size, observation_space_shape, num_objectives, device, rng: np.random.Generator, importance_sampling: bool = False, prioritise_crashes: bool = False):
        self.size = buffer_size
        self.num_objectives = num_objectives
        self.observation_space_size = observation_space_shape
        self.device = device
        self.rng = rng
        self.importance_sampling = importance_sampling
        self.prioritise_crashes = prioritise_crashes

        #initialise replay buffer
        self.buffer = torch.zeros(size=(self.size, self.observation_space_size*2+self.num_objectives+3),
                                        device=self.device) # +3 for selected action, termination flag and importance sampling id
        
        self.running_index = 0 #keeps track of next index of the replay buffer to be filled
        self.num_elements = 0 #keeps track of the current number of elements in the replay buffer

    def push(self, obs, action, next_obs, reward, terminated, importance_sampling_id = None, num_samples: int = 1):
        assert num_samples >= 1
        assert (not self.importance_sampling) or importance_sampling_id != None, "If importance sampling is activated, you need to provide a corresponding identifier"
        if not self.importance_sampling:
            importance_sampling_id = torch.tensor([0], device=self.device)

        #for single agent environments
        if num_samples == 1:
            elem = torch.concatenate([obs.flatten(), action, next_obs.flatten(), reward, terminated, importance_sampling_id])
            self.buffer[self.running_index] = elem
            self.__increment_indices()

        else:#for multi-agent environments. All samples must have the same importance_sampling_id
            for i in range(num_samples):
                elem = torch.concatenate([obs[i].flatten(), torch.tensor([action[i]], device=self.device), next_obs[i].flatten(), reward[i], 
                                          torch.tensor([terminated[i]], device=self.device), 
                                          torch.tensor([importance_sampling_id], device=self.device)])
                
                self.buffer[self.running_index] = elem
                self.__increment_indices()

    def __increment_indices(self):
        #update auxiliary variables
        self.running_index = (self.running_index + 1) % self.size
        if self.num_elements < self.size:
            self.num_elements += 1

    def sample(self, sample_size):
        sample_probs = (torch.ones(self.num_elements)/self.num_elements).to(self.device)
        if self.importance_sampling:
            sample_probs = self.compute_importance_sampling_probs()

        if self.prioritise_crashes:
            crashed_flag = self.buffer[:self.num_elements,-2].to(dtype=torch.bool)
            #inv_crash_ratio = self.num_elements/torch.sum(crashed_flag)
            sample_probs[crashed_flag] = sample_probs[crashed_flag] * 2

        #normalise so that the sum of probs is 1
        sample_probs = sample_probs / torch.cumsum(sample_probs, dim=0)[-1]
        sample_probs = sample_probs.cpu().numpy() # move to cpu so that it can be used by numpy

        sample_indices = self.rng.choice(self.num_elements, p = sample_probs, size=max(1,round(sample_size)), replace=True, shuffle=True)
        return self.buffer[sample_indices]
    
    def compute_importance_sampling_probs(self):
        imp_sampling_ids = self.buffer[:self.num_elements,-1]
        min_id = torch.min(imp_sampling_ids)

        #the more recent the sample, the higher the probability of being selected
        probs = (imp_sampling_ids - min_id + 1)
        
        return probs

    #only to be used when the samples originating from this buffer
    def get_observations(self, samples):
        return samples[:,:self.observation_space_size]

    def get_actions(self, samples: torch.Tensor):
        elem = samples[:,self.observation_space_size].to(torch.int64)#.reshape(-1,1,1) #second element was self.num_objectives
        arr = elem.repeat_interleave(repeats=self.num_objectives)
        arr = arr.reshape(-1,self.num_objectives,1)
        return arr
    def get_next_obs(self, samples):
        return samples[:,self.observation_space_size+1:self.observation_space_size*2+1]

    def get_rewards(self, samples):
        return samples[:,self.observation_space_size*2+1:-2]
    
    def get_termination_flag(self, samples):
        return samples[:,-2].flatten().to(torch.bool)
    
    def get_importance_sampling_id(self, samples):
        return samples[:,-1].flatten()


class DataLogger:
    def __init__(self, loggerName: str, fieldNames: List[str]):
        self.tupleType = namedtuple(loggerName, fieldNames)
        self.tuple_list = []

    def _add_by_list(self, entry_list: List):
        self.tuple_list.append(self.tupleType(*entry_list))
        
    def _add_by_params(self, *args, **kwargs):
        self.tuple_list.append(self.tupleType(*args, **kwargs))

    def add(self, *args, **kwargs):
        if isinstance(args, tuple) and len(args) == 1 and len(kwargs.values()) == 0:
            self._add_by_list(args[0])

        elif isinstance(args, tuple) and len(args) == 0 and len(kwargs.values()) == 1:
            self._add_by_list(list(kwargs.values())[0])

        else:
            self._add_by_params(*args, **kwargs)

    def to_dataframe(self):
        return pd.DataFrame(self.tuple_list)

def random_objective_weights(num_objectives: int, rng: np.random.Generator, device):
    random_weights = rng.random(num_objectives)
    random_weights = torch.tensor(random_weights / np.sum(random_weights), device=device) #normalise the random weights
    return random_weights


def calc_hypervolume(reference_point: np.ndarray = np.array([0,0]), reward_vector: np.ndarray = None):
    '''reference point represents the worst possible value'''
    assert reward_vector is not None, "You have to provide a reward vector!"
    reward_vector = reward_vector * (-1) # convert to minimisation problem
    ind = HV(ref_point=reference_point)
    return ind(reward_vector)