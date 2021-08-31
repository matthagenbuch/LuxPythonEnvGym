from luxai2021.game.game_constants import GAME_CONSTANTS
from luxai2021.game.position import Position
import gym
from gym import spaces
import numpy as np
from collections import OrderedDict

import time
import json
import datetime as dt
import os

from stable_baselines3 import PPO # pip install stable-baselines3
from luxai2021.env.lux_env import LuxEnvironment
from luxai2021.env.agent import Agent
from luxai2021.game.constants import LuxMatchConfigs_Default
from luxai2021.game.actions import *

from functools import partial # pip install functools

# https://codereview.stackexchange.com/questions/28207/finding-the-closest-point-to-a-list-of-points
def closest_node(node, nodes):
    dist_2 = np.sum((nodes - node)**2, axis=1)
    return np.argmin(dist_2)

class AgentPolicy(Agent):
    def __init__(self, mode="train", model=None) -> None:
        """
        Arguments:
            mode: "train" or "inference", which controls if this agent is for training or not.
            model: The pretrained model, or if None it will operate in training mode.
        """
        self.model = model
        self.mode = mode

        # Define action and observation space
        # They must be gym.spaces objects
        # Example when using discrete actions:
        self.actionSpaceMap = [
            partial(MoveAction,direction=Constants.DIRECTIONS.CENTER), # This is the do-nothing action
            partial(MoveAction,direction=Constants.DIRECTIONS.NORTH),
            partial(MoveAction,direction=Constants.DIRECTIONS.WEST),
            partial(MoveAction,direction=Constants.DIRECTIONS.SOUTH),
            partial(MoveAction,direction=Constants.DIRECTIONS.EAST),
            #partial(TransferAction,direction=Constants.DIRECTIONS.NORTH),
            #partial(TransferAction,direction=Constants.DIRECTIONS.WEST),
            #partial(TransferAction,direction=Constants.DIRECTIONS.SOUTH),
            #partial(TransferAction,direction=Constants.DIRECTIONS.EAST),
            SpawnWorkerAction,
            SpawnCityAction,
            #ResearchAction,
            #PillageAction,
        ]
        self.action_space = spaces.Discrete(len(self.actionSpaceMap))

        # Observation space: (Basic minimum for a wood miner agent)
        #   5x direction_nearest_wood
        #   5x direction_nearest_city
        #   1x cargo size
        self.observation_shape = (11, )
        self.observation_space = spaces.Box(low=0, high=1, shape=
                        (11, ), dtype=np.float16)


    def getAgentType(self):
        """
        Returns the type of agent. Use AGENT for inference, and LEARNING for training a model.
        """
        if self.mode == "train":
            return Constants.AGENT_TYPE.LEARNING
        else:
            return Constants.AGENT_TYPE.AGENT

    def getObservation(self, game, unit, citytile, team, isNewTurn):
        """
        Implements getting a observation from the current game for this unit or city
        """
        if isNewTurn:
            # It's a new turn this event. This flag is set True for only the first observation from each turn.
            # Update any per-turn fixed observation space that doesn't change per unit/city controlled.

            # Build a list of object nodes by type for quick distance-searches
            self.objectNodes = {}

            # Add resources
            for cell in game.map.resources:
                if cell.resource.type not in self.objectNodes:
                    self.objectNodes[cell.resource.type] = np.array([[cell.pos.x, cell.pos.y]])
                else:
                    self.objectNodes[cell.resource.type] = np.concatenate(
                        (
                            self.objectNodes[cell.resource.type],
                            [[cell.pos.x, cell.pos.y]]
                        )
                        , axis=0
                    )
            
            # Add your own units
            for unit in game.state["teamStates"][team]["units"].values():
                if unit.type not in self.objectNodes:
                    self.objectNodes[unit.type] = np.array([[unit.pos.x, unit.pos.y]])
                else:
                    self.objectNodes[unit.type] = np.concatenate(
                        (
                            self.objectNodes[unit.type],
                            [[unit.pos.x, unit.pos.y]]
                        )
                        , axis=0
                    )
            
            # Add your own cities
            for city in game.cities.values():
                if city.team == team:
                    for cells in city.citycells:
                        if "city" not in self.objectNodes:
                            self.objectNodes["city"] = np.array([[cells.pos.x, cells.pos.y]])
                        else:
                            self.objectNodes["city"] = np.concatenate(
                                (
                                    self.objectNodes["city"],
                                    [[cells.pos.x, cells.pos.y]]
                                )
                                , axis=0
                            )

        # Observation space: (Basic minimum for a wood miner agent)
        #   5x direction_nearest_wood
        #   5x direction_nearest_city
        #   1x cargo size
        obs = np.zeros(self.observation_shape)
        if unit != None:
            # Encode the direction to the nearest wood
            if Constants.RESOURCE_TYPES.WOOD in self.objectNodes:
                closestWoodIndex = closest_node((unit.pos.x, unit.pos.y), self.objectNodes[Constants.RESOURCE_TYPES.WOOD])
                if closestWoodIndex != None and closestWoodIndex >= 0:
                    closestWood = self.objectNodes[Constants.RESOURCE_TYPES.WOOD][closestWoodIndex]
                    direction = unit.pos.directionTo( Position(closestWood[0], closestWood[1]) )
                    mapping = {
                        Constants.DIRECTIONS.CENTER: 0,
                        Constants.DIRECTIONS.NORTH: 1,
                        Constants.DIRECTIONS.WEST: 2,
                        Constants.DIRECTIONS.SOUTH: 3,
                        Constants.DIRECTIONS.EAST: 4,
                    }
                    obs[mapping[direction]] = 1.0 # One-hot encoding

            # Encode the direction to the nearest city
            if "city" in self.objectNodes:
                closestCityIndex = closest_node((unit.pos.x, unit.pos.y), self.objectNodes["city"])
                if closestCityIndex != None and closestCityIndex >= 0:
                    closestCity = self.objectNodes["city"][closestCityIndex]
                    direction = unit.pos.directionTo( Position(closestCity[0], closestCity[1]) )
                    mapping = {
                        Constants.DIRECTIONS.CENTER: 0,
                        Constants.DIRECTIONS.NORTH: 1,
                        Constants.DIRECTIONS.WEST: 2,
                        Constants.DIRECTIONS.SOUTH: 3,
                        Constants.DIRECTIONS.EAST: 4,
                    }
                    obs[5+mapping[direction]] = 1.0 # One-hot encoding

            # Encode the cargo space
            obs[2*5] = unit.getCargoSpaceLeft() / GAME_CONSTANTS["PARAMETERS"]["RESOURCE_CAPACITY"]["WORKER"]
        
        
        return obs
        #return np.array(np.zeros( self.observation_space.shape ) )

    def actionCodeToAction(self, actionCode, game, unit=None, citytile=None, team=None):
        """
        Takes an action in the environment according to actionCode:
            actionCode: Index of action to take into the action array.
        Returns: An action.
        """
        # Map actionCode index into to a constructed Action object
        try:
            return self.actionSpaceMap[actionCode](
                game = game,
                unitid = unit.id if unit else None,
                unit = unit,
                cityid = citytile.cityid if citytile else None,
                citytile = citytile,
                team = team
            )
        except Exception as e:
            # Not a valid action
            print(e)
            return None

    def takeAction(self, actionCode, game, unit=None, citytile=None, team=None):
        """
        Takes an action in the environment according to actionCode:
            actionCode: Index of action to take into the action array.
        """
        action = self.actionCodeToAction( actionCode, game, unit, citytile, team )
        self.matchController.takeAction( action )

    def getReward(self, game, isGameFinished, isNewTurn):
        """
        Returns the reward function for this step of the game.
        """
        if isGameFinished:
            # Give a reward of 1 or -1 based on if they won or not.
            if game.getWinningTeam() == self.team:
                print("Won match")
                return 1.0
            else:
                print("Lost match")
                return -1.0
        else:
            # If you want, any micro rewards or other rewards that are not win/lose end-of-game rewards
            return 0.0

    def processTurn(self, game, team):
        """
        Decides on a set of actions for the current turn. Not used in training, only inference.
        Returns: Array of actions to perform.
        """
        startTime = time.time()
        actions = []
        newTurn = True

        # Inference the model per-unit
        units = game.state["teamStates"][team]["units"].values()
        for unit in units:
            if unit.canAct():
                obs = self.getObservation(game, unit, None, unit.team, newTurn )
                actionCode, _states = self.model.predict(obs)
                if actionCode != None:
                    actions.append(self.actionCodeToAction(actionCode, game=game, unit=unit, citytile=None, team=unit.team))
                newTurn = False
        
        # Inference the model per-city
        cities = game.cities.values()
        for city in cities:
            if city.team == team:
                for cell in city.citycells:
                    citytile = cell.citytile
                    if citytile.canAct():
                        obs = self.getObservation(game, None, citytile, city.team, newTurn )
                        actionCode, _states = self.model.predict(obs)
                        if actionCode != None:
                            actions.append(self.actionCodeToAction(actionCode, game=game, unit=None, citytile=citytile, team=city.team))
                        newTurn = False

        timeTaken = time.time() - startTime
        if timeTaken > 0.5: # Warn if larger than 0.5 seconds.
            print("WARNING: Inference took %.3f seconds for computing actions. Limit is 1 second." % (timeTaken))
        
        return actions


if __name__ == "__main__":
    configs = LuxMatchConfigs_Default

    # Create a default opponent agent
    opponent = Agent()

    # Create a RL agent in training mode
    player = AgentPolicy(mode="train")
    
    # Train the model
    env = LuxEnvironment(configs, player, opponent)
    model = PPO("MlpPolicy", env, verbose=1)
    print("Training model...")
    model.learn(total_timesteps=20000)
    print("Done training model.")

    # Inference the model
    print("Inferencing model policy with rendering...")
    obs = env.reset()
    for i in range(400):
        actionCode, _states = model.predict(obs)
        obs, rewards, done, info = env.step(actionCode)
        if i % 50 == 0:
            print("Turn %i" % i)
            env.render()
        if done:
            print("Episode done, resetting.")
            obs = env.reset()
        env.close()
    print("Done")

    # Learn with self-play against the learned model as an opponent now
    print("Training model with self-play against last version of model...")
    player = AgentPolicy(mode="train")
    opponent = AgentPolicy(mode="inference", model=model)
    env = LuxEnvironment(configs, player, opponent)
    model = PPO("MlpPolicy", env, verbose=1)

    model.learn(total_timesteps=20000)
    env.close()
    print("Done")