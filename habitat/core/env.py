#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import random
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union, cast
import json
import random
import gym
import numba
import numpy as np
from gym import spaces

from habitat.config import Config
from habitat.core.dataset import Dataset, Episode, EpisodeIterator
from habitat.core.embodied_task import EmbodiedTask, Metrics
from habitat.core.simulator import Observations, Simulator
from habitat.datasets import make_dataset
from habitat.sims import make_sim
from habitat.tasks import make_task
from habitat.utils import profiling_wrapper
from habitat.utils.geometry_utils import quaternion_to_list
from habitat_sim import utils
from onpolicy.envs.habitat.utils import pose as pu
from icecream import ic


class Env:
    r"""Fundamental environment class for :ref:`habitat`.

    :data observation_space: ``SpaceDict`` object corresponding to sensor in
        sim and task.
    :data action_space: ``gym.space`` object corresponding to valid actions.

    All the information  needed for working on embodied tasks with simulator
    is abstracted inside :ref:`Env`. Acts as a base for other derived
    environment classes. :ref:`Env` consists of three major components:
    ``dataset`` (`episodes`), ``simulator`` (:ref:`sim`) and :ref:`task` and
    connects all the three components together.
    """

    observation_space: spaces.Dict
    action_space: spaces.Dict
    _config: Config
    _dataset: Optional[Dataset[Episode]]
    number_of_episodes: Optional[int]
    _current_episode: Optional[Episode]
    _episode_iterator: Optional[Iterator[Episode]]
    _sim: Simulator
    _task: EmbodiedTask
    _max_episode_seconds: int
    _max_episode_steps: int
    _elapsed_steps: int
    _episode_start_time: Optional[float]
    _episode_over: bool
    _episode_from_iter_on_reset: bool
    _episode_force_changed: bool

    def __init__(
        self, config: Config, dataset: Optional[Dataset[Episode]] = None
    ) -> None:
        """Constructor

        :param config: config for the environment. Should contain id for
            simulator and ``task_name`` which are passed into ``make_sim`` and
            ``make_task``.
        :param dataset: reference to dataset for task instance level
            information. Can be defined as :py:`None` in which case
            ``_episodes`` should be populated from outside.
        """

        assert config.is_frozen(), (
            "Freeze the config before creating the "
            "environment, use config.freeze()."
        )
        self._config = config
        self.num_agents = config.SIMULATOR.NUM_AGENTS
        self._dataset = dataset
        if self._dataset is None and config.DATASET.TYPE:
            self._dataset = make_dataset(
                id_dataset=config.DATASET.TYPE, config=config.DATASET
            )

        self._current_episode = None
        self._episode_iterator = None
        self._episode_from_iter_on_reset = True
        self._episode_force_changed = False

        # load the first scene if dataset is present
        if self._dataset:
            assert (
                len(self._dataset.episodes) > 0
            ), "dataset should have non-empty episodes list"
            self._setup_episode_iterator()
            self.current_episode = next(self.episode_iterator)
            self._config.defrost()
            self._config.SIMULATOR.SCENE_DATASET = (
                self.current_episode.scene_dataset_config
            )
            self._config.SIMULATOR.SCENE = self.current_episode.scene_id
            self._config.SIMULATOR.ADDITIONAL_OBJECT_PATHS = (
                self.current_episode.additional_obj_config_paths
            )
            self._config.freeze()

            self.number_of_episodes = len(self.episodes)
        else:
            self.number_of_episodes = None

        self._sim = make_sim(
            id_sim=self._config.SIMULATOR.TYPE, config=self._config.SIMULATOR
        )

        self._task = make_task(
            self._config.TASK.TYPE,
            config=self._config.TASK,
            sim=self._sim,
            dataset=self._dataset,
        )
        self.observation_space = spaces.Dict(
            {
                **self._sim.sensor_suite.observation_spaces.spaces,
                **self._task.sensor_suite.observation_spaces.spaces,
            }
        )
        self.action_space = self._task.action_space
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
            
    def _setup_episode_iterator(self):
        assert self._dataset is not None
        iter_option_dict = {
            k.lower(): v
            for k, v in self._config.ENVIRONMENT.ITERATOR_OPTIONS.items()
        }
        iter_option_dict["seed"] = self._config.SEED
        self._episode_iterator = self._dataset.get_episode_iterator(
            **iter_option_dict
        )

    @property
    def current_episode(self) -> Episode:
        assert self._current_episode is not None
        return self._current_episode

    @current_episode.setter
    def current_episode(self, episode: Episode) -> None:
        self._current_episode = episode
        # This allows the current episode to be set here
        # and then reset be called without the episode changing
        self._episode_from_iter_on_reset = False
        self._episode_force_changed = True

    @property
    def episode_iterator(self) -> Iterator[Episode]:
        return self._episode_iterator

    @episode_iterator.setter
    def episode_iterator(self, new_iter: Iterator[Episode]) -> None:
        self._episode_iterator = new_iter
        self._episode_force_changed = True
        self._episode_from_iter_on_reset = True

    @property
    def episodes(self) -> List[Episode]:
        return (
            self._dataset.episodes
            if self._dataset
            else cast(List[Episode], [])
        )

    @episodes.setter
    def episodes(self, episodes: List[Episode]) -> None:
        assert (
            len(episodes) > 0
        ), "Environment doesn't accept empty episodes list."
        assert (
            self._dataset is not None
        ), "Environment must have a dataset to set episodes"
        self._dataset.episodes = episodes
        self._setup_episode_iterator()
        self._current_episode = None
        self._episode_force_changed = True
        self._episode_from_iter_on_reset = True

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
        return (
            self._max_episode_steps != 0
            and self._max_episode_steps <= self._elapsed_steps
        ) or (
            self._max_episode_seconds != 0
            and self._max_episode_seconds <= self._elapsed_seconds
        )

    def _reset_stats(self) -> None:
        self._episode_start_time = time.time()
        self._elapsed_steps = 0
        self._episode_over = False

    def reset(self) -> Observations:
        r"""Resets the environments and returns the initial observations.

        :return: initial observations from the environment.
        """
        self._reset_stats()

        # Delete the shortest path cache of the current episode
        # Caching it for the next time we see this episode isn't really worth
        # it
        if self._current_episode is not None:
            self._current_episode._shortest_path_cache = None

        if (
            self._episode_iterator is not None
            and self._episode_from_iter_on_reset
        ):
            self._current_episode = next(self._episode_iterator)

        assert len(self.episodes) > 0, "Episodes list is empty"
        self._current_episode = random.choice(self.episodes) if self._config.DATASET.USE_SAME_SCENE else next(self._episode_iterator)

        # This is always set to true after a reset that way
        # on the next reset an new episode is taken (if possible)
        self._episode_from_iter_on_reset = True
        self._episode_force_changed = False

        assert self._current_episode is not None, "Reset requires an episode"
        self.reconfigure(self._config)

        observations = self.task.reset(episode=self.current_episode)

        # TODO no update of observation in the new version of Habitat

        # for agent_id in range(len(observations)):
        #     observations[agent_id].update(
        #         self.task.sensor_suite.get_observations(
        #             observations=observations[agent_id], episode=self.current_episode
        #         )
        #     )
            
        self._task.measurements.reset_measures(
            episode=self.current_episode,
            task=self.task,
            observations=observations,
        )

        return observations

    def _update_step_stats(self) -> None:
        self._elapsed_steps += 1
        self._episode_over = not self._task.is_episode_active
        if self._past_limit():
            self._episode_over = True

        if self.episode_iterator is not None and isinstance(
            self.episode_iterator, EpisodeIterator
        ):
            self.episode_iterator.step_taken()

    def step(
        self, agent_id, action: Union[int, str, Dict[str, Any]], **kwargs
    ) -> Observations:
        r"""Perform an action in the environment and return observations.

        :param action: action (belonging to :ref:`action_space`) to be
            performed inside the environment. Action is a name or index of
            allowed task's action and action arguments (belonging to action's
            :ref:`action_space`) to support parametrized and continuous
            actions.
        :return: observations after taking action in environment.
        """

        assert (
            self._episode_start_time is not None
        ), "Cannot call step before calling reset"
        assert (
            self._episode_over is False
        ), "Episode over, call reset before calling step"
        assert (
            not self._episode_force_changed
        ), "Episode was changed either by setting current_episode or changing the episodes list. Call reset before stepping the environment again."

        # Support simpler interface as well
        if isinstance(action, (str, int, np.integer)):
            action = {"action": action}

        observations = self.task.step(
            agent_id=agent_id, action=action, episode=self.current_episode
        )

        self._task.measurements.update_measures(
            episode=self.current_episode,
            action=action,
            task=self.task,
            observations=observations,
        )

        self._update_step_stats()

        return observations

    @staticmethod
    @numba.njit
    def _seed_numba(seed: int):
        random.seed(seed)
        np.random.seed(seed)

    def seed(self, seed: int) -> None:
        random.seed(seed)
        np.random.seed(seed)
        self._seed_numba(seed)
        self._sim.seed(seed)
        self._task.seed(seed)

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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class RLEnv(gym.Env):
    r"""Reinforcement Learning (RL) environment class which subclasses ``gym.Env``.

    This is a wrapper over :ref:`Env` for RL users. To create custom RL
    environments users should subclass `RLEnv` and define the following
    methods: :ref:`get_reward_range()`, :ref:`get_reward()`,
    :ref:`get_done()`, :ref:`get_info()`.

    As this is a subclass of ``gym.Env``, it implements `reset()` and
    `step()`.
    """

    _env: Env

    def __init__(
        self, config: Config, dataset: Optional[Dataset] = None
    ) -> None:
        """Constructor

        :param config: config to construct :ref:`Env`
        :param dataset: dataset to construct :ref:`Env`.
        """

        self._env = Env(config, dataset)
        self.observation_space = self._env.observation_space
        self.action_space = self._env.action_space
        self.number_of_episodes = self._env.number_of_episodes
        self.reward_range = self.get_reward_range()

    @property
    def habitat_env(self) -> Env:
        return self._env

    @property
    def episodes(self) -> List[Episode]:
        return self._env.episodes

    @episodes.setter
    def episodes(self, episodes: List[Episode]) -> None:
        self._env.episodes = episodes

    @property
    def current_episode(self) -> Episode:
        return self._env.current_episode

    @profiling_wrapper.RangeContext("RLEnv.reset")
    def reset(self) -> Observations:
        return self._env.reset()

    def get_reward_range(self):
        r"""Get min, max range of reward.

        :return: :py:`[min, max]` range of reward.
        """
        raise NotImplementedError

    def get_reward(self, observations: Observations) -> Any:
        r"""Returns reward after action has been performed.

        :param observations: observations from simulator and task.
        :return: reward after performing the last action.

        This method is called inside the :ref:`step()` method.
        """
        raise NotImplementedError

    def get_done(self, observations: Observations) -> bool:
        r"""Returns boolean indicating whether episode is done after performing
        the last action.

        :param observations: observations from simulator and task.
        :return: done boolean after performing the last action.

        This method is called inside the step method.
        """
        raise NotImplementedError

    def get_info(self, observations) -> Dict[Any, Any]:
        r"""..

        :param observations: observations from simulator and task.
        :return: info after performing the last action.
        """
        raise NotImplementedError

    @profiling_wrapper.RangeContext("RLEnv.step")
    def step(self, agent_id, *args, **kwargs) -> Tuple[Observations, Any, bool, dict]:
        r"""Perform an action in the environment.

        :return: :py:`(observations, reward, done, info)`
        """

        observations = self._env.step(agent_id, *args, **kwargs)
        reward = self.get_reward(agent_id, observations, )
        done = self.get_done(agent_id, observations)
        info = self.get_info(agent_id, observations)

        return observations, reward, done, info

    def seed(self, seed: Optional[int] = None) -> None:
        self._env.seed(seed)

    def render(self, mode: str = "rgb") -> np.ndarray:
        return self._env.render(mode)

    def close(self) -> None:
        self._env.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
