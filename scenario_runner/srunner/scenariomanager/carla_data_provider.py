#!/usr/bin/env python

# Copyright (c) 2018-2019 Intel Corporation
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""
This module provides all frequently used data from CARLA via
local buffers to avoid blocking calls to CARLA
"""

from __future__ import print_function

import logging
import math
import random
import re
from six import iteritems

import carla


def calculate_velocity(actor):
    """
    Method to calculate the velocity of a actor
    """
    velocity_squared = actor.get_velocity().x**2
    velocity_squared += actor.get_velocity().y**2
    return math.sqrt(velocity_squared)


class CarlaDataProvider(object):

    """
    This class provides access to various data of all registered actors
    It buffers the data and updates it on every CARLA tick

    Currently available data:
    - Absolute velocity
    - Location
    - Transform

    Potential additions:
    - Acceleration


    In addition it provides access to the map and the transform of all traffic lights
    """

    _actor_velocity_map = dict()
    _actor_location_map = dict()
    _actor_transform_map = dict()
    _traffic_light_map = dict()
    _map = None
    _world = None
    _sync_flag = False
    _ego_vehicle_route = None

    @staticmethod
    def register_actor(actor):
        """
        Add new actor to dictionaries
        If actor already exists, throw an exception
        """
        if actor in CarlaDataProvider._actor_velocity_map:
            raise KeyError(
                "Vehicle '{}' already registered. Cannot register twice!".format(actor))
        else:
            CarlaDataProvider._actor_velocity_map[actor] = 0.0

        if actor in CarlaDataProvider._actor_location_map:
            raise KeyError(
                "Vehicle '{}' already registered. Cannot register twice!".format(actor.id))
        else:
            CarlaDataProvider._actor_location_map[actor] = None

        if actor in CarlaDataProvider._actor_transform_map:
            raise KeyError(
                "Vehicle '{}' already registered. Cannot register twice!".format(actor.id))
        else:
            CarlaDataProvider._actor_transform_map[actor] = None

    @staticmethod
    def register_actors(actors):
        """
        Add new set of actors to dictionaries
        """
        for actor in actors:
            CarlaDataProvider.register_actor(actor)

    @staticmethod
    def on_carla_tick():
        """
        Callback from CARLA
        """
        for actor in CarlaDataProvider._actor_velocity_map:
            if actor is not None and actor.is_alive:
                CarlaDataProvider._actor_velocity_map[actor] = calculate_velocity(actor)

        for actor in CarlaDataProvider._actor_location_map:
            if actor is not None and actor.is_alive:
                CarlaDataProvider._actor_location_map[actor] = actor.get_location()

        for actor in CarlaDataProvider._actor_transform_map:
            if actor is not None and actor.is_alive:
                CarlaDataProvider._actor_transform_map[actor] = actor.get_transform()

    @staticmethod
    def get_velocity(actor):
        """
        returns the absolute velocity for the given actor
        """
        for key in CarlaDataProvider._actor_velocity_map:
            if key.id == actor.id:
                return CarlaDataProvider._actor_velocity_map[key]

        # We are intentionally not throwing here
        # This may cause exception loops in py_trees
        print('{}.get_velocity: {} not found!' .format(__name__, actor))
        return 0.0

    @staticmethod
    def get_location(actor):
        """
        returns the location for the given actor
        """
        for key in CarlaDataProvider._actor_location_map:
            if key.id == actor.id:
                return CarlaDataProvider._actor_location_map[key]

        # We are intentionally not throwing here
        # This may cause exception loops in py_trees
        print('{}.get_location: {} not found!' .format(__name__, actor))
        return None

    @staticmethod
    def get_transform(actor):
        """
        returns the transform for the given actor
        """
        for key in CarlaDataProvider._actor_transform_map:
            if key.id == actor.id:
                return CarlaDataProvider._actor_transform_map[key]

        # We are intentionally not throwing here
        # This may cause exception loops in py_trees
        print('{}.get_transform: {} not found!' .format(__name__, actor))
        return None

    @staticmethod
    def prepare_map():
        """
        This function set the current map and loads all traffic lights for this map to
        _traffic_light_map
        """
        if CarlaDataProvider._map is None:
            CarlaDataProvider._map = CarlaDataProvider._world.get_map()

        # Parse all traffic lights
        CarlaDataProvider._traffic_light_map.clear()
        for traffic_light in CarlaDataProvider._world.get_actors().filter('*traffic_light*'):
            if traffic_light not in CarlaDataProvider._traffic_light_map.keys():
                CarlaDataProvider._traffic_light_map[traffic_light] = traffic_light.get_transform()
            else:
                raise KeyError(
                    "Traffic light '{}' already registered. Cannot register twice!".format(traffic_light.id))

    @staticmethod
    def get_world():
        """
        Return world
        """
        return CarlaDataProvider._world

    @staticmethod
    def is_sync_mode():
        """
        @return true if syncronuous mode is used
        """
        return CarlaDataProvider._sync_flag

    @staticmethod
    def set_world(world):
        """
        Set the world and world settings
        """
        CarlaDataProvider._world = world
        settings = world.get_settings()
        CarlaDataProvider._sync_flag = settings.synchronous_mode
        CarlaDataProvider._map = CarlaDataProvider._world.get_map()

    @staticmethod
    def get_map(world=None):
        """
        Get the current map
        """
        if CarlaDataProvider._map is None:
            if world is None:
                if CarlaDataProvider._world is None:
                    raise ValueError("class member \'world'\' not initialized yet")
                else:
                    CarlaDataProvider._map = CarlaDataProvider._world.get_map()
            else:
                CarlaDataProvider._map = world.get_map()

        return CarlaDataProvider._map

    @staticmethod
    def annotate_trafficlight_in_group(traffic_light):
        """
        Get dictionary with traffic light group info for a given traffic light
        """
        dict_annotations = {'ref': [], 'opposite': [], 'left': [], 'right': []}

        ref_yaw = traffic_light.get_transform().rotation.yaw
        group_tl = traffic_light.get_group_traffic_lights()
        for target_tl in group_tl:
            target_yaw = target_tl.get_transform().rotation.yaw
            diff = target_yaw - ref_yaw
            if diff < 0.0:
                diff = 360.0 + diff

            if diff <= 45.0 or diff > 340.0:
                dict_annotations['ref'].append(target_tl)
            elif diff > 240 and diff < 300:
                dict_annotations['left'].append(target_tl)
            elif diff > 160.0 and diff <= 240.0:
                dict_annotations['opposite'].append(target_tl)
            else:
                dict_annotations['right'].append(target_tl)

        return dict_annotations

    @staticmethod
    def update_light_states(ego_light, annotations, states, freeze=False, timeout=1000000000):
        """
        Update traffic light states
        """
        reset_params = []

        if 'ego' in states:
            prev_state = ego_light.get_state()
            prev_green_time = ego_light.get_green_time()
            prev_red_time = ego_light.get_red_time()
            prev_yellow_time = ego_light.get_yellow_time()
            reset_params.append({'light': ego_light,
                                 'state': prev_state,
                                 'green_time': prev_green_time,
                                 'red_time': prev_red_time,
                                 'yellow_time': prev_yellow_time})

            ego_light.set_state(states['ego'])
            if freeze:
                ego_light.set_green_time(timeout)
                ego_light.set_red_time(timeout)
                ego_light.set_yellow_time(timeout)
        if 'ref' in states:
            for light in annotations['ref']:
                prev_state = light.get_state()
                prev_green_time = light.get_green_time()
                prev_red_time = light.get_red_time()
                prev_yellow_time = light.get_yellow_time()
                reset_params.append({'light': light,
                                     'state': prev_state,
                                     'green_time': prev_green_time,
                                     'red_time': prev_red_time,
                                     'yellow_time': prev_yellow_time})

                light.set_state(states['ref'])
                if freeze:
                    light.set_green_time(timeout)
                    light.set_red_time(timeout)
                    light.set_yellow_time(timeout)
        if 'left' in states:
            for light in annotations['left']:
                prev_state = light.get_state()
                prev_green_time = light.get_green_time()
                prev_red_time = light.get_red_time()
                prev_yellow_time = light.get_yellow_time()
                reset_params.append({'light': light,
                                     'state': prev_state,
                                     'green_time': prev_green_time,
                                     'red_time': prev_red_time,
                                     'yellow_time': prev_yellow_time})

                light.set_state(states['left'])
                if freeze:
                    light.set_green_time(timeout)
                    light.set_red_time(timeout)
                    light.set_yellow_time(timeout)
        if 'right' in states:
            for light in annotations['right']:
                prev_state = light.get_state()
                prev_green_time = light.get_green_time()
                prev_red_time = light.get_red_time()
                prev_yellow_time = light.get_yellow_time()
                reset_params.append({'light': light,
                                     'state': prev_state,
                                     'green_time': prev_green_time,
                                     'red_time': prev_red_time,
                                     'yellow_time': prev_yellow_time})

                light.set_state(states['right'])
                if freeze:
                    light.set_green_time(timeout)
                    light.set_red_time(timeout)
                    light.set_yellow_time(timeout)
        if 'opposite' in states:
            for light in annotations['opposite']:
                prev_state = light.get_state()
                prev_green_time = light.get_green_time()
                prev_red_time = light.get_red_time()
                prev_yellow_time = light.get_yellow_time()
                reset_params.append({'light': light,
                                     'state': prev_state,
                                     'green_time': prev_green_time,
                                     'red_time': prev_red_time,
                                     'yellow_time': prev_yellow_time})

                light.set_state(states['opposite'])
                if freeze:
                    light.set_green_time(timeout)
                    light.set_red_time(timeout)
                    light.set_yellow_time(timeout)

        return reset_params

    @staticmethod
    def reset_lights(reset_params):
        """
        Reset traffic lights
        """
        for param in reset_params:
            param['light'].set_state(param['state'])
            param['light'].set_green_time(param['green_time'])
            param['light'].set_red_time(param['red_time'])
            param['light'].set_yellow_time(param['yellow_time'])

    @staticmethod
    def get_next_traffic_light(actor, use_cached_location=True):
        """
        returns the next relevant traffic light for the provided actor
        """

        CarlaDataProvider.prepare_map()
        location = CarlaDataProvider.get_location(actor)

        if not use_cached_location:
            location = actor.get_transform().location

        waypoint = CarlaDataProvider._map.get_waypoint(location)
        # Create list of all waypoints until next intersection
        list_of_waypoints = []
        while waypoint and not waypoint.is_intersection:
            list_of_waypoints.append(waypoint)
            waypoint = waypoint.next(2.0)[0]

        # If the list is empty, the actor is in an intersection
        if not list_of_waypoints:
            return None

        relevant_traffic_light = None
        distance_to_relevant_traffic_light = float("inf")

        for traffic_light in CarlaDataProvider._traffic_light_map:
            if hasattr(traffic_light, 'trigger_volume'):
                tl_t = CarlaDataProvider._traffic_light_map[traffic_light]
                transformed_tv = tl_t.transform(traffic_light.trigger_volume.location)
                distance = carla.Location(transformed_tv).distance(list_of_waypoints[-1].transform.location)

                if distance < distance_to_relevant_traffic_light:
                    relevant_traffic_light = traffic_light
                    distance_to_relevant_traffic_light = distance

        return relevant_traffic_light

    @staticmethod
    def set_ego_vehicle_route(route):
        """
        Set the route of the ego vehicle

        @todo extend ego_vehicle_route concept to support multi ego_vehicle scenarios
        """
        CarlaDataProvider._ego_vehicle_route = route

    @staticmethod
    def get_ego_vehicle_route():
        """
        returns the currently set route of the ego vehicle
        Note: Can be None
        """
        return CarlaDataProvider._ego_vehicle_route

    @staticmethod
    def find_weather_presets():
        """
        Get weather presets from CARLA
        """
        rgx = re.compile('.+?(?:(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|$)')
        name = lambda x: ' '.join(m.group(0) for m in rgx.finditer(x))
        presets = [x for x in dir(carla.WeatherParameters) if re.match('[A-Z].+', x)]
        return [(getattr(carla.WeatherParameters, x), name(x)) for x in presets]

    @staticmethod
    def cleanup():
        """
        Cleanup and remove all entries from all dictionaries
        """
        CarlaDataProvider._actor_velocity_map.clear()
        CarlaDataProvider._actor_location_map.clear()
        CarlaDataProvider._actor_transform_map.clear()
        CarlaDataProvider._traffic_light_map.clear()
        CarlaDataProvider._map = None
        CarlaDataProvider._world = None
        CarlaDataProvider._sync_flag = False
        CarlaDataProvider._ego_vehicle_route = None


class CarlaActorPool(object):

    """
    The CarlaActorPool caches all scenario relevant actors.
    It works similar to a singelton.

    An actor can be created via "request_actor", and access
    is possible via "get_actor_by_id".

    Using CarlaActorPool, actors can be shared between scenarios.
    """
    _client = None
    _world = None
    _carla_actor_pool = dict()
    _spawn_points = None
    _spawn_index = 0

    @staticmethod
    def set_client(client):
        """
        Set the CARLA client
        """
        CarlaActorPool._client = client

    @staticmethod
    def set_world(world):
        """
        Set the CARLA world
        """
        CarlaActorPool._world = world
        CarlaActorPool.generate_spawn_points()

    @staticmethod
    def get_actors():
        """
        Return list of actors and their ids

        Note: iteritems from six is used to allow compatibility with Python 2 and 3
        """
        return iteritems(CarlaActorPool._carla_actor_pool)

    @staticmethod
    def generate_spawn_points():
        """
        Generate spawn points for the current map
        """
        spawn_points = list(CarlaDataProvider.get_map(CarlaActorPool._world).get_spawn_points())
        random.shuffle(spawn_points)
        CarlaActorPool._spawn_points = spawn_points
        CarlaActorPool._spawn_index = 0

    @staticmethod
    def setup_actor(model, spawn_point, rolename='scenario', hero=False, autopilot=False,
                    random_location=False, color=None, vehicle_category="car"):
        """
        Function to setup the most relevant actor parameters,
        incl. spawn point and vehicle model.
        """

        _vehicle_blueprint_categories = {
            'car': 'vehicle.tesla.model3',
            'van': 'vehicle.volkswagen.t2',
            'truck': 'vehicle.carlamotors.carlacola',
            'trailer': '',
            'semitrailer': '',
            'bus': 'vehicle.volkswagen.t2',
            'motorbike': 'vehicle.kawasaki.ninja',
            'bicycle': 'vehicle.diamondback.century',
            'train': '',
            'tram': '',
        }

        blueprint_library = CarlaActorPool._world.get_blueprint_library()

        # Get vehicle by model
        try:
            blueprint = random.choice(blueprint_library.filter(model))
        except:
            # The model is not part of the blueprint library. Let's take a default one for the given category
            bp_filter = "vehicle.*"
            new_model = _vehicle_blueprint_categories[vehicle_category]
            if new_model != '':
                bp_filter = new_model
            print("WARNING: Actor model {} not available. Using instead {}".format(model, new_model))
            blueprint = random.choice(blueprint_library.filter(bp_filter))
        try:
            if color:
                blueprint.set_attribute('color', color)
        except:
            pass
            # Color can't be set for this vehicle

        # is it a pedestrian? -> make it mortal
        if blueprint.has_attribute('is_invincible'):
            blueprint.set_attribute('is_invincible', 'false')

        if autopilot:
            blueprint.set_attribute('role_name', 'autopilot')
        else:
            blueprint.set_attribute('role_name', rolename)

        if random_location:
            actor = None
            while not actor:
                spawn_point = random.choice(CarlaActorPool._spawn_points)
                actor = CarlaActorPool._world.try_spawn_actor(blueprint, spawn_point)

        else:
            # slightly lift the actor to avoid collisions with ground when spawning the actor
            # DO NOT USE spawn_point directly, as this will modify spawn_point permanently
            _spawn_point = carla.Transform(carla.Location(), spawn_point.rotation)
            _spawn_point.location.x = spawn_point.location.x
            _spawn_point.location.y = spawn_point.location.y
            _spawn_point.location.z = spawn_point.location.z + 0.2
            actor = CarlaActorPool._world.try_spawn_actor(blueprint, _spawn_point)

        if actor is None:
            raise RuntimeError(
                "Error: Unable to spawn vehicle {} at {}".format(model, spawn_point))
        else:
            # Let's deactivate the autopilot of the actor if it belongs to vehicle
            if actor in blueprint_library.filter('vehicle.*'):
                actor.set_autopilot(autopilot)
            else:
                pass
        # wait for the actor to be spawned properly before we do anything
        if CarlaActorPool._world.get_settings().synchronous_mode:
            CarlaActorPool._world.tick()
        else:
            CarlaActorPool._world.wait_for_tick()

        return actor

    @staticmethod
    def _sync(frame):
        while frame > CarlaActorPool._world.get_snapshot().timestamp.frame:
            pass
        assert frame == CarlaActorPool._world.get_snapshot().timestamp.frame

    @staticmethod
    def setup_batch_actors(model, amount, spawn_point, hero=False, autopilot=False,
                           random_location=False, cross_factor=0.01):
        """
        Function to setup a batch of actors with the most relevant parameters,
        incl. spawn point and vehicle model.
        """
        SpawnActor = carla.command.SpawnActor       # pylint: disable=invalid-name
        SetAutopilot = carla.command.SetAutopilot   # pylint: disable=invalid-name
        FutureActor = carla.command.FutureActor     # pylint: disable=invalid-name

        blueprint_library = CarlaActorPool._world.get_blueprint_library()

        if not hero:
            hero_actor = CarlaActorPool.get_hero_actor()
        else:
            hero_actor = None
        batch = []
        walker_speed = []
        for _ in range(amount):
            # Get vehicle by model
            blueprint = random.choice(blueprint_library.filter(model))
            # is it a pedestrian? -> make it mortal
            if blueprint.has_attribute('is_invincible'):
                blueprint.set_attribute('is_invincible', 'false')

            if hero:
                blueprint.set_attribute('role_name', 'hero')
            elif 'walker' in model:
                blueprint.set_attribute('role_name', 'walker')
            elif autopilot:
                blueprint.set_attribute('role_name', 'autopilot')
            else:
                blueprint.set_attribute('role_name', 'scenario')

            if random_location:
                if 'walker' in model:
                    spawn_point = carla.Transform()
                    spawn_point.location = CarlaDataProvider._world.get_random_location_from_navigation()
                    spawn_point.location.z = spawn_point.location.z + 1.0
                    if spawn_point.location is None:
                        print ("Wrong location")
                elif CarlaActorPool._spawn_index >= len(CarlaActorPool._spawn_points):

                    CarlaActorPool._spawn_index = len(CarlaActorPool._spawn_points)
                    spawn_point = None
                elif hero_actor is not None:
                    spawn_point = CarlaActorPool._spawn_points[CarlaActorPool._spawn_index]
                    CarlaActorPool._spawn_index += 1
                    # if the spawn point is to close to hero we just ignore this position
                    if hero_actor.get_transform().location.distance(spawn_point.location) < 8.0:
                        spawn_point = None
                else:
                    spawn_point = CarlaActorPool._spawn_points[CarlaActorPool._spawn_index]
                    CarlaActorPool._spawn_index += 1
            if spawn_point:
                if 'walker' in model:  # If the model is a walker we try to directly set the autopilot to it.
                    walker_bp = random.choice(blueprint_library.filter('walker.pedestrian*'))
                    # set as not invencible
                    if walker_bp.has_attribute('is_invincible'):
                        walker_bp.set_attribute('is_invincible', 'false')

                    walker_bp.set_attribute('role_name', 'walker')

                    if walker_bp.has_attribute('speed'):
                        if (random.random() > 0.1):
                            # walking
                            walker_speed.append(
                                walker_bp.get_attribute('speed').recommended_values[1])
                        else:
                            # running
                            walker_speed.append(
                                walker_bp.get_attribute('speed').recommended_values[2])
                    else:
                        print("Walker has no speed")

                    walker_shape = SpawnActor(walker_bp, spawn_point)
                    batch.append(walker_shape)

                else:
                    logging.debug("Spawn Vehicle !!!")
                    batch.append(SpawnActor(blueprint, spawn_point).then(SetAutopilot(FutureActor,
                                                                                      autopilot)))

        if CarlaActorPool._client:
            responses = CarlaActorPool._client.apply_batch_sync(batch, True)

        # wait for the actors to be spawned properly before we do anything
        CarlaActorPool._sync(CarlaActorPool._world.tick())

        actor_list = []
        actor_ids = []
        controllers_ids = []
        controllers = []
        if responses:
            for response in responses:
                if not response.error:
                    if 'walker' in CarlaActorPool._world.get_actor(response.actor_id).type_id:

                        logging.debug("SPAWN WALKER CONTROL into ID %d" % response.actor_id)
                        walker_controller_bp = blueprint_library.find(
                            'controller.ai.walker')
                        walker_control = SpawnActor(walker_controller_bp, carla.Transform(),
                                                    response.actor_id)
                        controllers.append(walker_control)
                    # Regardless of being a walker or a vehicle we add to the list
                    actor_ids.append(response.actor_id)
                else:
                    print ("Response", response.error)

        # Second spawn for the controllers
        if CarlaActorPool._client:
            results_controler = CarlaActorPool._client.apply_batch_sync(controllers)

        # Get the spawned controllers iD.
        for i in range(len(results_controler)):
            if results_controler[i].error:
                logging.error(results_controler[i].error)
            else:
                controllers_ids.append(results_controler[i].actor_id)

        carla_actors = CarlaActorPool._world.get_actors(actor_ids)
        for actor in carla_actors:
            actor_list.append(actor)

        walkers_present = CarlaActorPool._world.get_actors(controllers_ids)

        # This function set how often pedestrians cross
        CarlaActorPool._world.set_pedestrians_cross_factor(cross_factor)

        for i in range(0, len(walkers_present)):
            # start walker
            walkers_present[i].start()
            # set walk to random point
            location_to_go = CarlaActorPool._world.get_random_location_from_navigation()
            walkers_present[i].go_to_location(location_to_go)
            # random max speed
            walkers_present[i].set_max_speed(1 + random.random())    # max speed between 1 and 2 (default is 1.4 m/s)
            #logging.debug(f" walker send to location {location_to_go.x},"
            #              f"        {location_to_go.y} {location_to_go.z} ")

        return actor_list

    @staticmethod
    def request_new_batch_actors(model, amount, spawn_point, hero=False, autopilot=False,
                                 random_location=False, cross_factor=0.01):
        """
        This method tries to create a new actor. If this was
        successful, the new actor is returned, None otherwise.
        """
        actors = CarlaActorPool.setup_batch_actors(model, amount, spawn_point, hero=hero,
                                                   autopilot=autopilot,
                                                   random_location=random_location,
                                                   cross_factor=cross_factor)

        if actors is None:
            return None

        for actor in actors:
            CarlaActorPool._carla_actor_pool[actor.id] = actor
        return actors

    @staticmethod
    def request_new_actor(model, spawn_point, rolename='scenario', hero=False, autopilot=False,
                          random_location=False, color=None, vehicle_category=None):
        """
        This method tries to create a new actor. If this was
        successful, the new actor is returned, None otherwise.
        """
        actor = CarlaActorPool.setup_actor(
            model, spawn_point, rolename, hero, autopilot, random_location, color, vehicle_category)

        if actor is None:
            return None

        CarlaActorPool._carla_actor_pool[actor.id] = actor
        return actor

    @staticmethod
    def actor_id_exists(actor_id):
        """
        Check if a certain id is still at the simulation
        """
        if actor_id in CarlaActorPool._carla_actor_pool:
            return True

        return False

    @staticmethod
    def get_hero_actor():
        """
        Get the actor object of the hero actor if it exists, returns none otherwise.
        """
        for actor_id in CarlaActorPool._carla_actor_pool:
            if CarlaActorPool._carla_actor_pool[actor_id].attributes['role_name'] == 'hero':
                return CarlaActorPool._carla_actor_pool[actor_id]
        return None

    @staticmethod
    def get_actor_by_id(actor_id):
        """
        Get an actor from the pool by using its ID. If the actor
        does not exist, None is returned.
        """
        if actor_id in CarlaActorPool._carla_actor_pool:
            return CarlaActorPool._carla_actor_pool[actor_id]

        print("Non-existing actor id {}".format(actor_id))
        return None

    @staticmethod
    def remove_actor_by_id(actor_id):
        """
        Remove an actor from the pool using its ID
        """
        if actor_id in CarlaActorPool._carla_actor_pool:
            CarlaActorPool._carla_actor_pool[actor_id].destroy()
            CarlaActorPool._carla_actor_pool[actor_id] = None
            CarlaActorPool._carla_actor_pool.pop(actor_id)
        else:
            print("Trying to remove a non-existing actor id {}".format(actor_id))

    @staticmethod
    def cleanup():
        """
        Cleanup the actor pool, i.e. remove and destroy all actors
        """
        for actor_id in CarlaActorPool._carla_actor_pool.copy():
            CarlaActorPool._carla_actor_pool[actor_id].destroy()
            CarlaActorPool._carla_actor_pool.pop(actor_id)

        CarlaActorPool._carla_actor_pool = dict()
        CarlaActorPool._world = None
        CarlaActorPool._client = None
        CarlaActorPool._spawn_points = None
        CarlaActorPool._spawn_index = 0

    @staticmethod
    def remove_actors_in_surrounding(location, distance):
        """
        Remove all actors from the pool that are closer than distance to the
        provided location
        """
        for actor_id in CarlaActorPool._carla_actor_pool.copy():
            if CarlaActorPool._carla_actor_pool[actor_id].get_location().distance(location) < distance:
                CarlaActorPool._carla_actor_pool[actor_id].destroy()
                CarlaActorPool._carla_actor_pool.pop(actor_id)

        # Remove all keys with None values
        CarlaActorPool._carla_actor_pool = dict({k: v for k, v in CarlaActorPool._carla_actor_pool.items() if v})