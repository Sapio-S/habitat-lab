#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import time
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type
import json

import gym
import numpy as np
from gym.spaces.dict_space import Dict as SpaceDict
import random

from habitat.config import Config
from habitat.core.dataset import Dataset, Episode
from habitat.core.embodied_task import EmbodiedTask, Metrics
from habitat.core.simulator import Observations, Simulator
from habitat.datasets import make_dataset
from habitat.sims import make_sim
from habitat.tasks import make_task
from habitat.utils.geometry_utils import quaternion_to_list
from habitat_sim import utils
from onpolicy.envs.habitat.utils import pose as pu
from icecream import ic


class Env:
    r"""Fundamental environment class for ``habitat``. All the information 
    needed for working on embodied tasks with simulator is abstracted inside
    Env. Acts as a base for other derived environment classes. Env consists
    of three major components: ``dataset`` (``episodes``), ``simulator`` and 
    ``task`` and connects all the three components together.

    Args:
        config: config for the environment. Should contain id for simulator and
            ``task_name`` which are passed into ``make_sim`` and ``make_task``.
        dataset: reference to dataset for task instance level information.
            Can be defined as ``None`` in which case ``_episodes`` should be 
            populated from outside.

    Attributes:
        observation_space: ``SpaceDict`` object corresponding to sensor in sim
            and task.
        action_space: ``gym.space`` object corresponding to valid actions.
    """

    observation_space: SpaceDict
    action_space: SpaceDict
    _config: Config
    _dataset: Optional[Dataset]
    _episodes: List[Type[Episode]]
    _current_episode_index: Optional[int]
    _current_episode: Optional[Type[Episode]]
    _episode_iterator: Optional[Iterator]
    _sim: Simulator
    _task: EmbodiedTask
    _max_episode_seconds: int
    _max_episode_steps: int
    _elapsed_steps: int
    _episode_start_time: Optional[float]
    _episode_over: bool

    def __init__(
        self, config: Config, dataset: Optional[Dataset] = None
    ) -> None:
        assert config.is_frozen(), (
            "Freeze the config before creating the "
            "environment, use config.freeze()."
        )
        self._config = config
        self.num_agents = config.SIMULATOR.NUM_AGENTS

        self._dataset = dataset
        self._current_episode_index = None
        if self._dataset is None and config.DATASET.TYPE:
            self._dataset = make_dataset(
                id_dataset=config.DATASET.TYPE, config=config.DATASET
            )
        self._episodes = self._dataset.episodes if self._dataset else []

        self._current_episode = None
        iter_option_dict = {
            k.lower(): v
            for k, v in config.ENVIRONMENT.ITERATOR_OPTIONS.items()
        }
        self._episode_iterator = self._dataset.get_episode_iterator(
            **iter_option_dict
        )

        # load the first scene if dataset is present
        if self._dataset:
            assert (
                len(self._dataset.episodes) > 0
            ), "dataset should have non-empty episodes list"
            self._config.defrost()
            self._config.SIMULATOR.SCENE = self._dataset.episodes[0].scene_id
            self._config.freeze()

        self._sim = make_sim(
            id_sim=self._config.SIMULATOR.TYPE, config=self._config.SIMULATOR
        )

        self._task = make_task(
            self._config.TASK.TYPE,
            task_config=self._config.TASK,
            sim=self._sim,
            dataset=self._dataset,
        )
        self.observation_space = SpaceDict(
            {
                **self._sim.sensor_suite.observation_spaces.spaces,
                **self._task.sensor_suite.observation_spaces.spaces,
            }
        )
        self.action_space = self._sim.action_space
        self._max_episode_seconds = (
            self._config.ENVIRONMENT.MAX_EPISODE_SECONDS
        )
        self._max_episode_steps = self._config.ENVIRONMENT.MAX_EPISODE_STEPS * self._config.SIMULATOR.NUM_AGENTS
        self._elapsed_steps = 0
        self._episode_start_time: Optional[float] = None
        self._episode_over = False

        # for fixed start pos
        if self._config.SIMULATOR.USE_FIXED_START_POS:
            self.fixed_start_position = []
            self.fixed_start_rotation = []
            if "replica" in self._config.SIMULATOR.SCENE:
                scene_id = self._config.SIMULATOR.SCENE.split("/")[-3]
            else:
                scene_id = self._config.SIMULATOR.SCENE.split("/")[-1].split(".")[0]
            filepath = self._config.SIMULATOR.FIXED_MODEL_PATH + scene_id + "/{}agents/start_position.json".format(self.num_agents)
            with open(filepath,'r',encoding='utf-8') as json_file:
                self.fixed_start_position = json.load(json_file)

            filepath = self._config.SIMULATOR.FIXED_MODEL_PATH + scene_id +"/{}agents/start_rotation.json".format(self.num_agents)
            with open(filepath,'r',encoding='utf-8') as json_file:
                self.fixed_start_rotation = json.load(json_file)
            
            self.load_num = 0

    @property
    def current_episode(self) -> Type[Episode]:
        assert self._current_episode is not None
        return self._current_episode

    @current_episode.setter
    def current_episode(self, episode: Type[Episode]) -> None:
        self._current_episode = episode

    @property
    def episode_iterator(self) -> Iterator:
        return self._episode_iterator

    @episode_iterator.setter
    def episode_iterator(self, new_iter: Iterator) -> None:
        self._episode_iterator = new_iter

    @property
    def episodes(self) -> List[Type[Episode]]:
        return self._episodes

    @episodes.setter
    def episodes(self, episodes: List[Type[Episode]]) -> None:
        assert (
            len(episodes) > 0
        ), "Environment doesn't accept empty episodes list."
        self._episodes = episodes

    @property
    def sim(self) -> Simulator:
        return self._sim

    @property
    def episode_start_time(self) -> Optional[float]:
        return self._episode_start_time

    @property
    def episode_over(self) -> bool:
        return self._episode_over

    @property
    def task(self) -> EmbodiedTask:
        return self._task

    @property
    def _elapsed_seconds(self) -> float:
        assert (
            self._episode_start_time
        ), "Elapsed seconds requested before episode was started."
        return time.time() - self._episode_start_time

    def get_metrics(self) -> Metrics:
        return self._task.measurements.get_metrics()

    def _past_limit(self) -> bool:
        if (
            self._max_episode_steps != 0
            and self._max_episode_steps <= self._elapsed_steps
        ):
            return True
        elif (
            self._max_episode_seconds != 0
            and self._max_episode_seconds <= self._elapsed_seconds
        ):
            return True
        return False

    def _reset_stats(self) -> None:
        self._episode_start_time = time.time()
        self._elapsed_steps = 0
        self._episode_over = False

    def reset(self) -> Observations:
        r"""Resets the environments and returns the initial observations.

        Returns:
            initial observations from the environment.
        """
        self._reset_stats()

        assert len(self.episodes) > 0, "Episodes list is empty"

        self.current_episode = random.choice(self.episodes) if self._config.DATASET.USE_SAME_SCENE else next(self._episode_iterator)

        self.reconfigure(self._config)
        
        observations = self._sim.reset()
        
        for agent_id in range(len(observations)):
            observations[agent_id].update(
                self.task.sensor_suite.get_observations(
                    observations=observations[agent_id], episode=self.current_episode
                )
            )

        self._task.measurements.reset_measures(episode=self.current_episode)

        return observations

    def _update_step_stats(self) -> None:
        self._elapsed_steps += 1
        self._episode_over = not self._sim.is_episode_active
        if self._past_limit():
            self._episode_over = True

    def step(self, action: int, agent_id) -> Observations:
        r"""Perform an action in the environment and return observations.

        Args:
            action: action (belonging to ``action_space``) to be performed 
                inside the environment.

        Returns:
            observations after taking action in environment.
        """

        assert (
            self._episode_start_time is not None
        ), "Cannot call step before calling reset"
        assert (
            self._episode_over is False
        ), "Episode over, call reset before calling step"

        observations = self._sim.step(action, agent_id)
        observations.update(
            self._task.sensor_suite.get_observations(
                observations=observations, episode=self.current_episode
            )
        )

        self._task.measurements.update_measures(
            episode=self.current_episode, action=action
        )

        self._update_step_stats()

        return observations

    def seed(self, seed: int) -> None:
        random.seed(seed)
        self._sim.seed(seed)

    def generate_state(self):
        generate_success = False
        while not generate_success:
            state = random.sample(self.episodes, self.num_agents)
            start_position = []
            start_rotation = []
            start_y = []
            for agent_id in range(self.num_agents):
                start_position.append(state[agent_id].start_position)
                start_y.append(state[agent_id].start_position[1])
                start_rotation.append(state[agent_id].start_rotation)
            
            if len(np.unique(start_y)) == 1:
                generate_success = True
                if not self._config.SIMULATOR.USE_FULL_RAND_STATE:
                    for i in range(self.num_agents):
                        x1 = -start_position[i][2]
                        y1 = -start_position[i][0]
                        for j in range(self.num_agents-i-1):
                            x2 = -start_position[i+j+1][2]
                            y2 = -start_position[i+j+1][0]
                            if pu.get_l2_distance(x1, x2, y1, y2)<2:
                                pass
                            else:
                                generate_success = False
                                break
                        if generate_success == False:
                            break

        return start_position, start_rotation

    def reconfigure(self, config: Config) -> None:
        self._config = config

        self._config.defrost()

        if self._config.SIMULATOR.USE_FIXED_START_POS:
            start_position = self.fixed_start_position[self.load_num]
            start_rotation = self.fixed_start_rotation[self.load_num]
            self.load_num += 1
        else:
            start_position, start_rotation = self.generate_state()

        self._config.SIMULATOR = self._task.overwrite_sim_config(
            self._config.SIMULATOR, self.current_episode, start_position, start_rotation
        )
        self._config.freeze()

        self._sim.reconfigure(self._config.SIMULATOR)

    def render(self, mode="rgb") -> np.ndarray:
        return self._sim.render(mode)

    def close(self) -> None:
        self._sim.close()


class RLEnv(gym.Env):
    r"""Reinforcement Learning (RL) environment class which subclasses gym.Env.
    This is a wrapper over habitat.Env for RL users. To create custom RL
    environments users should subclass RLEnv and define the following methods:
    ``get_reward_range``, ``get_reward``, ``get_done``, ``get_info``.

    As this is a subclass of ``gym.Env``, it implements ``reset`` and ``step``.

    Args:
        config: config to construct ``habitat.Env``.
        dataset: dataset to construct ``habtiat.Env``.
    """

    _env: Env

    def __init__(
        self, config: Config, dataset: Optional[Dataset] = None
    ) -> None:
        self._env = Env(config, dataset)
        self.observation_space = self._env.observation_space
        self.action_space = self._env.action_space
        self.reward_range = self.get_reward_range()

    @property
    def habitat_env(self) -> Env:
        return self._env

    @property
    def episodes(self) -> List[Type[Episode]]:
        return self._env.episodes

    @property
    def current_episode(self) -> Type[Episode]:
        return self._env.current_episode

    @episodes.setter
    def episodes(self, episodes: List[Type[Episode]]) -> None:
        self._env.episodes = episodes

    def reset(self) -> Observations:
        return self._env.reset()

    def get_reward_range(self):
        r"""Get min, max range of reward.

        Returns:
             [min, max] range of reward.
        """
        raise NotImplementedError

    def get_reward(self, observations: Observations) -> Any:
        r"""Returns reward after action has been performed. This method
        is called inside the step method.

        Args:
            observations: observations from simulator and task.

        Returns:
            reward after performing the last action.
        """
        raise NotImplementedError

    def get_done(self, observations: Observations) -> bool:
        r"""Returns boolean indicating whether episode is done after performing
        the last action. This method is called inside the step method.

        Args:
            observations: observations from simulator and task.

        Returns:
            done boolean after performing the last action.
        """
        raise NotImplementedError

    def get_info(self, observations) -> Dict[Any, Any]:
        r"""
        Args:
            observations: observations from simulator and task.

        Returns:
            info after performing the last action.
        """
        raise NotImplementedError

    def step(self, action: int, i) -> Tuple[Observations, Any, bool, dict]:
        r"""Perform an action in the environment and return
        ``(observations, reward, done, info)``.

        Args:
            action: action (belonging to ``action_space``) to be performed 
                inside the environment.

        Returns:
            ``(observations, reward, done, info)``.
        """

        observations = self._env.step(action, i)
        reward = self.get_reward(observations,i)
        done = self.get_done(observations, i)
        info = self.get_info(observations, i)

        return observations, reward, done, info

    def seed(self, seed: Optional[int] = None) -> None:
        self._env.seed(seed)

    def render(self, mode: str = "rgb") -> np.ndarray:
        return self._env.render(mode)

    def close(self) -> None:
        self._env.close()
