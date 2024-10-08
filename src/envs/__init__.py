from circle_env import *
from mo_circle_env import *
from mo_highway_env import *
from moma_highway_env import *
from moma_circle_env import *
from gymnasium.envs.registration import register

# Hide pygame support prompt
import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'

register(
        id='circle-env-v0',
        entry_point='envs:CircleEnv',
    )
register(
        id='mo-circle-env-v0',
        entry_point='envs:MOCircleEnv',
    )
register(
        id="moma-circle-env-v0",
        entry_point='envs:MOMACircleEnv'
    )
register(
        id='mo-highway-env-v0',
        entry_point='envs:MOHighwayEnv',
    )
register(
        id="moma-highway-env-v0",
        entry_point='envs:MOMAHighwayEnv'
    )