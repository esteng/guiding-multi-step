#!/usr/bin/env python

import time
import os
import pdb
import signal
import sys
import random
import threading
import argparse
import torch
from torch.autograd import Variable
import numpy as np
import scipy as sc
import cv2
from collections import namedtuple
from robot import Robot
from trainer import Trainer
from logger import Logger
import utils
from utils import ACTION_TO_ID
from utils import ID_TO_ACTION
from utils import StackSequence
from utils import compute_demo_dist
from utils import annotate_success_manually
from utils_torch import action_space_argmax
from utils_torch import action_space_explore_random
from demo import Demonstration, load_all_demos
import plot
import json
import copy
import shutil
import matplotlib
import matplotlib.pyplot as plt
from data import DatasetReader, GoodRobotDatasetReader
from generate_logoblocks_images import BlockSetter
from annotate_data import Pair

def run_title(args):
    """
    # Returns

    title, dirname
    """
    title = ''
    title += 'Sim ' if args.is_sim else 'Real '

    if args.task_type is not None:
        if args.task_type == 'vertical_square':
            title += 'Vertical Square, '
        elif args.task_type == 'unstack':
            title += 'Unstacking, '
        elif args.task_type == 'stack':
            title += 'Stack, '
        elif args.task_type == 'row':
            title += 'Row, '

    elif args.check_row:
        title += 'Rows, '

    elif args.place:
        title += 'Stack, '

    elif not args.place and not args.check_row:
        title += 'Push and Grasp, '

    if args.use_demo:
        title += 'Imitation, '
    elif args.trial_reward:
        title += 'SPOT Trial Reward, '
    elif args.discounted_reward:
        title += 'Discounted Reward, '
    else:
        title += 'Two Step Reward, '

    if args.common_sense:
        title += 'Masked, '

    if not args.test_preset_cases:
        title += 'Testing' if args.is_testing else 'Training'
    else:
        title += 'Challenging Arrangements'

    if args.depth_channels_history:
        title += ', Three Step History'

    save_file = os.path.basename(title).replace(':', '-').replace('.', '-').replace(',','').replace(' ','-')
    dirname = utils.timeStamped(save_file)
    return title, dirname

def main(args):
    # TODO(ahundt) move main and process_actions() to a class?

    num_problems_detected = 0
    # --------------- Setup options ---------------
    is_sim = args.is_sim # Run in simulation?
    obj_mesh_dir = os.path.abspath(args.obj_mesh_dir) if is_sim else None # Directory containing 3D mesh files (.obj) of objects to be added to simulation
    num_obj = args.num_obj if is_sim or args.check_row else None # Number of objects to add to simulation
    goal_num_obj = args.goal_num_obj
    num_extra_obj = args.num_extra_obj if is_sim or args.check_row else None
    timeout = args.timeout # time to wait before simulator reset
    if num_obj is not None:
        num_obj += num_extra_obj
    if args.check_row:
        print('Overriding --num_obj to 4 because we have --check_row and will expect 4 blocks in a row.')
        num_obj = 4
    tcp_host_ip = args.tcp_host_ip if not is_sim else None # IP and port to robot arm as TCP client (UR5)
    tcp_port = args.tcp_port  # TODO(killeen) change the rest of these?
    rtc_host_ip = args.rtc_host_ip if not is_sim else None # IP and port to robot arm as real-time client (UR5)
    rtc_port = args.rtc_port if not is_sim else None
    if is_sim:
        workspace_limits = np.asarray([[-0.724, -0.276], [-0.224, 0.224], [-0.0001, 0.5]]) # Cols: min max, Rows: x y z (define workspace limits in robot coordinates)
    else:
        # Corner near window on robot base side
        # [0.47984089 0.34192974 0.02173636]
        # Corner on the side of the cameras and far from the window
        # [ 0.73409861 -0.45199446 -0.00229499]
        # Dimensions of workspace should be 448 mm x 448 mm. That's 224x224 pixels with each pixel being 2mm x2mm.
        workspace_limits = np.asarray([[0.376, 0.824], [-0.264, 0.184], [-0.07, 0.4]]) # Cols: min max, Rows: x y z (define workspace limits in robot coordinates)
        if args.place:
            # The object sets differ for stacking, so add a bit to min z.
            # TODO(ahundt) this keeps the real gripper from colliding with the block and causing a security stop when it misses a grasp on top of blocks. However, it makes the stacks appear shorter than they really are too, so this needs to be fixed in a more nuanced way.
            workspace_limits[2][0] += 0.02

        # Original visual pushing graping paper workspace definition
        # workspace_limits = np.asarray([[0.3, 0.748], [-0.224, 0.224], [-0.255, -0.1]]) # Cols: min max, Rows: x y z (define workspace limits in robot coordinates)
    heightmap_resolution = args.heightmap_resolution # Meters per pixel of heightmap
    random_seed = args.random_seed
    force_cpu = args.force_cpu
    flops = args.flops
    show_heightmap = args.show_heightmap
    max_train_actions = args.max_train_actions

    # ------------- Algorithm options -------------
    method = args.method # 'reactive' (supervised learning) or 'reinforcement' (reinforcement learning ie Q-learning)
    push_rewards = args.push_rewards if method == 'reinforcement' else None  # Use immediate rewards (from change detection) for pushing?
    future_reward_discount = args.future_reward_discount
    experience_replay_enabled = args.experience_replay # Use prioritized experience replay?
    trial_reward = args.trial_reward
    discounted_reward = args.discounted_reward
    heuristic_bootstrap = args.heuristic_bootstrap # Use handcrafted grasping algorithm when grasping fails too many times in a row?
    explore_rate_decay = args.explore_rate_decay
    grasp_only = args.grasp_only
    check_row = args.check_row
    check_z_height = args.check_z_height
    check_z_height_goal = args.check_z_height_goal
    check_z_height_max = args.check_z_height_max
    pretrained = not args.random_weights
    max_iter = args.max_iter
    no_height_reward = args.no_height_reward
    transfer_grasp_to_place = args.transfer_grasp_to_place
    neural_network_name = args.nn
    num_dilation = args.num_dilation
    disable_situation_removal = args.disable_situation_removal
    evaluate_random_objects = args.evaluate_random_objects
    skip_noncontact_actions = args.skip_noncontact_actions
    common_sense = args.common_sense
    place_common_sense = common_sense and ((args.task_type is None) or ('unstack' not in args.task_type))
    print('main.py using common sense:', common_sense, 'using place common sense:', place_common_sense)
    common_sense_backprop = not args.no_common_sense_backprop
    disable_two_step_backprop = args.disable_two_step_backprop
    random_trunk_weights_max = args.random_trunk_weights_max
    random_trunk_weights_reset_iters = args.random_trunk_weights_reset_iters
    random_trunk_weights_min_success = args.random_trunk_weights_min_success
    random_actions = args.random_actions
    # TODO(zhe) Added static language mask option
    static_language_mask = args.static_language_mask
    baseline_language_mask = args.baseline_language_mask
    is_bisk = args.is_bisk
    randomized = static_language_mask # If we are using the language mask, we are using the logoblock_dataset
    obj_scale = 0.00018 if args.is_bisk else 1 # Hard coded value based on logoblock mesh size.
    language_model_config = args.language_model_config
    language_model_weights = args.language_model_weights
    end_on_incorrect_order = args.end_on_incorrect_order
    separation_threshold = args.separation_threshold
    distance_threshold = args.distance_threshold

    # -------------- Demo options -----------------------
    use_demo = args.use_demo
    demo_path = args.demo_path
    task_type = args.task_type
    primitive_distance_method = args.primitive_distance_method
    cycle_consistency = args.cycle_consistency
    depth_channels_history = args.depth_channels_history

    # NOTE(adit98) HACK, make sure we set task_type to 'unstack' and not 'unstacking'
    if task_type is not None and 'unstack' in args.task_type:
        args.task_type = 'unstack'
        task_type = 'unstack'

    # -------------- Test grasping options --------------
    is_testing = args.is_testing
    if is_testing:
        print('Testing mode detected, automatically disabling situation removal.')
        disable_situation_removal = True
    max_test_trials = args.max_test_trials # Maximum number of test runs per case/scenario

    # set max trial actions
    if is_testing:
        max_trial_actions = args.max_trial_actions_test
    else:
        max_trial_actions = args.max_trial_actions_train

    test_preset_cases = args.test_preset_cases
    trials_per_case = 1
    show_preset_cases_then_exit = args.show_preset_cases_then_exit
    if show_preset_cases_then_exit:
        test_preset_cases = True
    if test_preset_cases:
        if args.test_preset_file:
            # load just one specific file
            preset_files = [os.path.abspath(args.test_preset_file)]
        else:
            # load a directory of files
            preset_files = os.listdir(args.test_preset_dir)
            preset_files = [os.path.abspath(os.path.join(args.test_preset_dir, filename)) for filename in preset_files]
            preset_files = sorted(preset_files)
        trials_per_case = max_test_trials
        # run each preset file max_test_trials times.
        max_test_trials *= len(preset_files)
        test_preset_file = preset_files[0]
    else:
        preset_files = None
        test_preset_file = None

    unstack = args.unstack
    if args.place and not args.is_sim:
        unstack = True
        args.unstack = True
        print('--unstack is automatically enabled')

    # ------ Pre-loading and logging options ------
    snapshot_file, multi_task_snapshot_files, continue_logging, logging_directory = parse_resume_and_snapshot_file_args(args)

    save_visualizations = args.save_visualizations  # Save visualizations of FCN predictions? Takes 0.6s per training step if set to True
    plot_window = args.plot_window

    # ------ Stacking Blocks and Grasping Specific Colors -----
    grasp_color_task = args.grasp_color_task
    place = args.place
    if grasp_color_task:
        if not is_sim:
            raise NotImplementedError('Real execution goal conditioning is not yet implemented')
        goal_condition_len = num_obj
    else:
        goal_condition_len = 0

    # Set random seed
    np.random.seed(random_seed)

    # Initialize pick-and-place system (camera and robot)
    # TODO(zhe) modify the None here to ensure that the test_preset_arr option is set correctly
    robot = Robot(is_sim, obj_mesh_dir, num_obj, workspace_limits,
                  tcp_host_ip, tcp_port, rtc_host_ip, rtc_port,
                  is_testing, test_preset_cases, test_preset_file, None,
                  place, grasp_color_task, unstack=unstack,
                  heightmap_resolution=heightmap_resolution, randomized=randomized, obj_scale=obj_scale, task_type=task_type,
                  language=static_language_mask)

    # Set the "common sense" dynamic action space region around objects,
    # which defines where place actions are permitted. Units are in meters.
    if check_row:
        place_dilation = 0.05
    elif task_type is not None:
        # TODO(adit98) think about how we set different place dilations
        stack_place_dilation = 0.00
        place_dilation = 0.05
    else:
        place_dilation = 0.00

    # Initialize trainer(s)
    if use_demo:
        print("main.py stacking place dilation:", stack_place_dilation, "place_dilation:", place_dilation)
        assert task_type is not None, ("Must provide task_type if using demo")
        assert is_testing, ("Must run in testing mode if using demo")
        stack_trainer, row_trainer, unstack_trainer, vertical_square_trainer = None, None, None, None
        if 'stack' in multi_task_snapshot_files:
            stack_trainer = Trainer(method, push_rewards, future_reward_discount,
                              is_testing, multi_task_snapshot_files['stack'], force_cpu,
                              goal_condition_len, place, pretrained, flops,
                              network=neural_network_name, common_sense=common_sense,
                              place_common_sense=place_common_sense, show_heightmap=show_heightmap,
                              place_dilation=stack_place_dilation, common_sense_backprop=common_sense_backprop,
                              trial_reward='discounted' if discounted_reward else 'spot',
                              num_dilation=num_dilation)

        if 'row' in multi_task_snapshot_files:
            row_trainer = Trainer(method, push_rewards, future_reward_discount,
                              is_testing, multi_task_snapshot_files['row'], force_cpu,
                              goal_condition_len, place, pretrained, flops,
                              network=neural_network_name, common_sense=common_sense,
                              place_common_sense=place_common_sense, show_heightmap=show_heightmap,
                              place_dilation=place_dilation, common_sense_backprop=common_sense_backprop,
                              trial_reward='discounted' if discounted_reward else 'spot',
                              num_dilation=num_dilation)

        if 'unstack' in multi_task_snapshot_files:
            unstack_trainer = Trainer(method, push_rewards, future_reward_discount,
                              is_testing, multi_task_snapshot_files['unstack'], force_cpu,
                              goal_condition_len, place, pretrained, flops,
                              network=neural_network_name, common_sense=common_sense,
                              place_common_sense=place_common_sense, show_heightmap=show_heightmap,
                              place_dilation=place_dilation, common_sense_backprop=common_sense_backprop,
                              trial_reward='discounted' if discounted_reward else 'spot',
                              num_dilation=num_dilation)

        if 'vertical_square' in multi_task_snapshot_files:
            vertical_square_trainer = Trainer(method, push_rewards, future_reward_discount,
                              is_testing, multi_task_snapshot_files['vertical_square'], force_cpu,
                              goal_condition_len, place, pretrained, flops,
                              network=neural_network_name, common_sense=common_sense,
                              place_common_sense=place_common_sense, show_heightmap=show_heightmap,
                              place_dilation=place_dilation, common_sense_backprop=common_sense_backprop,
                              trial_reward='discounted' if discounted_reward else 'spot',
                              num_dilation=num_dilation)

        # set trainer reference to stack_trainer to get metadata (e.g. iteration)
        trainer = stack_trainer

    else:
        trainer = Trainer(method, push_rewards, future_reward_discount,
                          is_testing, snapshot_file, force_cpu,
                          goal_condition_len, place, pretrained, flops,
                          network=neural_network_name, common_sense=common_sense,
                          place_common_sense=place_common_sense, show_heightmap=show_heightmap,
                          place_dilation=place_dilation, common_sense_backprop=common_sense_backprop,
                          trial_reward='discounted' if discounted_reward else 'spot',
                          num_dilation=num_dilation, static_language_mask=static_language_mask, check_row = check_row, baseline_language_mask = baseline_language_mask) 

    if transfer_grasp_to_place:
        # Transfer pretrained grasp weights to the place action.
        trainer.model.transfer_grasp_to_place()

    # Initialize data logger
    title, dir_name = run_title(args)
    logger = Logger(continue_logging, logging_directory, args=args, dir_name=dir_name)
    logger.save_camera_info(robot.cam_intrinsics, robot.cam_pose, robot.cam_depth_scale) # Save camera intrinsics and pose
    logger.save_heightmap_info(workspace_limits, heightmap_resolution) # Save heightmap parameters

    # Quick hack for nonlocal memory between threads in Python 2
    # Most of these variables are saved to a json file during a run, and reloaded during resume.
    nonlocal_variables = {'executing_action': False,
                          'primitive_action': None,
                          'best_pix_ind': None,
                          'push_success': False,
                          'grasp_success': False,
                          'color_success': False,
                          'place_success': False,
                          'partial_stack_success': False,
                          'stack_height': 1,
                          'stack_rate': np.inf,
                          'trial_success_rate': np.inf,
                          'replay_iteration': 0,
                          'trial_complete': False,
                          'finalize_prev_trial_log': False,
                          'prev_stack_height': 1,
                          'save_state_this_iteration': False,
                          'example_actions_dict': None,
                          'language_metadata': {},
                          'color_partial_stack_success': None}

    # Ignore these nonlocal_variables when saving/loading and resuming a run.
    # They will always be initialized to their default values
    always_default_nonlocals = ['executing_action',
                                'primitive_action',
                                'save_state_this_iteration']

    # These variables handle pause and exit state. Also a quick hack for nonlocal memory.
    nonlocal_pause = {'pause': 0,
                      'pause_time_start': time.time(),
                      # setup KeyboardInterrupt signal handler for pausing
                      'original_sigint': signal.getsignal(signal.SIGINT),
                      'exit_called': False,
                      'process_actions_exit_called': False}

    # Find last executed iteration of pre-loaded log, and load execution info and RL variables
    if continue_logging:
        trainer.preload(logger.transitions_directory)

        # when resuming, load nonlocal_variables from previous point the the log was finalized in the run
        nonlocal_vars_filename = os.path.join(logger.base_directory, 'data', 'variables', 'nonlocal_vars_%d.json' % (trainer.iteration))
        if os.path.exists(nonlocal_vars_filename):
            with open(nonlocal_vars_filename, 'r') as f:
                nonlocals_to_load = json.load(f)

                # copy loaded values to nonlocals
                for k, v in nonlocals_to_load.items():
                    if k not in always_default_nonlocals:
                        if k in nonlocal_variables:  # ignore any entries in the saved data which aren't in nonlocal_variables
                            nonlocal_variables[k] = v
        else:
            print('WARNING: Missing /data/variables/nonlocal_vars_%d.json on resume. Default values initialized. Inconsistencies' % (trainer.iteration))

        num_trials = trainer.end_trial()

        # trainer.iteration += 1  # Begin next trial after loading

    else:
        num_trials = 0

    # Initialize variables for heuristic bootstrapping and exploration probability
    no_change_count = [2, 2] if not is_testing else [0, 0]
    explore_prob = 0.5 if not is_testing else 0.0

    if check_z_height:
        nonlocal_variables['stack_height'] = 0.0
        nonlocal_variables['prev_stack_height'] = 0.0
    best_stack_rate = np.inf
    prev_grasp_success = False

    if check_z_height:
        is_goal_conditioned = False
    else:
        is_goal_conditioned = grasp_color_task or place
    # Choose the first color block to grasp, or None if not running in goal conditioned mode
    # color_names = ["red","blue","green","yellow", "brown", "orange", "gray", "purple", "cyan", "pink"]
    color_names = robot.object_colors
    if num_obj is not None:
        nonlocal_variables['stack'] = StackSequence(num_obj - num_extra_obj, goal_num_obj=goal_num_obj, is_goal_conditioned_task=is_goal_conditioned, trial=num_trials, total_steps=trainer.iteration, color_names=color_names)
    else:
        nonlocal_variables['stack'] = StackSequence(20, is_goal_conditioned, trial=num_trials, total_steps=trainer.iteration, color_names=color_names)


    num_trials = 0
    if continue_logging:
        num_trials = int(max(trainer.trial_log)[0])
        nonlocal_variables['stack'].trial = num_trials + 1

    if place:
        # If we are stacking we actually skip to the second block which needs to go on the first
        nonlocal_variables['stack'].next()

    trainer_iteration_of_most_recent_model_reload = 0

    def pause(signum, frame):
        """This function is designated as the KeyboardInterrupt handler.

        It blocks execution in the main thread
        and pauses the process action thread. Execution will resume when this function returns,
        or will stop if ctrl-c is pressed 5 more times
        """
        # TODO(ahundt) come up with a cleaner pause resume API, maybe use an OpenCV interface.
        ctrl_c_stop_threshold = 3
        ctrl_c_kill_threshold = 5
        try:
            # restore the original signal handler as otherwise evil things will happen
            # in input when CTRL+C is pressed, and our signal handler is not re-entrant
            signal.signal(signal.SIGINT, nonlocal_pause['original_sigint'])
            time_since_last_ctrl_c = time.time() - nonlocal_pause['pause_time_start']
            if time_since_last_ctrl_c > 5:
                nonlocal_pause['pause'] = 0
                nonlocal_pause['pause_time_start'] = time.time()
                print('More than 5 seconds since last ctrl+c, Unpausing. '
                      'Press again within 5 seconds to pause.'
                      ' Ctrl+C Count: ' + str(nonlocal_pause['pause']))
            else:
                nonlocal_pause['pause'] += 1
                print('\n\nPaused, press ctrl-c 3 total times in less than 5 seconds '
                      'to stop the run cleanly, 5 to do a hard stop. '
                      'Pressing Ctrl + C after 5 seconds will resume.'
                      'Remember, you can always press Ctrl+\\ to hard kill the program at any time.'
                      ' Ctrl+C Count: ' + str(nonlocal_pause['pause']))

            if nonlocal_pause['pause'] >= ctrl_c_stop_threshold:
                print('Starting a clean exit, wait a few seconds for the robot and code to finish.')
                nonlocal_pause['exit_called'] = True
                # we need to unpause to complete the exit
                nonlocal_pause['pause'] = 0
            elif nonlocal_pause['pause'] >= ctrl_c_kill_threshold:
                print('Triggering a Hard exit now.')
                sys.exit(1)

        except KeyboardInterrupt:
            nonlocal_pause['pause'] += 1
        # restore the pause handler here
        signal.signal(signal.SIGINT, pause)

    # Set up the pause signal
    signal.signal(signal.SIGINT, pause)

    def set_nonlocal_success_variables_false():
        nonlocal_variables['push_success'] = False
        nonlocal_variables['grasp_success'] = False
        nonlocal_variables['place_success'] = False
        nonlocal_variables['grasp_color_success'] = False
        nonlocal_variables['place_color_success'] = False
        nonlocal_variables['partial_stack_success'] = False

    def check_stack_update_goal(place_check=False, top_idx=-1, depth_img=None, use_imitation=False, task_type=None):
        """ Check nonlocal_variables for a good stack and reset if it does not match the current goal.

        # Params

            place_check: If place check is True we should match the current stack goal,
                all other actions should match the stack check excluding the top goal block,
                which will not have been placed yet.
            top_idx: The index of blocks sorted from high to low which is expected to contain the top stack block.
                -1 will be the highest object in the scene, -2 will be the second highest in the scene, etc.
            use_imitation: If use_imitation is True, we are doing an imitation task
            task_type: Needs to be set if use_imitation is set (options are 'vertical_square', 'unstack')

        # Returns

        needed_to_reset boolean which is True if a reset was needed and False otherwise.
        """
        current_stack_goal = nonlocal_variables['stack'].current_sequence_progress()
        print(f"CURRENT STACK GOAL: {current_stack_goal}")
        # no need to reset by default
        needed_to_reset = False
        toppled = None
        if place_check:
            # Only reset while placing if the stack decreases in height!
            stack_shift = 1
        elif current_stack_goal is not None:
            # only the place check expects the current goal to be met
            current_stack_goal = current_stack_goal[:-1]
            stack_shift = 0

        # TODO(ahundt) BUG Figure out why a real stack of size 2 or 3 and a push which touches no blocks does not pass the stack_check and ends up a MISMATCH in need of reset. (update: may now be fixed, double check then delete when confirmed)
        if task_type is not None:
            # based on task type, call partial success function from robot, 'stack_height' represents task progress in these cases
            if task_type == 'vertical_square':
                # NOTE(adit98) explicitly set a lower distance threshold for vertical square
                stack_matches_goal, nonlocal_variables['stack_height'] = \
                        robot.vertical_square_partial_success(current_stack_goal,
                                check_z_height=check_z_height, stack_dist_thresh=0.04)
            elif task_type == 'unstack':
                # structure size (stack_height) is 1 + # of blocks removed from stack (1, 2, 3, 4)
                stack_matches_goal, nonlocal_variables['stack_height'] = \
                        robot.unstacking_partial_success(nonlocal_variables['prev_stack_height'])

            elif task_type == 'stack':
                # TODO(adit98) make sure we have path for real robot here
                stack_matches_goal, nonlocal_variables['stack_height'] = \
                        robot.check_stack(current_stack_goal, check_z_height=check_z_height,
                                top_idx=top_idx)

            elif task_type == 'row':
                # TODO(adit98) make sure we have path for real robot here
                stack_matches_goal, nonlocal_variables['stack_height'] = robot.check_row(current_stack_goal,
                        num_obj=num_obj, check_z_height=check_z_height, valid_depth_heightmap=valid_depth_heightmap, separation_threshold=separation_threshold, distance_threshold=distance_threshold,
                        prev_z_height=nonlocal_variables['prev_stack_height'])

            else:
                # TODO(adit98) trigger graceful exit here
                raise NotImplementedError(task_type)

        elif check_row:
            stack_matches_goal, nonlocal_variables['stack_height'] = robot.check_row(current_stack_goal,
                    num_obj=num_obj,distance_threshold=distance_threshold, separation_threshold=separation_threshold, check_z_height=check_z_height, valid_depth_heightmap=valid_depth_heightmap,
                    prev_z_height=nonlocal_variables['prev_stack_height'])
            # Note that for rows, a single action can make a row (horizontal stack) go from size 1 to a much larger number like 4.
            if not check_z_height and not static_language_mask:
                stack_matches_goal = nonlocal_variables['stack_height'] >= len(current_stack_goal)
        elif check_z_height:
            # decrease_threshold = None  # None means decrease_threshold will be disabled
            stack_matches_goal, nonlocal_variables['stack_height'], needed_to_reset = robot.check_z_height(depth_img, nonlocal_variables['prev_stack_height'])
            max_workspace_height = ' (see max_workspace_height printout above) '
            # TODO(ahundt) add a separate case for incremental height where continuous heights are converted back to height where 1.0 is the height of a block.
            # stack_matches_goal, nonlocal_variables['stack_height'] = robot.check_incremental_height(input_img, current_stack_goal)
        else:
            if static_language_mask:
                # current_stack_goal = nonlocal_variables['stack'].object_color_sequence[0:4]
                stack_matches_goal, nonlocal_variables['stack_height'], pred_stack_goal = robot.check_stack(current_stack_goal, top_idx=top_idx, goal_num_obj=goal_num_obj, return_inds=True)
            else:
                stack_matches_goal, nonlocal_variables['stack_height'] = robot.check_stack(current_stack_goal, top_idx=top_idx, goal_num_obj=goal_num_obj)

        nonlocal_variables['partial_stack_success'] = stack_matches_goal

        if not check_z_height:
            if nonlocal_variables['stack_height'] == 1:
                # A stack of size 1 does not meet the criteria for a partial stack success
                nonlocal_variables['partial_stack_success'] = False
                nonlocal_variables['stack_success'] = False
            max_workspace_height = len(current_stack_goal) - stack_shift

            # Has that stack gotten shorter than it was before? If so we need to reset
            needed_to_reset = nonlocal_variables['stack_height'] < max_workspace_height or nonlocal_variables['stack_height'] < nonlocal_variables['prev_stack_height']
            if task_type is not None and task_type == 'unstack':
                # also reset if we toppled while unstacking
                if nonlocal_variables['primitive_action'] == 'place':
                    # can't progress unstacking with place action, so this must have been a topple
                    toppled = nonlocal_variables['stack_height'] > nonlocal_variables['prev_stack_height']
                elif nonlocal_variables['primitive_action'] == 'grasp' and not nonlocal_variables['grasp_success']:
                    # can't progress legally if we have failed grasp
                    toppled = nonlocal_variables['stack_height'] > nonlocal_variables['prev_stack_height']
                else:
                    # caused decrease of more than 1 block during push/grasp
                    toppled = nonlocal_variables['stack_height'] > (nonlocal_variables['prev_stack_height'] + 1)

        insufficient_objs_in_scene = False
        # add check for num_obj in scene
        if is_sim and task_type in ['row', 'vertical_square', 'unstack']:
            if task_type == 'unstack' and is_testing:
                pass
            else:
                objs = robot.get_objects_in_scene()
                if len(objs) < nonlocal_variables['stack'].num_obj:
                    needed_to_reset = True
                    insufficient_objs_in_scene = True

        if not is_sim and grasp_color_task:
            if 'prev_color_heightmap' in nonlocal_variables['language_metadata'].keys() and \
                nonlocal_variables['language_metadata']['prev_color_heightmap'] is not None:
                success_code, comment = annotate_success_manually(nonlocal_variables['language_metadata']['command'],
                                                                nonlocal_variables['language_metadata']['prev_color_heightmap'],
                                                                nonlocal_variables['language_metadata']['next_color_heightmap'])

                nonlocal_variables['color_partial_stack_success'] = True if success_code == "success" else False


        print('check_stack() stack_height: ' + str(nonlocal_variables['stack_height']) + ' stack matches current goal: ' + str(stack_matches_goal) + ' partial_stack_success: ' +
                str(nonlocal_variables['partial_stack_success']) + ' Does the code think a reset is needed: ' + str(needed_to_reset) + ' Does the code think the stack toppled: ' +
                str(toppled))

        if static_language_mask and end_on_incorrect_order and not check_row:
            # check order, if it is the right length and bad then kill this trial
            # if there is a mistake further down in the stack than the top-most block, there's no way to recover
            # but if it's the top-most one, we can still unstack it, so don't kill the trial quite yet
            if len(pred_stack_goal) == 2:
                # if there are only 2 blocks and it places it on the wrong block, it can recover
                # pred_matches_goal_up_to_top = pred_stack_goal[-1] == current_stack_goal[-1]
                # just check that it isn't buried
                pred_matches_goal_up_to_top = pred_stack_goal[0] != current_stack_goal[1]
            else:
                pred_matches_goal_up_to_top = np.array_equal(pred_stack_goal, current_stack_goal)
            # only check for this if the predicted stack and goal stack are the same height
            lens_match = len(pred_stack_goal) >= len(current_stack_goal)
            # also check for toppling, but only if the previous stack was more than 2 block high
            was_over_one = nonlocal_variables['prev_stack_height'] > 1
            decreased_height = len(pred_stack_goal) < nonlocal_variables['prev_stack_height']
            # conditions for ending a trial:
            # either a mistake is made in the bottom of the stack, or the stack has toppled
            if (not pred_matches_goal_up_to_top and lens_match) or (was_over_one and decreased_height):
                print('ERROR: Stack is irreparably misordered or has toppled, trial is a failure. Ending...')
                needed_to_reset = True
                toppled = True
        else:
            pass
        # if place and needed_to_reset:
        # TODO(ahundt) BUG may reset push/grasp success too aggressively. If statement above and below for debugging, remove commented line after debugging complete
        if needed_to_reset or evaluate_random_objects or (toppled is not None and toppled):
            # we are two blocks off the goal, reset the scene.
            mismatch_str = 'main.py check_stack() DETECTED PROGRESS REVERSAL, mismatch between the goal height: ' + str(max_workspace_height) + ' and current workspace stack height: ' + str(nonlocal_variables['stack_height'])
            if insufficient_objs_in_scene:
                mismatch_str += ', INSUFFICIENT OBJECTS IN SCENE'
            if toppled is not None and toppled:
                mismatch_str += ', TOPPLED stack'
            if not disable_situation_removal or insufficient_objs_in_scene or (toppled is not None and toppled):
                mismatch_str += ', RESETTING the objects, goals, and action success to FALSE...'
                print(mismatch_str)
                # this reset is appropriate for stacking, but not checking rows
                get_and_save_images(robot, workspace_limits, heightmap_resolution, logger, trainer, '1')
                robot.reposition_objects()
                nonlocal_variables['stack'].reset_sequence()
                nonlocal_variables['stack'].next()
                # We needed to reset, so the stack must have been knocked over!
                # all rewards and success checks are False!
                set_nonlocal_success_variables_false()
                nonlocal_variables['trial_complete'] = True
                if check_row or (task_type is not None and ((task_type == 'row') or (task_type == 'vertical_square'))):
                    # on reset get the current row state
                    _, nonlocal_variables['stack_height'] = robot.check_row(current_stack_goal, num_obj=num_obj, check_z_height=check_z_height, valid_depth_heightmap=valid_depth_heightmap, separation_threshold=separation_threshold, distance_threshold=distance_threshold)
                    nonlocal_variables['prev_stack_height'] = copy.deepcopy(nonlocal_variables['stack_height'])
            else:
                print(mismatch_str)

        return needed_to_reset

    # Parallel thread to process network output and execute actions
    # -------------------------------------------------------------
    def process_actions():
        last_iteration_saved = -1  # used so the loop only saves one time while waiting
        action_count = 0
        grasp_count = 0
        successful_grasp_count = 0
        successful_color_grasp_count = 0
        place_count = 0
        place_rate = 0
        # short stacks of blocks
        partial_stack_count = 0
        partial_stack_rate = np.inf
        # all the blocks stacked
        stack_count = 0
        stack_rate = np.inf
        # will need to reset if something went wrong with stacking
        needed_to_reset = False
        grasp_str = ''
        successful_trial_count = int(np.max(trainer.trial_success_log)) if continue_logging and len(trainer.trial_success_log) > 0 else 0
        trial_rate = np.inf

        # when resuming a previous run, load variables saved from previous run
        if continue_logging:
            process_vars = None
            resume_var_values_path = os.path.join(logger.base_directory, 'data', 'variables','process_action_var_values_%d.json' % (trainer.iteration))
            if os.path.exists(resume_var_values_path):
                with open(resume_var_values_path, 'r') as f:
                    process_vars = json.load(f)
                # TODO(ahundt) the loop below should be a simpler way to do the same thing, but it doesn't seem to work
                # for k, v in process_vars.items():
                #     # initialize all the local variables based on the dictionary entries
                #     setattr(sys.modules[__name__], k, v)
                action_count = process_vars['action_count']
                grasp_count = process_vars['grasp_count']
                successful_grasp_count = process_vars['successful_grasp_count']
                successful_color_grasp_count = process_vars['successful_color_grasp_count']
                place_count = process_vars['place_count']
                place_rate = process_vars['place_rate']
                partial_stack_count = process_vars['partial_stack_count']
                partial_stack_rate = process_vars['partial_stack_rate']
                stack_count = process_vars['stack_count']
                stack_rate = process_vars['stack_rate']
                needed_to_reset = process_vars['needed_to_reset']
                grasp_str = process_vars['grasp_str']
                successful_trial_count = process_vars['successful_trial_count']
                trial_rate = process_vars['trial_rate']

            else:
                print("WARNING: Missing /data/variables/process_action_var_values_%d.json on resume. Default values initialized. May cause log inconsistencies" % (trainer.iteration))

        # NOTE(zhe) The loop continues to run until an exit signal appears. The loop doesn't run when not "executing action"
        while not nonlocal_pause['process_actions_exit_called']:
            if nonlocal_variables['executing_action']:
                action_count += 1
                # Determine whether grasping or pushing should be executed based on network predictions OR with demo
                if use_demo:
                    # initialize preds array
                    preds = []
                    # figure out primitive action (limited to grasp or place)
                    if nonlocal_variables['primitive_action'] != 'grasp':
                        # next action is grasp if we didn't grasp already
                        nonlocal_variables['primitive_action'] = 'grasp'

                        # get grasp predictions (since next action is grasp)
                        # fill the masked arrays and add to preds
                        if row_trainer is not None:
                            preds.append(grasp_feat_row)
                        else:
                            preds.append(None)

                        if stack_trainer is not None:
                            preds.append(grasp_feat_stack)
                        else:
                            preds.append(None)

                        if unstack_trainer is not None:
                            preds.append(grasp_feat_unstack)
                        else:
                            preds.append(None)

                        if vertical_square_trainer is not None:
                            preds.append(grasp_feat_vertical_square)
                        else:
                            preds.append(None)

                    else:
                        if nonlocal_variables['grasp_success']:
                            # if we had a successful grasp, set next action to place
                            nonlocal_variables['primitive_action'] = 'place'

                            # get place predictions (since next action is place)
                            # fill the masked arrays and add to preds
                            if row_trainer is not None:
                                preds.append(place_feat_row)
                            else:
                                preds.append(None)

                            if stack_trainer is not None:
                                preds.append(place_feat_stack)
                            else:
                                preds.append(None)

                            if unstack_trainer is not None:
                                preds.append(place_feat_unstack)
                            else:
                                preds.append(None)

                            if vertical_square_trainer is not None:
                                preds.append(place_feat_vertical_square)
                            else:
                                preds.append(None)

                        else:
                            # last grasp was unsuccessful, so we need to grasp again
                            nonlocal_variables['primitive_action'] = 'grasp'

                            # get grasp predictions (since next action is grasp)
                            # fill the masked arrays and add to preds
                            if row_trainer is not None:
                                preds.append(grasp_feat_row)
                            else:
                                preds.append(None)

                            if stack_trainer is not None:
                                preds.append(grasp_feat_stack)
                            else:
                                preds.append(None)

                            if unstack_trainer is not None:
                                preds.append(grasp_feat_unstack)
                            else:
                                preds.append(None)

                            if vertical_square_trainer is not None:
                                preds.append(grasp_feat_vertical_square)
                            else:
                                preds.append(None)

                    print("main.py: running demo.get_action for stack height",
                            nonlocal_variables['stack_height'], "and primitive action",
                            nonlocal_variables['primitive_action'])

                    # first check if nonlocal_variables['example_actions_dict'] is none
                    if nonlocal_variables['example_actions_dict'] is None:
                        nonlocal_variables['example_actions_dict'] = {}

                    # check if embeddings for demo for progress n and primitive action p_a already exists
                    task_progress = nonlocal_variables['stack_height']

                    # in unstacking task, max task_progress at 3 (final place has progress 4 technically but we could have reversal)
                    if task_type == 'unstack':
                        task_progress = min(3, task_progress)

                    action = nonlocal_variables['primitive_action']
                    if task_progress not in nonlocal_variables['example_actions_dict']:
                        nonlocal_variables['example_actions_dict'][task_progress] = {}
                    if action not in nonlocal_variables['example_actions_dict'][task_progress]:
                        nonlocal_variables['example_actions_dict'][task_progress][action] = {}

                    for ind, d in enumerate(example_demos):
                        # get action embeddings from example demo
                        if ind not in nonlocal_variables['example_actions_dict'][task_progress][action]:
                            demo_row_action, demo_stack_action, demo_unstack_action, demo_vertical_square_action, action_id = \
                                d.get_action(workspace_limits, action, task_progress, stack_trainer,
                                        row_trainer, unstack_trainer, vertical_square_trainer, use_hist=depth_channels_history,
                                        cycle_consistency=cycle_consistency)
                            nonlocal_variables['example_actions_dict'][task_progress][action][ind] = [demo_row_action,
                                    demo_stack_action, demo_unstack_action, demo_vertical_square_action]

                    print("main.py nonlocal_variables['executing_action']: got demo actions")

                else:
                    best_push_conf = np.ma.max(push_predictions)
                    best_grasp_conf = np.ma.max(grasp_predictions)
                    if place:
                        best_place_conf = np.ma.max(place_predictions)
                        print('Primitive confidence scores: %f (push), %f (grasp), %f (place)' % (best_push_conf, best_grasp_conf, best_place_conf))
                    else:
                        print('Primitive confidence scores: %f (push), %f (grasp)' % (best_push_conf, best_grasp_conf))

                # Exploitation (do best action) vs exploration (do random action)
                if is_testing:
                    explore_actions = False
                else:
                    explore_actions = np.random.uniform() < explore_prob
                    if explore_actions:
                        print('Strategy: explore (exploration probability: %f)' % (explore_prob))
                    else:
                        print('Strategy: exploit (exploration probability: %f)' % (explore_prob))

                if not use_demo:
                    # NOTE(zhe) Designate action type (grasp vs place) based on previous action.
                    # If we just did a successful grasp, we always need to place
                    if place and nonlocal_variables['primitive_action'] == 'grasp' and nonlocal_variables['grasp_success']:
                        nonlocal_variables['primitive_action'] = 'place'
                    else:
                        nonlocal_variables['primitive_action'] = 'grasp'

                # NOTE(zhe) Switch grasp to push if push has better score. NO PUSHING IN LANGUAGE MODEL.
                # determine if the network indicates we should do a push or a grasp
                # otherwise if we are exploring and not placing choose between push and grasp randomly
                if not grasp_only and not nonlocal_variables['primitive_action'] == 'place':
                    if is_testing and method == 'reactive':
                        if best_push_conf > 2 * best_grasp_conf:
                            nonlocal_variables['primitive_action'] = 'push'
                    else:
                        nonlocal_variables['primitive_action'] = 'grasp'

                    # determine if the network indicates we should do a push or a grasp
                    # otherwise if we are exploring and not placing choose between push and grasp randomly
                    if not grasp_only and not nonlocal_variables['primitive_action'] == 'place':
                        if is_testing and method == 'reactive':
                            if best_push_conf > 2 * best_grasp_conf:
                                nonlocal_variables['primitive_action'] = 'push'
                        else:
                            if best_push_conf > best_grasp_conf:
                                nonlocal_variables['primitive_action'] = 'push'
                        if explore_actions:
                            # explore the choices of push actions vs place actions
                            push_frequency_one_in_n = 5
                            nonlocal_variables['primitive_action'] = 'push' if np.random.randint(0, push_frequency_one_in_n) == 0 else 'grasp'

                trainer.is_exploit_log.append([0 if explore_actions else 1])
                logger.write_to_log('is-exploit', trainer.is_exploit_log)
                    # TODO(ahundt) remove if this has been working for a while, the trial log is now updated in the main thread rather than the robot control thread.
                    # trainer.trial_log.append([nonlocal_variables['stack'].trial])
                    # logger.write_to_log('trial', trainer.trial_log)

                # NOTE(zhe) Choose the argmax of the predictions, returns the coordinate of the max and the max value.
                if random_actions and explore_actions and not is_testing and np.random.uniform() < 0.5:
                    # Half the time we actually explore the full 2D action space
                    print('Strategy: explore ' + nonlocal_variables['primitive_action'] + '2D action space (exploration probability: %f)' % (explore_prob/2))
                    # explore a random action from the masked predictions
                    nonlocal_variables['best_pix_ind'], each_action_max_coordinate, predicted_value = action_space_explore_random(nonlocal_variables['primitive_action'], push_predictions, grasp_predictions, place_predictions)

                else:
                    if use_demo:
                        # get parameters of current action to do dict lookup
                        task_progress = nonlocal_variables['stack_height']
                        action = nonlocal_variables['primitive_action']

                        # clamp task_progress at 3 if task_type is unstack
                        if task_type == 'unstack':
                            task_progress = min(3, task_progress)

                        # TODO(adit98) implement
                        if cycle_consistency:
                            raise NotImplementedError("Need to test if below line works with different array shape!!!")
                        # rearrange example actions dictionary into (P, D) array where P is number of policies, D # of demos
                        example_actions = np.array([*nonlocal_variables['example_actions_dict'][task_progress][action].values()],
                                dtype=object).T

                        # construct preds and example_actions based on task type ("Leave One Out")
                        if task_type == 'row':
                            preds = preds[1:]
                            example_actions = example_actions[1:].tolist()
                        elif task_type == 'stack':
                            inds = [0, 2, 3]
                            preds = [preds[i] for i in inds]
                            example_actions = example_actions[inds].tolist()
                        elif task_type == 'unstack':
                            inds = [0, 1, 3]
                            preds = [preds[i] for i in inds]
                            example_actions = example_actions[inds].tolist()
                        elif task_type == 'vertical_square':
                            preds = preds[:3]
                            example_actions = example_actions[:3].tolist()
                        else:
                            # TODO(adit98) trigger graceful exit here
                            raise NotImplementedError(task_type + ' is not implemented.')

                        # select preds based on primitive action selected in demo (theta, y, x)
                        if cycle_consistency:
                            correspondences, nonlocal_variables['best_pix_ind'] = \
                                    compute_cc_dist(preds, example_actions, metric=primitive_distance_method)
                        else:
                            correspondences, nonlocal_variables['best_pix_ind'] = \
                                    compute_demo_dist(preds, example_actions, metric=primitive_distance_method)

                        predicted_value = correspondences[nonlocal_variables['best_pix_ind']]

                    else:
                        # Get pixel location and rotation with highest affordance prediction from the neural network algorithms (rotation, y, x)
                        nonlocal_variables['best_pix_ind'], each_action_max_coordinate, \
                            predicted_value = action_space_argmax(nonlocal_variables['primitive_action'],
                                    push_predictions, grasp_predictions, place_predictions)

                # If heuristic bootstrapping is enabled: if change has not been detected more than 2 times, execute heuristic algorithm to detect grasps/pushes
                # NOTE: typically not necessary and can reduce final performance.
                if heuristic_bootstrap and nonlocal_variables['primitive_action'] == 'push' and no_change_count[0] >= 2:
                    print('Change not detected for more than two pushes. Running heuristic pushing.')
                    nonlocal_variables['best_pix_ind'] = trainer.push_heuristic(valid_depth_heightmap)
                    no_change_count[0] = 0
                    predicted_value = push_predictions[nonlocal_variables['best_pix_ind']]
                    use_heuristic = True
                elif heuristic_bootstrap and nonlocal_variables['primitive_action'] == 'grasp' and no_change_count[1] >= 2:
                    print('Change not detected for more than two grasps. Running heuristic grasping.')
                    nonlocal_variables['best_pix_ind'] = trainer.grasp_heuristic(valid_depth_heightmap)
                    no_change_count[1] = 0
                    predicted_value = grasp_predictions[nonlocal_variables['best_pix_ind']]
                    use_heuristic = True
                else:
                    use_heuristic = False

                trainer.use_heuristic_log.append([1 if use_heuristic else 0])
                logger.write_to_log('use-heuristic', trainer.use_heuristic_log)

                # Save predicted confidence value
                trainer.predicted_value_log.append([predicted_value])
                logger.write_to_log('predicted-value', trainer.predicted_value_log)

                # NOTE(zhe) compute the best (rotAng, x, y)
                # Compute 3D position of pixel
                print('Action: %s at (%d, %d, %d)' % (nonlocal_variables['primitive_action'], nonlocal_variables['best_pix_ind'][0], nonlocal_variables['best_pix_ind'][1], nonlocal_variables['best_pix_ind'][2]))
                best_rotation_angle = np.deg2rad(nonlocal_variables['best_pix_ind'][0]*(360.0/trainer.model.num_rotations))
                best_pix_x = nonlocal_variables['best_pix_ind'][2]
                best_pix_y = nonlocal_variables['best_pix_ind'][1]

                # NOTE(zhe) calculate the action in terms of the robot pose
                # Adjust start position of all actions, and make sure z value is safe and not too low
                primitive_position, push_may_contact_something = robot.action_heightmap_coordinate_to_3d_robot_pose(best_pix_x, best_pix_y, nonlocal_variables['primitive_action'], valid_depth_heightmap)

                # Save executed primitive where [0, 1, 2] corresponds to [push, grasp, place]
                trainer.executed_action_log.append([ACTION_TO_ID[nonlocal_variables['primitive_action']], nonlocal_variables['best_pix_ind'][0], nonlocal_variables['best_pix_ind'][1], nonlocal_variables['best_pix_ind'][2]])
                logger.write_to_log('executed-action', trainer.executed_action_log)

                # TODO(adit98) set this up to work with demos
                # Visualize executed primitive, and affordances
                if save_visualizations:
                    # Q values are mostly 0 to 1 for pushing/grasping, mostly 0 to 4 for multi-step tasks with placing
                    scale_factor = 4 if place else 1
                    push_pred_vis = trainer.get_prediction_vis(push_predictions, color_heightmap, each_action_max_coordinate['push'], scale_factor=scale_factor)
                    logger.save_visualizations(trainer.iteration, push_pred_vis, 'push')
                    cv2.imwrite('visualization.push.png', push_pred_vis)
                    grasp_pred_vis = trainer.get_prediction_vis(grasp_predictions, color_heightmap, each_action_max_coordinate['grasp'], scale_factor=scale_factor)
                    logger.save_visualizations(trainer.iteration, grasp_pred_vis, 'grasp')
                    cv2.imwrite('visualization.grasp.png', grasp_pred_vis)
                    if place:
                        place_pred_vis = trainer.get_prediction_vis(place_predictions, color_heightmap, each_action_max_coordinate['place'], scale_factor=scale_factor)
                        logger.save_visualizations(trainer.iteration, place_pred_vis, 'place')
                        cv2.imwrite('visualization.place.png', place_pred_vis)

                # Initialize variables that influence reward
                set_nonlocal_success_variables_false()
                if place:
                    current_stack_goal = nonlocal_variables['stack'].current_sequence_progress()

                # NOTE(zhe) Execute the primitive action (grasp, push, or place)
                # Execute primitive
                if nonlocal_variables['primitive_action'] == 'push':
                    if skip_noncontact_actions and not push_may_contact_something:
                        # We are too high to contact anything, don't bother actually pushing.
                        # TODO(ahundt) also check for case where we are too high for the local gripper path
                        nonlocal_variables['push_success'] = False
                    else:
                        nonlocal_variables['push_success'] = robot.push(primitive_position, best_rotation_angle, workspace_limits)

                    # check if task is complete
                    if place and (check_row or task_type is not None):
                        # add for annotation process if we're not in the sim
                        try:
                            nonlocal_variables['language_metadata']['prev_color_heightmap'] = prev_color_heightmap
                            nonlocal_variables['language_metadata']['next_color_heightmap'] = color_heightmap
                        except NameError:
                            print(f"Threading issue: cannot set language metadata")
                            nonlocal_variables['language_metadata']['prev_color_heightmap'] = None
                            nonlocal_variables['language_metadata']['next_color_heightmap'] = None

                        needed_to_reset = check_stack_update_goal(use_imitation=use_demo, task_type=task_type)
                        if (not needed_to_reset and nonlocal_variables['partial_stack_success']):
                            # TODO(ahundt) HACK clean up this if check_row elif, it is pretty redundant and confusing
                            if check_row and nonlocal_variables['stack_height'] > nonlocal_variables['prev_stack_height']:
                                nonlocal_variables['stack'].next()
                                # TODO(ahundt) create a push to partial stack count separate from the place to partial stack count
                                partial_stack_count += 1
                                print('nonlocal_variables[stack].num_obj: ' + str(nonlocal_variables['stack'].num_obj))
                                # print('nonlocal_variables[stack].goal_num_obj: ' + str(nonlocal_variables['stack'].goal_num_obj))

                            elif nonlocal_variables['stack_height'] >= len(current_stack_goal):
                                nonlocal_variables['stack'].next()
                                # TODO(ahundt) create a push to partial stack count separate from the place to partial stack count
                                partial_stack_count += 1
                            next_stack_goal = nonlocal_variables['stack'].current_sequence_progress()

                            if nonlocal_variables['stack_height'] >= nonlocal_variables['stack'].goal_num_obj:
                                if check_row and static_language_mask:
                                    secondary_check = nonlocal_variables['partial_stack_success']
                                else: 
                                    secondary_check = True
                                if secondary_check: 
                                    print('TRIAL ' + str(nonlocal_variables['stack'].trial) + ' SUCCESS!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
                                    if is_testing:
                                        # we are in testing mode which is frequently recorded,
                                        # so sleep for 10 seconds to show off our results!
                                        time.sleep(10)
                                    nonlocal_variables['stack_success'] = True
                                    stack_count += 1
                                    # full stack complete! reset the scene
                                    successful_trial_count += 1
                                    get_and_save_images(robot, workspace_limits, heightmap_resolution, logger, trainer, '1')
                                    robot.reposition_objects()
                                    if len(next_stack_goal) > 1:
                                        # if multiple parts of a row are completed in one action, we need to reset the trial counter.
                                        nonlocal_variables['stack'].reset_sequence()
                                    # goal is 2 blocks in a row
                                    nonlocal_variables['stack'].next()
                                    nonlocal_variables['trial_complete'] = True

                    elif not place or not needed_to_reset:
                        print('Push motion successful (no crash, need not move blocks): %r' % (nonlocal_variables['push_success']))

                    #TODO(hkwon214) Get image after executing push action. save also? better place to put?
                    valid_depth_heightmap_push, color_heightmap_push, depth_heightmap_push, color_img_push, depth_img_push = get_and_save_images(robot,
                            workspace_limits, heightmap_resolution, logger, trainer, '2')

                    # this check is important for original row making and stacking modes, don't need if task type is set
                    if place and task_type is None:
                        # Check if the push caused a topple, size shift zero because
                        # place operations expect increased height,
                        # while push expects constant height.
                        # add for annotation process if we're not in the sim
                        try:
                            nonlocal_variables['language_metadata']['prev_color_heightmap'] = prev_color_heightmap
                            nonlocal_variables['language_metadata']['next_color_heightmap'] = color_heightmap
                        except NameError:
                            print(f"Threading issue: cannot set language metadata")
                            nonlocal_variables['language_metadata']['prev_color_heightmap'] = None
                            nonlocal_variables['language_metadata']['next_color_heightmap'] = None
                        needed_to_reset = check_stack_update_goal(depth_img=valid_depth_heightmap_push,
                                use_imitation=use_demo, task_type=task_type)


                elif nonlocal_variables['primitive_action'] == 'grasp':
                    grasp_count += 1
                    # TODO(ahundt) this probably will cause threading conflicts, add a mutex
                    if nonlocal_variables['stack'].object_color_index is not None and (grasp_color_task or static_language_mask):
                        grasp_color_name = robot.color_names[int(nonlocal_variables['stack'].object_color_index)]
                        #grasp_color_name = robot.color_names[int(nonlocal_variables['stack'].object_color_index)]
                        print('Attempt to grasp color: ' + grasp_color_name)

                    if(skip_noncontact_actions and (np.isnan(valid_depth_heightmap[best_pix_y][best_pix_x]) or
                            valid_depth_heightmap[best_pix_y][best_pix_x] < 0.01)):
                        # Skip noncontact actions we don't bother actually grasping if there is nothing there to grasp
                        nonlocal_variables['grasp_success'], nonlocal_variables['grasp_color_success'] = False, False
                        print('Grasp action failure, heuristics determined grasp would not contact anything.')
                    else:
                        if static_language_mask:
                            #grasp_color_name = robot.color_names[int(nonlocal_variables['stack'].object_color_index)]
                            stack_idx = nonlocal_variables['stack'].object_color_index
                            grasp_color_idx = nonlocal_variables['stack'].object_color_sequence[stack_idx]
                            grasp_color_name = robot.color_names[grasp_color_idx]
                            object_color = grasp_color_name
                        else:
                            object_color = nonlocal_variables['stack'].object_color_index
                        nonlocal_variables['grasp_success'], nonlocal_variables['grasp_color_success'] = robot.grasp(primitive_position, best_rotation_angle, object_color=object_color) 
                    print('Grasp successful: %r' % (nonlocal_variables['grasp_success']))
                    # Get image after executing grasp action.
                    # TODO(ahundt) save also? better place to put?
                    valid_depth_heightmap_grasp, color_heightmap_grasp, depth_heightmap_grasp, color_img_grasp, depth_img_grasp = get_and_save_images(robot, workspace_limits, heightmap_resolution, logger, trainer, '2')
                    if place:
                        # when we are stacking we must also check the stack in case we caused it to topple
                        top_idx = -1
                        if nonlocal_variables['grasp_success']:
                            # we will need to check the second from top block for the stack
                            top_idx = -2
                        # check if a failed grasp led to a topple, or if the top block was grasped
                        # TODO(ahundt) in check_stack() support the check after a specific grasp in case of successful grasp topple. Perhaps allow the top block to be specified?
                        print('running check_stack_update_goal for grasp action')
                        nonlocal_variables['language_metadata']['prev_color_heightmap'] = None
                        needed_to_reset = check_stack_update_goal(top_idx=top_idx,
                                depth_img=valid_depth_heightmap_grasp, task_type=task_type, use_imitation=use_demo)

                    if nonlocal_variables['grasp_success']:
                        # robot.restart_sim()
                        successful_grasp_count += 1
                        if grasp_color_task or static_language_mask:
                            if nonlocal_variables['grasp_color_success']:
                                successful_color_grasp_count += 1
                            if not place and not static_language_mask:
                                # reposition the objects if we aren't also attempting to place correctly.
                                robot.reposition_objects()
                                nonlocal_variables['trial_complete'] = True
                            print('Successful color-specific grasp: %r intended target color: %s' % (nonlocal_variables['grasp_color_success'], grasp_color_name))

                    grasp_rate = float(successful_grasp_count) / float(grasp_count)
                    color_grasp_rate = float(successful_color_grasp_count) / float(grasp_count)
                    grasp_str = 'Grasp Count: %r, grasp success rate: %r' % (grasp_count, grasp_rate)
                    if grasp_color_task:
                        grasp_str += ' color success rate: %r' % (color_grasp_rate)
                    if not place:
                        print(grasp_str)

                elif nonlocal_variables['primitive_action'] == 'place':
                    # TODO(adit98) set over_block when calling demo.get_action()
                    # NOTE we always assume we are placing over a block for vertical square and stacking
                    if task_type is not None and ((task_type == 'unstack') or (task_type == 'row')):
                        over_block = False
                    else:
                        over_block = not check_row

                    place_count += 1
                    nonlocal_variables['place_success'] = robot.place(primitive_position, best_rotation_angle, over_block=over_block, intended_position=nonlocal_variables['intended_position'])

                    # Get image after executing place action.
                    # TODO(ahundt) save also? better place to put?
                    valid_depth_heightmap_place, color_heightmap_place, depth_heightmap_place, color_img_place, depth_img_place = get_and_save_images(robot, workspace_limits,
                            heightmap_resolution, logger, trainer, '2')

                    # add for annotation process if we're not in the sim
                    try:
                        nonlocal_variables['language_metadata']['prev_color_heightmap'] = prev_color_heightmap
                        nonlocal_variables['language_metadata']['next_color_heightmap'] = color_heightmap
                    except NameError:
                        print(f"Threading issue: cannot set language metadata")
                        nonlocal_variables['language_metadata']['prev_color_heightmap'] = None
                        nonlocal_variables['language_metadata']['next_color_heightmap'] = None

                    needed_to_reset = check_stack_update_goal(place_check=True, depth_img=valid_depth_heightmap_place,
                            task_type=task_type, use_imitation=use_demo)

                    # NOTE(adit98) sometimes place is unsuccessful but can lead to task progress when task type is set, so added check for this
                    if (not needed_to_reset and
                            ((nonlocal_variables['place_success'] and nonlocal_variables['partial_stack_success']) or
                             (check_row and not check_z_height and nonlocal_variables['stack_height'] >= len(current_stack_goal)) or
                             (task_type is not None and nonlocal_variables['partial_stack_success']))):
                        partial_stack_count += 1
                        # Only increment our progress checks if we've surpassed the current goal
                        # TODO(ahundt) check for a logic error between rows and stack modes due to if height ... next() check.
                        if not check_z_height and nonlocal_variables['stack_height'] >= len(current_stack_goal):
                            nonlocal_variables['stack'].next()
                        next_stack_goal = nonlocal_variables['stack'].current_sequence_progress()

                        if ((check_z_height and nonlocal_variables['stack_height'] > check_z_height_goal) or
                                (not check_z_height and (len(next_stack_goal) < len(current_stack_goal) or nonlocal_variables['stack_height'] >= nonlocal_variables['stack'].goal_num_obj))):

                            if check_row and static_language_mask:
                                # make sure row actually matches goal, not just any row of 4
                                secondary_check = nonlocal_variables['partial_stack_success']
                            else: 
                                secondary_check = True

                            if secondary_check: 
                                print('TRIAL ' + str(nonlocal_variables['stack'].trial) + ' SUCCESS!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')
                                if is_testing:
                                    # we are in testing mode which is frequently recorded,
                                    # so sleep for 10 seconds to show off our results!
                                    time.sleep(10)
                                nonlocal_variables['stack_success'] = True
                                nonlocal_variables['place_success'] = True
                                nonlocal_variables['partial_stack_success'] = True
                                stack_count += 1
                                # full stack complete! reset the scene
                                successful_trial_count += 1
                                get_and_save_images(robot, workspace_limits, heightmap_resolution, logger, trainer, '1')
                                robot.reposition_objects()
                                # We don't need to reset here because the algorithm already reset itself
                                if static_language_mask:
                                    nonlocal_variables['stack'].reset_sequence()
                                nonlocal_variables['stack'].next()
                                nonlocal_variables['trial_complete'] = True
                    # TODO(ahundt) perhaps reposition objects every time a partial stack step fails (partial_stack_success == false) to avoid weird states?

                # NOTE(zhe) Update logs with success/failures in the trainer object
                trainer.grasp_success_log.append([int(nonlocal_variables['grasp_success'])])
                if grasp_color_task:
                    trainer.color_success_log.append([int(nonlocal_variables['color_success'])])
                if static_language_mask:
                    trainer.grasp_color_success_log.append([int(nonlocal_variables['grasp_color_success'])])
                if place:
                    # place trainer logs are updated in process_actions()
                    trainer.stack_height_log.append([float(nonlocal_variables['stack_height'])])
                    print("main.py() process_actions: place_success:", nonlocal_variables['place_success'])
                    print("main.py() process_actions: partial_stack_success:", nonlocal_variables['partial_stack_success'])
                    trainer.partial_stack_success_log.append([int(nonlocal_variables['partial_stack_success'])])
                    trainer.place_success_log.append([int(nonlocal_variables['place_success'])])
                    trainer.trial_success_log.append([int(successful_trial_count)])

                    if partial_stack_count > 0 and place_count > 0:
                        partial_stack_rate = float(action_count)/float(partial_stack_count)
                        place_rate = float(partial_stack_count)/float(place_count)
                    if stack_count > 0:
                        stack_rate = float(action_count)/float(stack_count)
                        nonlocal_variables['stack_rate'] = stack_rate
                        trial_rate = float(successful_trial_count)/float(nonlocal_variables['stack'].trial)
                        nonlocal_variables['trial_success_rate'] = trial_rate
                    print('STACK:  trial: ' + str(nonlocal_variables['stack'].trial) + ' actions/partial: ' + str(partial_stack_rate) +
                            '  actions/full stack: ' + str(stack_rate) +
                            ' (lower is better)  ' + grasp_str + ' place_on_stack_rate: ' + str(place_rate) + ' place_attempts: ' + str(place_count) +
                            '  partial_stack_successes: ' + str(partial_stack_count) +
                            '  stack_successes: ' + str(stack_count) + ' trial_success_rate: ' + str(trial_rate) + ' stack goal: ' + str(current_stack_goal) +
                            ' current_height: ' + str(nonlocal_variables['stack_height']))

                # NOTE(zhe) process action loop now stalls after setting executing_action to False
                nonlocal_variables['executing_action'] = False

            # NOTE(zhe) this is like a checkpoint to save the thread's variable when the log and model are saved.
            # save this thread's variables every time the log and model are saved
            if nonlocal_variables['finalize_prev_trial_log']:
                # finalize_prev_trial_log gets set to false before all data is saved in the rest of the loop.
                # This flag is used to save variables in the other thread without
                # breaking anything by messing with finalize_prev_trial_log
                nonlocal_variables['save_state_this_iteration'] = True

                if last_iteration_saved != trainer.iteration: # checks if it already saved this iteration
                    last_iteration_saved = trainer.iteration

                    # create dict of all variables and save a json file
                    process_vars = {}
                    process_vars['action_count'] = action_count
                    process_vars['grasp_count'] = grasp_count
                    process_vars['successful_grasp_count'] = successful_grasp_count
                    process_vars['successful_color_grasp_count'] = successful_color_grasp_count
                    process_vars['place_count'] = place_count
                    process_vars['place_rate'] = place_rate
                    process_vars['partial_stack_count'] = partial_stack_count
                    process_vars['partial_stack_rate'] = partial_stack_rate
                    process_vars['stack_count'] = stack_count
                    process_vars['stack_rate'] = stack_rate
                    process_vars['needed_to_reset'] = needed_to_reset
                    process_vars['grasp_str'] = grasp_str
                    process_vars['successful_trial_count'] = successful_trial_count
                    process_vars['trial_rate'] = trial_rate
                    # save process vars into nonlocal variables so they can be used to inform future training
                    nonlocal_variables['prev_process_vars'] = process_vars
                    save_location = os.path.join(logger.base_directory, 'data', 'variables')
                    if not os.path.exists(save_location):
                        os.mkdir(save_location)
                    with open(os.path.join(save_location, 'process_action_var_values_%d.json' % (trainer.iteration)), 'w') as f:
                            json.dump(process_vars, f, cls=utils.NumpyEncoder, sort_keys=True)

            # TODO(ahundt) this should really be using proper threading and locking algorithms
            time.sleep(0.01)

    # helper function to update variables for trial ending
    def end_trial():
        # Check if the other thread ended the trial and reset the important values
        no_change_count = [0, 0]
        num_trials = trainer.end_trial()
        if nonlocal_variables['stack'] is not None:
            # TODO(ahundt) HACK to work around BUG where the stack sequence class currently over-counts the trials due to double resets at the end of one trial.
            nonlocal_variables['stack'].trial = num_trials
        logger.write_to_log('clearance', trainer.clearance_log)
        # we've recorded the data to mark this trial as complete
        nonlocal_variables['trial_complete'] = False
        # we're still not totally done, we still need to finilaize the log for the trial
        nonlocal_variables['finalize_prev_trial_log'] = True
        if is_testing:
            # Do special testing mode update steps
            # If at end of test run, re-load original weights (before test run)
            if use_demo:
                if 'stack' in multi_task_snapshot_files:
                    stack_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['stack']))
                if 'row' in multi_task_snapshot_files:
                    row_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['row']))
                if 'unstack' in multi_task_snapshot_files:
                    unstack_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['unstack']))
                if 'vertical_square' in multi_task_snapshot_files:
                    vertical_square_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['vertical_square']))
            else:
                print(f"Snapshot file: {snapshot_file}")
                try:
                    trainer.model.load_state_dict(torch.load(snapshot_file))
                except FileNotFoundError:
                    pass

            if test_preset_cases:
                case_file = preset_files[min(len(preset_files)-1, int(float(num_trials+1)/float(trials_per_case)))]
                # case_file = preset_files[min(len(preset_files)-1, int(float(num_trials-1)/float(trials_per_case)))]
                # load the current preset case, incrementing as trials are cleared
                print('loading case file: ' + str(case_file))
                robot.load_preset_case(case_file)
            if not place and num_trials >= max_test_trials:
                nonlocal_pause['exit_called'] = True  # Exit after training thread (backprop and saving labels)

        return no_change_count

    def sim_problem_end_trial(num_problems_detected=0):
        ''' Call when trials must be ended due to an exceptional situation from the main training loop.
        Currently it is called when the simulator is unresponsive for more than 60 seconds and when training progress drops or jumps in an implausible manner,
        which often means the simulator entered a physically impossible state.
        The existing code should already call the regular end_trial() function when the next iteration starts.
        '''
        get_and_save_images(robot, workspace_limits, heightmap_resolution, logger, trainer, '1')
        robot.check_sim()
        if not robot.reposition_objects():
            # This can happen if objects are in impossible positions (NaN),
            # so set the variable to immediately and completely restart
            # the simulation below.
            num_problems_detected += 3
        nonlocal_variables['trial_complete'] = True

        if place:
            nonlocal_variables['stack'].reset_sequence()
            nonlocal_variables['stack'].next()
        if check_z_height:
            # Zero out the height because the trial is done.
            # Note these lines must normally be after the
            # logging of these variables is complete,
            # but this is a special (hopefully rare) recovery scenario.
            nonlocal_variables['stack_height'] = 0.0
            nonlocal_variables['prev_stack_height'] = 0.0
        else:
            nonlocal_variables['stack_height'] = 1.0
            nonlocal_variables['prev_stack_height'] = 1.0
        num_problems_detected += 1
        if num_problems_detected > 2:
            # Try more drastic recovery methods the second time around
            robot.restart_sim(connect=True)
            robot.add_objects()
        return num_problems_detected

    action_thread = threading.Thread(target=process_actions)
    action_thread.daemon = True
    action_thread.start()
    nonlocal_pause['exit_called'] = False
    # -------------------------------------------------------------
    # -------------------------------------------------------------
    prev_primitive_action = None
    prev_reward_value = None
    if test_preset_cases:
        # save out the order we will visit the preset files for a sanity check
        print('preset files order: ' + str(preset_files))
        np.savetxt(os.path.join(logger.transitions_directory, 'preset-case-files.log.txt'), preset_files, delimiter=' ', fmt='%s')
    if show_preset_cases_then_exit and test_preset_cases:
        # Just a quick temporary mode for viewing the saved preset test cases
        for case_file in preset_files:
            # load the current preset case, incrementing as trials are cleared
            print('loading case file: ' + str(case_file))
            robot.load_preset_case(case_file)
            robot.restart_sim()
            robot.add_objects()
            time.sleep(3)
        exit_called = True
        robot.shutdown()
        return

    if use_demo:
        example_demos = load_all_demos(demo_path=args.demo_path, check_z_height=check_z_height,
                task_type=args.task_type)

    num_trials = trainer.num_trials()
    do_continue = False
    best_dict = {}
    prev_best_dict = {}
    backprop_enabled = None  # will be a dictionary indicating if specific actions have backprop enabled

    # Instantiate the DatasetReader in order to provide language commands during training
    language_data = None
    blockMover = None
    language_model = None
    if static_language_mask:
        if is_sim and not is_bisk:
            # define a dataset reader closure so that we can pass individual objects at a time
            # (elias) turn these into command-line args
            dataset_reader_fxn = lambda x: GoodRobotDatasetReader(path_or_obj=x,
                                                    split_type="none",
                                                    task_type="rows",
                                                    augment_by_flipping=False,
                                                    augment_language = False,
                                                    augment_by_rotating=False,
                                                    leave_out_color=None,
                                                    batch_size=1,
                                                    max_seq_length=40,
                                                    resolution = 64,
                                                    is_bert = True,
                                                    overfit=False)

        # if dealing with Bisk data, do as before
        if is_sim and is_bisk:
            dataset_reader = DatasetReader(args.train_language_inputs,
                                        None,
                                        None,
                                        batch_by_line=True,
                                        batch_size = 1)
            print(f"Reading data from {args.train_language_inputs}, this may take a few minutes.")
            language_data = dataset_reader.data['train']
            # END IF

        if is_sim and is_bisk:
            blockMover = BlockSetter(num_obj, [0.15, 0.0, 0.0], robot, side_len=0.027)

        language_model = utils.load_language_model_from_config(configYamlPath=language_model_config, weightsPath=language_model_weights)
        language_model.eval()

    # Start main training/testing loop, max_iter == 0 or -1 goes forever.
    # NOTE(zhe) We may not be able to simply use the common sense filter for placing since we need to place in "empty space" sometimes.
    while max_iter < 0 or trainer.iteration < max_iter:
        # end trial if signaled by process_actions thread
        if nonlocal_variables['trial_complete']:
            no_change_count = end_trial()
            num_trials = trainer.num_trials()

        print('\n%s iteration: %d' % ('Testing' if is_testing else 'Training', trainer.iteration))
        iteration_time_0 = time.time()
        # Record the current trial number
        trainer.trial_log.append([trainer.num_trials()])

        # Get latest RGB-D image
        valid_depth_heightmap, color_heightmap, depth_heightmap, color_img, depth_img = get_and_save_images(
            robot, workspace_limits, heightmap_resolution, logger, trainer, depth_channels_history=depth_channels_history)

        # Make sure simulation is still stable (if not, reset simulation)
        if is_sim:
            robot.check_sim()

        # If using the language map, get the command sentence and set up the scene
        nonlocal_variables['intended_position'] = None
        if static_language_mask:
            # TODO(elias) ensure prev_primitive_action is thread-safe
            # TODO(elias) what about unsuccessful grasp actions that move the block
            if is_sim and (prev_primitive_action == "place" or prev_primitive_action is None):
                json_data = sim_object_state_to_json(robot)
                #print(nonlocal_variables['stack'].object_color_sequence, nonlocal_variables['stack'].object_color_index)
                pair = Pair.from_main_idxs(color_heightmap,
                                           valid_depth_heightmap,
                                           json_data,
                                           nonlocal_variables['stack'],
                                           is_row = check_row)

                # batchify a single example
                language_data_instance = dataset_reader_fxn(pair).data['train'][0]

            # only set up the scene if working with Bisk (2018) data
            elif is_sim and is_bisk:
                # Update nonlocal variables with bisk goal here
                nonlocal_variables['intended_position'] = [language_data_instance.next_positions[0], language_data_instance.next_rotations[0]]
                # RESET THE SCENE to match the "previous" state if we are in the simulator
                # NOTE(zhe) This currently requires the dynamics to be turned off. TODO(zhe) Allow dynamics in future.
                pos = language_data_instance.previous_positions[0]
                rot = language_data_instance.previous_rotations[0]
                blockMover.load_setup(pos, rot)
            else:
                pass
        else:
            language_data_instance = None

        # Reset simulation or pause real-world training if table is empty
        stuff_count = np.zeros(valid_depth_heightmap.shape[:2])
        stuff_count[valid_depth_heightmap[:, :, 0] > 0.02] = 1
        if show_heightmap:
            # show the heightmap
            f = plt.figure()
            f.suptitle(str(trainer.iteration))
            f.add_subplot(1,3, 1)
            plt.imshow(valid_depth_heightmap)
            f.add_subplot(1,3, 2)
            # f.add_subplot(1,2, 1)
            if robot.background_heightmap is not None:
                plt.imshow(robot.background_heightmap)
                f.add_subplot(1,3, 3)
            plt.imshow(stuff_count)
            plt.show(block=True)
        stuff_sum = np.sum(stuff_count)
        empty_threshold = 300
        if is_sim and is_testing:
            empty_threshold = 10
        if check_row:
            # here we are assuming blocks for check_row, if any block leaves the scene then we can't succeed.
            # TODO(ahundt) Ideally volume should also be accounted for, a perfect stack is about the area of 1 block, and the scene might start with a stack.
            num_empty_obj = num_obj
            if is_testing:
                num_empty_obj -= 1
            empty_threshold = 300 * (num_empty_obj + num_extra_obj)
        print('Current count of pixels with stuff: ' + str(stuff_sum) + ' threshold below which the scene is considered empty: ' + str(empty_threshold))

        # NOTE(zhe) The pushing & grasping only task is to pick up items and teleport them to a bin outside of the workspace.
        if not place and stuff_sum < empty_threshold:
            print('Pushing And Grasping Trial Successful!')
            num_trials = trainer.num_trials()
            pg_trial_success_count = np.max(trainer.trial_success_log, initial=0)
            for i in range(len(trainer.trial_success_log), trainer.iteration):
                # previous trials were ended early
                trainer.trial_success_log.append([int(pg_trial_success_count)])
            trainer.trial_success_log.append([int(pg_trial_success_count + 1)])
            nonlocal_variables['trial_complete'] = True

        # calculate number of actions/iterations in this trial
        actions_in_trial = trainer.get_final_trial_action_count()

        # NOTE(zhe) This is for the stacking task (BUG But it runs for place/grasp as well?), error is thrown when not enough objects are in the workspace or no change in workspace
        if stuff_sum < empty_threshold or ((is_testing or is_sim) and not prev_grasp_success and no_change_count[0] + no_change_count[1] > 10) or (actions_in_trial >= max_trial_actions):
            if is_sim:
                print('There have not been changes to the objects for for a long time [push, grasp]: ' + str(no_change_count) +
                      ', the trial took ' + str(actions_in_trial) + ' vs a limit of ' + str(max_trial_actions) + ' actions, ' +
                      'or there are not enough objects in view (value: %d)! Repositioning objects.' % (stuff_sum))
                robot.restart_sim()
                robot.add_objects()
                if is_testing:  # If at end of test run, re-load original weights (before test run)
                    if use_demo:
                        if 'stack' in multi_task_snapshot_files:
                            stack_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['stack']))
                        if 'row' in multi_task_snapshot_files:
                            row_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['row']))
                        if 'unstack' in multi_task_snapshot_files:
                            unstack_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['unstack']))
                        if 'vertical_square' in multi_task_snapshot_files:
                            vertical_square_trainer.model.load_state_dict(torch.load(multi_task_snapshot_files['vertical_square']))

                    else:
                        try:
                            trainer.model.load_state_dict(torch.load(snapshot_file))
                        except FileNotFoundError:
                            pass
                if place:
                    set_nonlocal_success_variables_false()
                    nonlocal_variables['stack'].reset_sequence()
                    nonlocal_variables['stack'].next()
            else:
                # print('Not enough stuff on the table (value: %d)! Pausing for 30 seconds.' % (np.sum(stuff_count)))
                # time.sleep(30)
                print('Not enough stuff on the table (value: %d)! Moving objects to reset the real robot scene...' % (stuff_sum))
                robot.restart_real()

            # fill trial success log with 0s if we had a no activity-caused reset, do for both real and sim
            if not place:
                pg_trial_success_count = np.max(trainer.trial_success_log, initial=0)
                for i in range(len(trainer.trial_success_log), trainer.iteration + 1):
                    # previous trials were ended early
                    trainer.trial_success_log.append([int(pg_trial_success_count)])

            # If the scene started empty, we are just setting up
            # trial 0 with a reset, so no trials have been completed.
            if trainer.iteration > 0:
                # All other nonzero trials should be considered over,
                # so mark the trial as complete and move on to the next one.
                # NOTE(zhe) Continue to next trial after error or success determined above.
                nonlocal_variables['trial_complete'] = True
                # TODO(ahundt) might this continue statement increment trainer.iteration, break accurate indexing of the clearance log into the label, reward, and image logs?
                do_continue = True
                # continue

        # TODO(adit98) figure out if we need to disable experience replay for boundary situations e.g. successful action & trial reset for trial action limit, successful action & objects leaving scene
        # end trial if scene is empty or no changes
        if nonlocal_variables['trial_complete']:
            # Check if the other thread ended the trial and reset the important values
            no_change_count = end_trial()
            num_trials = trainer.num_trials()

            if do_continue:
                do_continue = False
                continue

            # TODO(ahundt) update experience replay trial rewards

        # check for possible bugs in the code
        if len(trainer.reward_value_log) < trainer.iteration - 2:
            # check for progress counting inconsistencies
            print('WARNING POSSIBLE CRITICAL ERROR DETECTED: log data index and trainer.iteration out of sync!!! Experience Replay may break! '
                  'Check code for errors in indexes, continue statements etc.')
        if place and nonlocal_variables['stack'].trial != num_trials:
            # check that num trials is always the current trial number
            print('WARNING variable mismatch num_trials + 1: ' + str(num_trials + 1) + ' nonlocal_variables[stack].trial: ' + str(nonlocal_variables['stack'].trial))

        # check if we have completed the current test
        if is_testing and place and nonlocal_variables['stack'].trial > max_test_trials:
            # If we are doing a fixed number of test trials, end the run the next time around.
            nonlocal_pause['exit_called'] = True

        if not nonlocal_pause['exit_called']:
            # NOTE(zhe) setting the ordered stack goal.
            # Run forward pass with network to get affordances
            if nonlocal_variables['stack'].is_goal_conditioned_task and grasp_color_task:
                goal_condition = np.array([nonlocal_variables['stack'].current_one_hot()])
            else:
                goal_condition = None

            # here, we run forward pass on imitation video
            if args.use_demo:
                # run forward pass, keep action features and get softmax predictions
                # stack features
                if stack_trainer is not None:
                    push_feat_stack, grasp_feat_stack, place_feat_stack, push_predictions_stack, \
                            grasp_predictions_stack, place_predictions_stack, _, _ = \
                            stack_trainer.forward(color_heightmap, valid_depth_heightmap,
                                    is_volatile=True, keep_action_feat=True, demo_mask=True)
                    print("main.py nonlocal_pause['exit_called'] got stack features")

                    # fill masked arrays of features
                    push_feat_stack, grasp_feat_stack, place_feat_stack = \
                            push_feat_stack.filled(0.0), grasp_feat_stack.filled(0.0), place_feat_stack.filled(0.0)

                    # TODO(adit98) may need to refactor, for now just store stack predictions
                    push_predictions, grasp_predictions, place_predictions = \
                            push_predictions_stack, grasp_predictions_stack, place_predictions_stack

                if row_trainer is not None:
                    # row features
                    push_feat_row, grasp_feat_row, place_feat_row, push_predictions_row, \
                            grasp_predictions_row, place_predictions_row, _, _ = \
                            row_trainer.forward(color_heightmap, valid_depth_heightmap,
                                    is_volatile=True, keep_action_feat=True, demo_mask=True)
                    print("main.py nonlocal_pause['exit_called'] got row features")

                    # fill masked arrays of features
                    push_feat_row, grasp_feat_row, place_feat_row = \
                            push_feat_row.filled(0.0), grasp_feat_row.filled(0.0), place_feat_row.filled(0.0)

                    # NOTE(adit98) what gets logged in these variables is unlikely to be relevant
                    # set predictions variables to row predictions if stack trainer not specified
                    if stack_trainer is None:
                        push_predictions, grasp_predictions, place_predictions = \
                                push_predictions_row, grasp_predictions_row, place_predictions_row

                if unstack_trainer is not None:
                    # unstack features
                    push_feat_unstack, grasp_feat_unstack, place_feat_unstack, push_predictions_unstack, \
                            grasp_predictions_unstack, place_predictions_unstack, _, _ = \
                            unstack_trainer.forward(color_heightmap, valid_depth_heightmap,
                                    is_volatile=True, keep_action_feat=True, demo_mask=True)
                    print("main.py nonlocal_pause['exit_called'] got unstack features")

                    # fill masked arrays of features
                    push_feat_unstack, grasp_feat_unstack, place_feat_unstack = \
                            push_feat_unstack.filled(0.0), grasp_feat_unstack.filled(0.0), place_feat_unstack.filled(0.0)

                    # NOTE(adit98) what gets logged in these variables is unlikely to be relevant
                    # set predictions variables to unstack predictions if stack trainer not specified
                    if stack_trainer is None:
                        push_predictions, grasp_predictions, place_predictions = \
                                push_predictions_unstack, grasp_predictions_unstack, place_predictions_unstack

                if vertical_square_trainer is not None:
                    # vertical_square features
                    push_feat_vertical_square, grasp_feat_vertical_square, place_feat_vertical_square, push_predictions_vertical_square, \
                            grasp_predictions_vertical_square, place_predictions_vertical_square, _, _ = \
                            vertical_square_trainer.forward(color_heightmap, valid_depth_heightmap,
                                    is_volatile=True, keep_action_feat=True, demo_mask=True)
                    print("main.py nonlocal_pause['exit_called'] got vertical_square features")

                    # fill masked arrays of features
                    push_feat_vertical_square, grasp_feat_vertical_square, place_feat_vertical_square = \
                            push_feat_vertical_square.filled(0.0), grasp_feat_vertical_square.filled(0.0), place_feat_vertical_square.filled(0.0)

                    # NOTE(adit98) what gets logged in these variables is unlikely to be relevant
                    # set predictions variables to vertical_square predictions if stack trainer not specified
                    if stack_trainer is None:
                        push_predictions, grasp_predictions, place_predictions = \
                                push_predictions_vertical_square, grasp_predictions_vertical_square, place_predictions_vertical_square

            else:
                # DONE(zhe) Need to ensure that "predictions" also have language mask
                # TODO(zhe) Goal condition needs to be false for grasp color task and language stacking.
                # TODO(elias) add check for if we're using language instruction
                # Generate the language mask using the language model.
                language_output = None # 4 ndarrays with shape (batch_size, 256, 2, 1) where each pixel has two options (block is present, block is not present)
                if static_language_mask and language_data_instance is not None:
                    with torch.no_grad():
                        language_output = language_model.forward(language_data_instance)
                if language_data_instance is not None:
                    command = [x for x in language_data_instance['command'][0] if x != "<PAD>"]
                    nonlocal_variables['language_metadata']['command'] = command
                    print(f"Command: {' '.join(command)}")

                push_predictions, grasp_predictions, place_predictions, state_feat, output_prob = \
                        trainer.forward(color_heightmap, valid_depth_heightmap,
                                is_volatile=True, goal_condition=goal_condition, language_output=language_output)

                # min of 5 active pixels or we kill the sim
                # if np.sum(1-grasp_predictions.mask) < 5:
                #     print(f"Grasp action '{command}' cannot be executed because of missing blocks, exiting!")
                #     no_change_count = end_trial()
                #     nonlocal_pause['exit_called'] = True
                # if np.sum(1-place_predictions.mask) < 5:
                #     print(f"Place action '{command}' cannot be executed because of missing blocks, exiting!")
                #     no_change_count = end_trial()
                #     nonlocal_pause['exit_called'] = True

            if not nonlocal_variables['finalize_prev_trial_log']:
                # Execute best primitive action on robot in another thread
                # START THE REAL ROBOT EXECUTING THE NEXT ACTION IN THE OTHER THREAD,
                # unless it is a new trial, then we will wait a moment to do final
                # logging before starting the next action
                nonlocal_variables['executing_action'] = True

        # Run training iteration in current thread (aka training thread)
        # NOTE(zhe) First time the loop doesn't run.
        if 'prev_color_img' in locals():

            # Detect changes
            change_detected, no_change_count = detect_changes(prev_primitive_action, depth_heightmap, prev_depth_heightmap, prev_grasp_success, no_change_count)

            if no_height_reward:
                # used to assess the value of the reward multiplier
                reward_multiplier = 1
            else:
                reward_multiplier = prev_stack_height

            # Compute training labels, returns are:
            # label_value == expected_reward (with future rewards)
            # prev_reward_value == current_reward (without future rewards)
            # NOTE(zhe) label_value is y_t, prev_reward_value is R_P
            # TODO(zhe) Edit label_value to include bisk stacking rewards.
            label_value, prev_reward_value = trainer.get_label_value(
                prev_primitive_action, prev_push_success, prev_grasp_success, change_detected,
                prev_push_predictions, prev_grasp_predictions, color_heightmap, valid_depth_heightmap,
                prev_color_success, goal_condition=prev_goal_condition, prev_place_predictions=prev_place_predictions,
                place_success=prev_partial_stack_success, reward_multiplier=reward_multiplier)
            # label_value is also known as expected_reward in trainer.get_label_value(), this is what the nn predicts.
            trainer.label_value_log.append([label_value])
            logger.write_to_log('label-value', trainer.label_value_log)
            # prev_reward_value is the regular old reward value actually based on the multiplier and action success
            trainer.reward_value_log.append([prev_reward_value])
            logger.write_to_log('reward-value', trainer.reward_value_log)
            trainer.change_detected_log.append([change_detected])
            logger.write_to_log('change-detected', trainer.change_detected_log)
            logger.write_to_log('grasp-success', trainer.grasp_success_log)
            if nonlocal_variables['stack'].is_goal_conditioned_task and grasp_color_task:
                trainer.goal_condition_log.append(nonlocal_variables['stack'].current_one_hot())
                logger.write_to_log('goal-condition', trainer.goal_condition_log)
                logger.write_to_log('color-success', trainer.color_success_log)
            if static_language_mask:
                logger.write_to_log('grasp-color-success', trainer.grasp_color_success_log)
            if place:
                logger.write_to_log('stack-height', trainer.stack_height_log)
                logger.write_to_log('partial-stack-success', trainer.partial_stack_success_log)
                logger.write_to_log('place-success', trainer.place_success_log)
            if nonlocal_variables['finalize_prev_trial_log']:
                # Do final logging from the previous trial and previous complete iteration
                nonlocal_variables['finalize_prev_trial_log'] = False
                trainer.trial_reward_value_log_update()
                logger.write_to_log('trial-reward-value', trainer.trial_reward_value_log)
                logger.write_to_log('iteration', np.array([trainer.iteration]))
                logger.write_to_log('trial-success', trainer.trial_success_log)
                logger.write_to_log('trial', trainer.trial_log)
                logger.write_to_log('load_snapshot_file_iteration', trainer.load_snapshot_file_iteration_log)
                best_dict, prev_best_dict, current_dict = save_plot(trainer, plot_window, is_testing,
                        num_trials, best_dict, logger, title, place, prev_best_dict, task_type=task_type)
                # if we exceeded max_train_actions at the end of the last trial, stop training
                if max_train_actions is not None and trainer.iteration > max_train_actions:
                    nonlocal_pause['exit_called'] = True
                print('Trial logging complete: ' + str(num_trials) + ' --------------------------------------------------------------')

                # reset the state for this trial THEN START EXECUTING THE ACTION FOR THE NEW TRIAL
                if check_z_height:
                    # TODO(ahundt) BUG THIS A NEW LOCATION BUT WE MUST BE SURE WE ARE NOT MESSING UP TRIAL REWARDS
                    # Zero out the height because the trial is done.
                    # Note these lines must be after the logging of these variables is complete.
                    nonlocal_variables['stack_height'] = 1.0
                    nonlocal_variables['prev_stack_height'] = 1.0
                else:
                    # Set back to the minimum stack height because the trial is done.
                    # Note these lines must be after the logging of these variables is complete.
                    nonlocal_variables['stack_height'] = 1
                    nonlocal_variables['prev_stack_height'] = 1
                # Start executing the action for the new trial
                nonlocal_variables['executing_action'] = True

            # Adjust exploration probability
            if not is_testing:
                explore_decay_rate = 0.9996
                if (max_train_actions is not None and max_train_actions > 20000) or (max_train_actions is None and (max_iter == -1 or max_iter > 20000)):
                    # do more exploration on longer runs
                    explore_decay_rate = 0.9999
                explore_prob = max(0.5 * np.power(explore_decay_rate, trainer.iteration), 0.01) if explore_rate_decay else 0.5

            # Do sampling for experience replay
            if experience_replay_enabled and prev_primitive_action is not None and not is_testing:
                # Choose if experience replay should be trained on a
                # historical successful or failed action
                if prev_primitive_action == 'push':
                    train_on_successful_experience = not change_detected
                elif prev_primitive_action == 'grasp':
                    train_on_successful_experience = not prev_grasp_success
                elif prev_primitive_action == 'place':
                    train_on_successful_experience = not prev_partial_stack_success
                # TODO(ahundt) the experience replay delays real robot execution, only use the parallel version below, delete this and move this code block down if that's been ok for a while.
                # Here we will try to sample a reward value from the same action as the current one
                # which differs from the most recent reward value to reduce the chance of catastrophic forgetting.
                # experience_replay(method, prev_primitive_action, prev_reward_value, trainer, grasp_color_task, logger,
                #                   nonlocal_variables, place, goal_condition, trial_reward=trial_reward,
                #                   train_on_successful_experience=train_on_successful_experience)

            # Save model snapshot
            if not is_testing:
                logger.save_backup_model(trainer.model, method)
                # save the best model based on all tracked plotting metrics.
                for k, v in best_dict.items():
                    if k in prev_best_dict and (prev_best_dict[k] is None or v > prev_best_dict[k]):
                        best_model_name = method + '_' + k
                        logger.save_model(trainer.model, best_model_name)
                        best_stats_file = os.path.join(logger.models_directory, best_model_name + '.json')
                        print('Saving new best model with stats in: ' + best_stats_file)
                        with open(best_stats_file, 'w') as f:
                            json.dump(best_dict, f, cls=utils.NumpyEncoder, sort_keys=True)
                        current_stats_file = os.path.join(logger.models_directory, best_model_name + '_current_stats.json')
                        print('Saving new best model current stats in: ' + current_stats_file)
                        with open(current_stats_file, 'w') as f:
                            json.dump(current_dict, f, cls=utils.NumpyEncoder, sort_keys=True)

                # saves once every time logs are finalized
                if nonlocal_variables['save_state_this_iteration']:
                    nonlocal_variables['save_state_this_iteration'] = False

                    logger.save_model(trainer.model, method)

                    # copy nonlocal_variable values and discard those which shouldn't be saved
                    nonlocals_to_save = nonlocal_variables.copy()
                    entries_to_pop = always_default_nonlocals.copy()

                    # save all entries which are JSON serializable only. Otherwise don't save
                    for k, v in nonlocals_to_save.items():
                        if not utils.is_jsonable(v):
                            entries_to_pop.append(k)

                    for k in entries_to_pop:
                        nonlocals_to_save.pop(k)

                    # save nonlocal_variables for resuming later
                    save_location = os.path.join(logger.base_directory, 'data', 'variables')
                    if not os.path.exists(save_location):
                        os.makedirs(save_location)
                    with open(os.path.join(save_location, 'nonlocal_vars_%d.json' % (trainer.iteration)), 'w') as f:
                        json.dump(nonlocals_to_save, f, cls=utils.NumpyEncoder, sort_keys=True)

                    if trainer.use_cuda:
                        trainer.model = trainer.model.cuda()

                # reload the best model if trial performance has declined by more than 10%
                if(trainer.iteration >= 1000 and 'trial_success_rate_best_value' in best_dict and 'trial_success_rate_current_value' in current_dict and
                   trainer_iteration_of_most_recent_model_reload + 60 < trainer.iteration):
                    # TODO(ahundt) check if the default scale requrement for a bad decline should be even smaller like 0.5 or 0.4
                    def is_bad_decline(best, current, subtract=0.1, scale=0.6):
                        return (best - subtract) * scale > current
                    def is_bad_decline_string(name, scale=0.6):
                        current_success = current_dict[name + '_success_rate_current_value']
                        best_success = best_dict[name + '_success_rate_best_value']
                        is_bad = is_bad_decline(best_success, current_success, scale=scale)
                        if is_bad:
                            print('WARNING: ' + name + ' success declined from the best ' + str(best_success) + ' to below the allowed limit of ' + str(current_success))
                        return is_bad

                    # check if grasp success or place success dropped a lot
                    bad_action_decline = is_bad_decline_string('grasp') or (place and is_bad_decline_string('place'))
                    if is_bad_decline_string('trial', scale=0.9) or bad_action_decline:
                        # The model quality has declined too much from the peak, reload the previous best model.
                        snapshot_file = choose_testing_snapshot(logger.base_directory, best_dict)
                        trainer.load_snapshot_file(snapshot_file)
                        logger.write_to_log('load_snapshot_file_iteration', trainer.load_snapshot_file_iteration_log)
                        trainer_iteration_of_most_recent_model_reload = trainer.iteration
                        print('WARNING: reloading the best model ' + str(snapshot_file))
                    # Trial decline needs to be worse for an actual reset
                    if is_bad_decline_string('trial') or bad_action_decline:
                        print('ERROR: PROBLEM DETECTED IN SCENE, STEEP TRIAL, GRASP, OR PLACE PERFORMANCE DECLINE, RESETTING THE OBJECTS TO RECOVER... sometimes performance declines because the simulator is in a physically impossible state, so move on to the next trial to be safe.')
                        num_problems_detected = sim_problem_end_trial(num_problems_detected)

                # Save model if we are at a new best stack rate
                if place and trainer.iteration >= 1000:
                    # if the stack rate is lower that means new stacks happen in fewer actions.
                    if nonlocal_variables['stack_rate'] < best_stack_rate:
                        best_stack_rate = nonlocal_variables['stack_rate']
                        stack_rate_str = method + '-best-stack-rate'
                        logger.save_backup_model(trainer.model, stack_rate_str)
                        logger.save_model(trainer.model, stack_rate_str)
                        logger.write_to_log('best-iteration', np.array([trainer.iteration]))

                    if trainer.use_cuda:
                        trainer.model = trainer.model.cuda()

            # Backprop is enabled on a per-action basis, or if the current iteration is over a certain threshold
            backprop_enabled = trainer.randomize_trunk_weights(backprop_enabled, random_trunk_weights_max, random_trunk_weights_reset_iters, random_trunk_weights_min_success)
            # Backpropagate
            if prev_primitive_action is not None and backprop_enabled[prev_primitive_action] and not disable_two_step_backprop and not is_testing:
                print('Running two step backprop()')
                #if use_demo:
                #    demo_color_heightmap, demo_depth_heightmap = \
                #            demo.get_heightmaps(prev_primitive_action, prev_stack_height)
                #    trainer.backprop(demo_color_heightmap, demo_depth_heightmap,
                #            prev_primitive_action, prev_best_dict, label_value,
                #            goal_condition=prev_goal_condition)
                trainer.backprop(prev_color_heightmap, prev_valid_depth_heightmap,
                        prev_primitive_action, prev_best_pix_ind, label_value,
                        goal_condition=prev_goal_condition, use_demo=use_demo)

        # While in simulated mode we need to keep count of simulator problems,
        # because the simulator's physics engine is pretty buggy. For example, solid
        # objects sometimes stick to each other or have their volumes intersect, and
        # on occasion this and/or Inverse Kinematics issues lead to acceleration to
        # nearly infinite velocities. We attempt to detect these situation and
        # when problems occur we start with simple workarounds. If that does not work
        # we apply more intrusive resets to restore the simulator to a valid state.
        #
        # Fortunately for us, the real robot is not like the simulator in that it
        # tends to obey the laws of physics. :-) However, it can suffer from its own
        # issues like safety/security stops when it collides with objects. In these
        # cases it will not move until a human resets the robot. Therefore, we also
        # try to detect these issues and wait for a human to intervene before resuming
        # real robot execution. The way we do this is to make sure the robot is actually
        # at the home position before moving on to the next iteration.
        num_problems_detected = 0
        # The real robot may experience security stops, so we must check for those too.
        wait_until_home_and_not_executing_action = not is_sim
        # nonlocal variable for quick threading workaround
        real_home = {'is_home': False, 'home_lock': threading.Lock()}

        # This is the primary experience replay loop which runs while the separate
        # robot thread is physically moving as well as when the program is paused.
        while nonlocal_variables['executing_action'] or nonlocal_pause['pause'] or wait_until_home_and_not_executing_action:
            if prev_primitive_action is not None and backprop_enabled[prev_primitive_action] and experience_replay_enabled and not is_testing:
                # flip between training success and failure, disabled because it appears to slow training down
                # train_on_successful_experience = not train_on_successful_experience
                # do some experience replay while waiting, rather than sleeping
                experience_replay(method, prev_primitive_action, prev_reward_value, trainer,
                                  grasp_color_task, logger, nonlocal_variables, place, goal_condition,
                                  trial_reward=trial_reward or discounted_reward, train_on_successful_experience=train_on_successful_experience)
            else:
                time.sleep(0.1)
            time_elapsed = time.time()-iteration_time_0
            if nonlocal_pause['pause']:
                print('Pause engaged for ' + str(time_elapsed) + ' seconds, press ctrl + c after at least 5 seconds to resume.')
            elif not is_sim and not nonlocal_variables['executing_action']:
                # the real robot should not move to the next action until execution of this action is complete AND
                # the robot has actually made it home. This is to prevent collecting bad data after a security stop due to the robot colliding.
                # Here the action has finished, now we must make sure we are home.
                def homing_thread():
                    with real_home['home_lock']:
                        # send the robot home
                        real_home['is_home'] = robot.go_home(block_until_home=True)
                if real_home['home_lock'].acquire(blocking=False):
                    if num_problems_detected == 0:
                        # check if we are home
                        wait_until_home_and_not_executing_action = not robot.block_until_home(0)
                        num_problems_detected += 1
                    if num_problems_detected > 0:
                        # Command the robot to go home if we are not home
                        wait_until_home_and_not_executing_action = not real_home['is_home']
                        if wait_until_home_and_not_executing_action:
                            # start a thread to go home, we will continue to experience replay while we wait
                            t = threading.Thread(target=homing_thread)
                            t.start()
                            num_problems_detected += 1
                    real_home['home_lock'].release()

                if wait_until_home_and_not_executing_action and num_problems_detected > 2:
                    print('The robot was not at home after the current action finished running. '
                          'Make sure the robot did not experience either an error or security stop. '
                          'WARNING: The robot will attempt to go home again in a few seconds.')
            elif is_sim and int(time_elapsed) > timeout:
                # The simulator can experience catastrophic physics instability, so here we detect that and reset.
                print('ERROR: PROBLEM DETECTED IN SCENE, NO CHANGES FOR OVER 60 SECONDS, RESETTING THE OBJECTS TO RECOVER...')
                num_problems_detected = sim_problem_end_trial(num_problems_detected)
                # don't reset again for 20 more seconds
                iteration_time_0 = time.time()
                # TODO(ahundt) Improve recovery: maybe set trial_complete = True here and call continue or set do_continue = True?

        if nonlocal_pause['exit_called']:
            # shut down the simulation or robot
            robot.shutdown()
            break


        # If we don't have any successes reinitialize model
        # Save information for next training step
        prev_color_img = color_img.copy()
        prev_depth_img = depth_img.copy()
        prev_color_heightmap = color_heightmap.copy()
        prev_depth_heightmap = depth_heightmap.copy()
        prev_valid_depth_heightmap = valid_depth_heightmap.copy()
        prev_push_success = copy.deepcopy(nonlocal_variables['push_success'])
        prev_grasp_success = copy.deepcopy(nonlocal_variables['grasp_success'])
        prev_primitive_action = copy.deepcopy(nonlocal_variables['primitive_action'])
        prev_place_success = copy.deepcopy(nonlocal_variables['place_success'])
        prev_partial_stack_success = copy.deepcopy(nonlocal_variables['partial_stack_success'])
        # stack_height will just always be 1 if we are not actually stacking
        prev_stack_height = copy.deepcopy(nonlocal_variables['stack_height'])
        nonlocal_variables['prev_stack_height'] = copy.deepcopy(nonlocal_variables['stack_height'])
        prev_push_predictions = push_predictions.copy()
        prev_grasp_predictions = grasp_predictions.copy()
        prev_place_predictions = place_predictions
        prev_best_pix_ind = copy.deepcopy(nonlocal_variables['best_pix_ind'])
        # TODO(ahundt) BUG We almost certainly need to copy nonlocal_variables['stack']
        prev_stack = copy.deepcopy(nonlocal_variables['stack'])
        prev_goal_condition = copy.deepcopy(goal_condition)
        if grasp_color_task:
            prev_color_success = copy.deepcopy(nonlocal_variables['grasp_color_success'])
            if nonlocal_variables['grasp_success'] and nonlocal_variables['grasp_color_success']:
                # Choose the next color block to grasp, or None if not running in goal conditioned mode
                nonlocal_variables['stack'].next()
                print('NEW GOAL COLOR: ' + str(robot.color_names[nonlocal_variables['stack'].object_color_index]) + ' GOAL CONDITION ENCODING: ' + str(nonlocal_variables['stack'].current_one_hot()))
        else:
            prev_color_success = None

        # TODO(elias) this is where we check for trial completion nonlocal_variables['train_complete]
        #if nonlocal_variables['trial_complete']:
            # load next image of the task
            # use trial number variable as the index to pick out the image
            # use that also to get/save the string

        iteration_time_1 = time.time()
        print('Time elapsed: %f' % (iteration_time_1-iteration_time_0))

        print('Trainer iteration: %d complete' % int(trainer.iteration))
        if use_demo:
            if stack_trainer is not None:
                stack_trainer.iteration += 1
            if row_trainer is not None:
                row_trainer.iteration += 1
            if unstack_trainer is not None:
                unstack_trainer.iteration += 1
            if vertical_square_trainer is not None:
                vertical_square_trainer.iteration += 1

        else:
            trainer.iteration += 1

    nonlocal_pause['process_actions_exit_called'] = True
    # Save the final plot when the run has completed cleanly, plus specifically handle preset cases
    best_dict, prev_best_dict, current_dict = save_plot(trainer, plot_window, is_testing, num_trials,
            best_dict, logger, title, place, prev_best_dict, preset_files, task_type=task_type)
    if not is_testing:
        # save a backup of the best training stats from the original run, this is because plotting updates
        # or other utilities might modify or overwrite the real stats fom the original run.
        best_stats_path = os.path.join(logger.base_directory, 'best_stats.json')
        best_stats_backup_path = os.path.join(logger.base_directory, 'models', 'training_best_stats.json')
        shutil.copyfile(best_stats_path, best_stats_backup_path)
    return logger.base_directory, best_dict

def sim_object_state_to_json(robot):
    # Dump scene state information to a file.
    sim_positions, sim_orientations = robot.get_obj_positions_and_orientations()
    to_ret = {'positions': sim_positions,
              'orientations': sim_orientations,
              'color_names': robot.color_names,
              'num_obj': robot.num_obj}
    return to_ret

def dump_sim_object_state_to_json(robot, logger, filename):
    # Dump scene state information to a file.
    to_dump = sim_object_state_to_json(robot)
    save_location = os.path.join(logger.base_directory, 'data', 'variables')
    if not os.path.exists(save_location):
        os.mkdir(save_location)
    with open(os.path.join(save_location, filename), 'w') as f:
            json.dump(to_dump, f, cls=utils.NumpyEncoder, sort_keys=True)

def parse_resume_and_snapshot_file_args(args):
    if args.resume == 'last':
        dirs = [os.path.join(os.path.abspath('logs'), p) for p in os.listdir(os.path.abspath('logs'))]
        dirs = list(filter(os.path.isdir, dirs))
        if dirs:
            continue_logging = True
            logging_directory = sorted(dirs)[-1]
        else:
            print('no logging dirs to resume, starting new run')
            continue_logging = False
            logging_directory = os.path.abspath('logs')
    elif args.resume:
        continue_logging = True
        logging_directory = os.path.abspath(args.resume)
    else:
        continue_logging = False
        logging_directory = os.path.abspath('logs')

    snapshot_file = os.path.abspath(args.snapshot_file) if args.snapshot_file else ''
    multi_task_snapshot_files = {}
    # load all snapshots
    if args.stack_snapshot_file: multi_task_snapshot_files['stack'] = os.path.abspath(args.stack_snapshot_file)
    if args.row_snapshot_file: multi_task_snapshot_files['row'] = os.path.abspath(args.row_snapshot_file)
    if args.unstack_snapshot_file: multi_task_snapshot_files['unstack'] = os.path.abspath(args.unstack_snapshot_file)
    if args.vertical_square_snapshot_file: multi_task_snapshot_files['vertical'] = os.path.abspath(args.vertical_square_snapshot_file)

    # if neither snapshot file is provided
    if continue_logging:
        if (not args.check_row and not args.stack_snapshot_file) or (args.check_row and not args.row_snapshot_file):
            snapshot_file = os.path.join(logging_directory, 'models', 'snapshot.reinforcement.pth')
            print('loading snapshot file: ' + snapshot_file)
            if not os.path.isfile(snapshot_file):
                snapshot_file = os.path.join(logging_directory, 'models',
                        'snapshot-backup.reinforcement.pth')
                print('snapshot file does not exist, trying backup: ' + snapshot_file)
            if not os.path.isfile(snapshot_file):
                print('cannot resume, no snapshots exist, check the code and your \
                        log directory for errors')
                exit(1)

    return snapshot_file, multi_task_snapshot_files, continue_logging, logging_directory

def save_plot(trainer, plot_window, is_testing, num_trials, best_dict, logger, title, place, prev_best_dict, preset_files=None, task_type=None):
    if preset_files is not None:
        # note preset_files is changing from a list of strings to an integer
        preset_files = len(preset_files)
    current_dict = {}
    if (trainer.iteration > plot_window or is_testing) and num_trials > 1:
        prev_best_dict = copy.deepcopy(best_dict)
        if is_testing:
            # when testing the plot data should be averaged across the whole run
            # if less than 3 iterations have passed, set plot_window to 1
            plot_window = max(1, trainer.iteration - 3)
        best_dict, current_dict = plot.plot_it(logger.base_directory, title, place=place,
                window=plot_window, num_preset_arrangements=preset_files, task_type=task_type)
    return best_dict, prev_best_dict, current_dict

def detect_changes(prev_primitive_action, depth_heightmap, prev_depth_heightmap, prev_grasp_success, no_change_count, change_threshold=300):
    """ Detect changes

    # NOTE: original VPG change_threshold was 300
    """
    depth_diff = abs(depth_heightmap - prev_depth_heightmap)
    depth_diff[np.isnan(depth_diff)] = 0
    depth_diff[depth_diff > 0.3] = 0
    depth_diff[depth_diff < 0.01] = 0
    depth_diff[depth_diff > 0] = 1
    change_value = np.sum(depth_diff)
    change_detected = change_value > change_threshold or prev_grasp_success
    print('Change detected: %r (value: %d)' % (change_detected, change_value))

    if change_detected:
        if prev_primitive_action == 'push':
            no_change_count[0] = 0
        elif prev_primitive_action == 'grasp' or prev_primitive_action == 'place':
            no_change_count[1] = 0
    else:
        if prev_primitive_action == 'push':
            no_change_count[0] += 1
        elif prev_primitive_action == 'grasp':
            no_change_count[1] += 1
    return change_detected, no_change_count

def get_and_save_images(robot, workspace_limits, heightmap_resolution, logger, trainer, filename_poststring='0', save_image=True, depth_channels_history=False, history_len=3):
    # Get latest RGB-D image
    valid_depth_heightmap, color_heightmap, depth_heightmap, _, color_img, depth_img = robot.get_camera_data(return_heightmaps=True)

    # Save RGB-D images and RGB-D heightmaps
    if save_image:
        logger.save_images(trainer.iteration, color_img, depth_img, filename_poststring)
        logger.save_heightmaps(trainer.iteration, color_heightmap, valid_depth_heightmap, filename_poststring)

    # load history and modify valid_depth_heightmap
    if depth_channels_history:
        valid_depth_heightmap = trainer.generate_hist_heightmap(valid_depth_heightmap,
                trainer.iteration, logger)

    # otherwise, repeat depth values in each channel
    else:
        valid_depth_heightmap = np.stack([valid_depth_heightmap] * 3, axis=-1)

    if robot.is_sim:
        # Dump scene state information to a file for analysis, training, and language models.
        dump_sim_object_state_to_json(robot, logger, 'object_positions_and_orientations_' + str(trainer.iteration) + '_' + filename_poststring + '.json')

    return valid_depth_heightmap, color_heightmap, depth_heightmap, color_img, depth_img

def experience_replay(method, prev_primitive_action, prev_reward_value, trainer, grasp_color_task, logger, nonlocal_variables, place, goal_condition, all_history_prob=0.05, trial_reward=False, train_on_successful_experience=None):
    # Here we will try to sample a reward value from the same action as the current one
    # which differs from the most recent reward value to reduce the chance of catastrophic forgetting.
    # TODO(ahundt) experience replay is very hard-coded with lots of bugs, won't evaluate all reward possibilities, and doesn't deal with long range time dependencies.
    sample_primitive_action = prev_primitive_action
    sample_primitive_action_id = ACTION_TO_ID[sample_primitive_action]
    if trial_reward and len(trainer.trial_reward_value_log) > 2:
        log_len = len(trainer.trial_reward_value_log)
    else:
        trial_reward = False
        log_len = trainer.iteration
    # executed_action_log includes the action, push grasp or place, and the best pixel index
    actions = np.asarray(trainer.executed_action_log)[1:log_len, 0]

    # Get samples of the same primitive but with different success results
    if np.random.random(1) < all_history_prob:
        # Sample all of history every one out of n times.
        sample_ind = np.arange(1, log_len-1).reshape(log_len-2, 1)
    else:
        # Sample from the current specific action
        if sample_primitive_action == 'push':
            # sample_primitive_action_id = 0
            log_to_compare = np.asarray(trainer.change_detected_log)
        elif sample_primitive_action == 'grasp':
            # sample_primitive_action_id = 1
            log_to_compare = np.asarray(trainer.grasp_success_log)
        elif sample_primitive_action == 'place':
            log_to_compare = np.asarray(trainer.partial_stack_success_log)
        else:
            raise NotImplementedError('ERROR: ' + sample_primitive_action + ' action is not yet supported in experience replay')

        sample_ind = np.argwhere(np.logical_and(log_to_compare[1:log_len, 0] == train_on_successful_experience,
                                                actions == sample_primitive_action_id))

    if sample_ind.size == 0 and (trial_reward or prev_reward_value is not None) and log_len > 2:
        print('Experience Replay: We do not have samples for the ' + sample_primitive_action + ' action with a success state of ' + str(train_on_successful_experience) + ', so sampling from the whole history.')
        sample_ind = np.arange(1, log_len-1).reshape(log_len-2, 1)

    if sample_ind.size > 0:
        # Find sample with highest surprise value
        if method == 'reactive':
            # TODO(ahundt) BUG what to do with prev_reward_value? (formerly named sample_reward_value in previous commits)
            sample_surprise_values = np.abs(np.asarray(trainer.predicted_value_log)[sample_ind[:, 0]] - (1 - prev_reward_value))
        elif method == 'reinforcement':
            if trial_reward:
                sample_surprise_values = np.abs(np.asarray(trainer.predicted_value_log)[sample_ind[:, 0]] - np.asarray(trainer.trial_reward_value_log)[sample_ind[:,0]])
            else:
                sample_surprise_values = np.abs(np.asarray(trainer.predicted_value_log)[sample_ind[:, 0]] - np.asarray(trainer.label_value_log)[sample_ind[:,0]])
        sorted_surprise_ind = np.argsort(sample_surprise_values[:, 0])
        sorted_sample_ind = sample_ind[sorted_surprise_ind, 0]
        pow_law_exp = 2
        rand_sample_ind = int(np.round(np.random.power(pow_law_exp, 1)*(sample_ind.size-1)))
        # sample_iteration is the actual time step on which we will run experience replay
        sample_iteration = sorted_sample_ind[rand_sample_ind]

        nonlocal_variables['replay_iteration'] += 1
        # Load the data from disk, and run a forward pass with the current model
        [sample_stack_height, sample_primitive_action_id, sample_grasp_success,
         sample_change_detected, sample_push_predictions, sample_grasp_predictions,
         next_sample_color_heightmap, next_sample_depth_heightmap, sample_color_success,
         exp_goal_condition, sample_place_predictions, sample_place_success, sample_color_heightmap,
         sample_depth_heightmap] = trainer.load_sample(sample_iteration, logger, depth_channels_history=args.depth_channels_history)

        sample_primitive_action = ID_TO_ACTION[sample_primitive_action_id]
        print('Experience replay %d: history timestep index %d, action: %s, surprise value: %f' % (nonlocal_variables['replay_iteration'], sample_iteration, str(sample_primitive_action), sample_surprise_values[sorted_surprise_ind[rand_sample_ind]]))
        # sample_push_success is always true in the current version, because it only checks if the push action run, not if something was actually pushed, that is handled by change_detected.
        sample_push_success = True
        # TODO(ahundt) deleteme if this has been working for a while, sample reward value isn't actually used for anything...
        # if trial_reward:
        #     sample_reward_value = trainer.trial_reward_value_log[sample_iteration]
        # else:
        #     sample_reward_value = trainer.reward_value_log[sample_iteration]

        # if no_height_reward:  # TODO(ahundt) why does the args.no_height_reward line below work and the regular no_height_reward here broken?
        if args.no_height_reward:
            # used to assess the value of the reward multiplier
            reward_multiplier = 1
        else:
            reward_multiplier = sample_stack_height
        # TODO(ahundt) This mix of current and next parameters (like next_sample_color_heightmap and sample_push_success) seems a likely spot for a bug, we must make sure we haven't broken the behavior. ahundt has already fixed one bug here.
        # get_label_value does the forward pass for updating the label value log.
        update_label_value_log = False
        if update_label_value_log:
            new_sample_label_value, _ = trainer.get_label_value(
                sample_primitive_action, sample_push_success, sample_grasp_success, sample_change_detected,
                sample_push_predictions, sample_grasp_predictions, next_sample_color_heightmap, next_sample_depth_heightmap,
                sample_color_success, goal_condition=exp_goal_condition, prev_place_predictions=sample_place_predictions,
                place_success=sample_place_success, reward_multiplier=reward_multiplier)

        if trial_reward:
            reward_to_backprop = trainer.trial_reward_value_log[sample_iteration]
        else:
            reward_to_backprop = trainer.label_value_log[sample_iteration]

        # Get labels for sample and backpropagate, trainer.backprop also does a forward pass internally.
        sample_best_pix_ind = np.asarray(trainer.executed_action_log)[sample_iteration, 1:4].astype(np.int)
        trainer.backprop(sample_color_heightmap, sample_depth_heightmap, sample_primitive_action, sample_best_pix_ind,
                         reward_to_backprop, goal_condition=exp_goal_condition)
        # Recompute prediction value and label for replay buffer
        if sample_primitive_action == 'push':
            trainer.predicted_value_log[sample_iteration] = [np.ma.max(sample_push_predictions)]
            # trainer.predicted_value_log[sample_iteration] = [sample_push_predictions[sample_best_pix_ind[0], sample_best_pix_ind[1], sample_best_pix_ind[2]]]
        elif sample_primitive_action == 'grasp':
            trainer.predicted_value_log[sample_iteration] = [np.ma.max(sample_grasp_predictions)]
            # trainer.predicted_value_log[sample_iteration] = [sample_grasp_predictions[sample_best_pix_ind[0], sample_best_pix_ind[1], sample_best_pix_ind[2]]]
        elif sample_primitive_action == 'place':
            trainer.predicted_value_log[sample_iteration] = [np.ma.max(sample_place_predictions)]
            # trainer.predicted_value_log[sample_iteration] = [sample_place_predictions[sample_best_pix_ind[0], sample_best_pix_ind[1], sample_best_pix_ind[2]]]

        if update_label_value_log:
            trainer.label_value_log[sample_iteration] = [new_sample_label_value]

    else:
        # print('Experience Replay: 0 prior training samples. Skipping experience replay.')
        time.sleep(0.01)

def choose_testing_snapshot(training_base_directory, best_dict, prioritize_action_efficiency=False):
    """ Select the best test mode snapshot model file to load after training.
    """
    testing_snapshot = ''
    print('Choosing a snapshot from the following options:' + str(best_dict))
    print('Evaluating trial_success_rate_best_value')
    if 'trial_success_rate_best_value' in best_dict:
        best_trial_value = best_dict['trial_success_rate_best_value']
        best_trial_snapshot = os.path.join(training_base_directory, 'models', 'snapshot.reinforcement_trial_success_rate_best_value.pth')
        if os.path.exists(best_trial_snapshot):
            testing_snapshot = best_trial_snapshot
        else:
            print(best_trial_snapshot + ' does not exist, looking for other options.')
        # If the best trial success rate is high enough, lets use the best action efficiency model
        if 'grasp_success_rate_best_value' in best_dict and (not testing_snapshot or (best_trial_value > 0.99 and best_dict['grasp_success_rate_best_value'] > 0.9)):
            if testing_snapshot:
                print('The trial_success_rate_best_value is fantastic at ' + str(best_trial_value) + ', so we will look for the best grasp_success_rate_best_value.')
            best_grasp_efficiency_snapshot = os.path.join(training_base_directory, 'models', 'snapshot.reinforcement_grasp_success_rate_best_value.pth')
            if os.path.exists(best_grasp_efficiency_snapshot):
                testing_snapshot = best_grasp_efficiency_snapshot
            else:
                print(best_grasp_efficiency_snapshot + ' does not exist, looking for other options.')
        if 'action_efficiency_best_value' in best_dict and (prioritize_action_efficiency or best_trial_value > 0.99) and best_dict['action_efficiency_best_value'] > .5:
            if testing_snapshot:
                print('The trial_success_rate_best_value is fantastic at ' + str(best_trial_value) + ', so we will look for the best action_efficiency_best_value.')
            best_efficiency_snapshot = os.path.join(training_base_directory, 'models', 'snapshot.reinforcement_action_efficiency_best_value.pth')
            if os.path.exists(best_efficiency_snapshot):
                testing_snapshot = best_efficiency_snapshot
            else:
                print(best_efficiency_snapshot + ' does not exist, looking for other options.')

    if not testing_snapshot:
        print('Could not find any best-of models, checking for the basic training models.')
        final_snapshot = os.path.join(training_base_directory, 'models', 'snapshot.reinforcement.pth')
        if os.path.exists(final_snapshot):
            testing_snapshot = final_snapshot
        else:
            print(final_snapshot + ' does not exist, looking for other options.')
        final_snapshot = os.path.join(training_base_directory, 'models', 'snapshot.reactive.pth')
        if os.path.exists(final_snapshot):
            testing_snapshot = final_snapshot
        else:
            print(final_snapshot + ' does not exist, looking for other options.')

    print('Shapshot chosen: ' + testing_snapshot)
    return testing_snapshot

def check_training_complete(args):
    ''' Function for use at program startup to check if we should run training some more or move on to testing mode.
    '''
    snapshot_file, multi_task_snapshot_files, continue_logging, logging_directory = parse_resume_and_snapshot_file_args(args)

    training_complete = False
    iteration = 0
    if continue_logging:
        transitions_directory = os.path.join(logging_directory, 'transitions')
        kwargs = {'delimiter': ' ', 'ndmin': 2}
        iteration = int(np.loadtxt(os.path.join(transitions_directory, 'iteration.log.txt'), **kwargs)[0, 0])
        max_iter_complete = args.max_train_actions is None and (args.max_iter > 0 and iteration > args.max_iter)
        max_train_actions_complete = args.max_train_actions is not None and iteration > args.max_train_actions
        training_complete = max_iter_complete or max_train_actions_complete

    return training_complete, logging_directory

def one_train_test_run(args):
    ''' One run of all necessary training and testing configurations.
    '''
    training_complete, training_base_directory = check_training_complete(args)

    if not training_complete:
        # Run main program with specified arguments
        training_base_directory, best_dict = main(args)
    else:
        best_dict_path = os.path.join(training_base_directory, 'best_stats.json')
        if os.path.exists(best_dict_path):
            with open(best_dict_path, 'r') as f:
                best_dict = json.load(f)
        else:
            raise ValueError('main.py one_train_test_run() best_dict:' + best_dict_path + ' does not exist! Cannot load final results.')
    # if os.path.exists()
    testing_best_dict = {}
    testing_dest_dir = ''
    preset_testing_dest_dir = ''
    if args.max_train_actions is not None:
        if args.resume:
            # testing mode will always start from scratch
            args.resume = None
        print('Training Complete! Dir: ' + training_base_directory)
        testing_snapshot = choose_testing_snapshot(training_base_directory, best_dict)
        print('testing snapshot: ' + str(testing_snapshot))
        args.snapshot_file = testing_snapshot
        args.random_seed = 1238
        args.is_testing = True
        args.save_visualizations = True
        args.max_test_trials = 100
        testing_base_directory, testing_best_dict = main(args)
        # move the testing data into the training directory
        testing_dest_dir = shutil.move(testing_base_directory, training_base_directory)
        # TODO(ahundt) figure out if this symlink caused a crash, fix bug and re-enable
        # os.symlink(testing_dest_dir, training_base_directory)
        if not args.place:
            # run preset arrangements for pushing and grasping
            pargs = copy.deepcopy(args)
            pargs.test_preset_cases = True
            pargs.max_test_trials = 10
            # run testing mode
            preset_testing_base_directory, preset_testing_best_dict = main(pargs)
            preset_testing_dest_dir = shutil.move(preset_testing_base_directory, training_base_directory)
            # TODO(ahundt) figure out if this symlink caused a crash, fix bug and re-enable
            # os.symlink(preset_testing_dest_dir, training_base_directory)
            print('Challenging Arrangements Preset Testing Complete! Dir: ' + preset_testing_dest_dir)
            print('Challenging Arrangements Preset Testing results: \n ' + str(preset_testing_best_dict))

        # Test action efficiency model too
        testing_snapshot_action_efficiency = choose_testing_snapshot(training_base_directory, best_dict, prioritize_action_efficiency=True)
        if testing_snapshot_action_efficiency != testing_snapshot:
            print('testing snapshot, prioritizing action efficiency: ' + str(testing_snapshot))
            args.snapshot_file = testing_snapshot_action_efficiency
            efficiency_testing_base_directory, eff_testing_best_dict = main(args)
            # move the testing data into the training directory
            eff_testing_dest_dir = shutil.move(efficiency_testing_base_directory, training_base_directory)

            if not args.place:
                # run preset arrangements for pushing and grasping efficiency configuration
                pargs = copy.deepcopy(args)
                pargs.test_preset_cases = True
                pargs.max_test_trials = 10
                # run testing mode
                preset_testing_base_directory, preset_testing_best_dict = main(pargs)
                preset_testing_dest_dir = shutil.move(preset_testing_base_directory, training_base_directory)
                # TODO(ahundt) figure out if this symlink caused a crash, fix bug and re-enable
                # os.symlink(preset_testing_dest_dir, training_base_directory)
                print('Challenging Arrangements Preset Testing Complete! Action Efficiency Model Dir: ' + preset_testing_dest_dir)
                print('Challenging Arrangements Preset Testing results Action Efficiency Model Dir: \n ' + str(preset_testing_best_dict))

            test_diff = eff_testing_best_dict['trial_success_rate_best_value'] - testing_best_dict['trial_success_rate_best_value']
            if test_diff > 0.0 or (abs(test_diff) < 2.0 and testing_best_dict['action_efficiency_best_value'] - eff_testing_best_dict['action_efficiency_best_value'] > 10.0):
                # keep the better of the saved models
                testing_best_dict = eff_testing_best_dict
                testing_dest_dir = eff_testing_dest_dir

        if not args.place:
            print('Challenging Arrangements Preset Testing Complete! Dir: ' + preset_testing_dest_dir)
            print('Challenging Arrangements Preset Testing results: \n ' + str(preset_testing_best_dict))

        print('Random Testing Complete! Dir: ' + testing_dest_dir)
        print('Random Testing results: \n ' + str(testing_best_dict))
        #  --is_testing --random_seed 1238 --snapshot_file '/home/ahundt/src/real_good_robot/logs/2020-02-02-20-29-27_Sim-Push-and-Grasp-Two-Step-Reward-Training/models/snapshot.reinforcement.pth'  --max_test_trials 10 --test_preset_cases

    print('Training Complete! Dir: ' + training_base_directory)
    print('Training results: \n ' + str(best_dict))
    return training_base_directory, best_dict, testing_dest_dir, testing_best_dict

def ablation(args):

    ablation_dir = utils.mkdir_p(os.path.join('logs', 'ablation'))
    ablation_summary_json = os.path.join(ablation_dir, 'ablation.json')
    ablation_summary = {}
    if os.path.exists(ablation_summary_json):
        with open(ablation_summary_json, 'r') as f:
            ablation_summary.update(json.load(f))
    args_run_one = copy.deepcopy(args)

    run_name = 'two step training (no task progress) Baseline case'
    args_run_one.no_height_reward = True
    # export CUDA_VISIBLE_DEVICES="0" && python main.py --is_sim --obj_mesh_dir objects/blocks --num_obj 8 --push_rewards --experience_replay --explore_rate_decay --save_visualizations --tcp_port 19998 --place --check_z_height --max_train_actions 10000
    # --no_height_reward
    training_base_directory, best_dict, training_dest_dir, testing_best_dict = one_train_test_run(args_run_one)
    preset_training_dest_dir = shutil.move(training_base_directory, ablation_dir)

    # SPOT, no masking, no SPOT-Q "No Reversal" (basic task progress)
    # export CUDA_VISIBLE_DEVICES="0" && python main.py --is_sim --obj_mesh_dir objects/blocks --num_obj 8 --push_rewards --experience_replay --explore_rate_decay --save_visualizations --tcp_port 19998 --place --check_z_height --max_train_actions 10000
    # --no_height_reward

    # SPOT, no masking, Trial Reward

    # SPOT, masking,
    args_run_one.common_sense = True

    # SPOT, masking, FULL FEATURED RUN
    args_run_one.common_sense = True

if __name__ == '__main__':
    # workaround matplotlib plotting thread crash https://stackoverflow.com/a/29172195
    # matplotlib.use('Agg')

    # Parse arguments
    parser = argparse.ArgumentParser(description='Train robotic agents to learn how to plan complementary pushing, grasping, and placing as well as multi-step tasks for manipulation with deep reinforcement learning in PyTorch.')

    # --------------- Setup options ---------------
    parser.add_argument('--is_sim', dest='is_sim', action='store_true', default=False,                                    help='run in simulation?')
    parser.add_argument('--obj_mesh_dir', dest='obj_mesh_dir', action='store', default='objects/blocks',                  help='directory containing 3D mesh files (.obj) of objects to be added to simulation')
    parser.add_argument('--num_obj', dest='num_obj', type=int, action='store', default=10,                                help='number of objects to add to simulation')
    parser.add_argument('--num_extra_obj', dest='num_extra_obj', type=int, action='store', default=0,                     help='number of secondary objects, like distractors, to add to simulation')
    parser.add_argument('--goal_num_obj', dest='goal_num_obj', type=int, action='store', default=4,                       help='max height of the desired stack. Set when not using extra objects with language command')
    parser.add_argument('--tcp_host_ip', dest='tcp_host_ip', action='store', default='192.168.1.155',                     help='IP address to robot arm as TCP client (UR5)')
    parser.add_argument('--tcp_port', dest='tcp_port', type=int, action='store', default=30002,                           help='port to robot arm as TCP client (UR5)')
    parser.add_argument('--rtc_host_ip', dest='rtc_host_ip', action='store', default='192.168.1.155',                     help='IP address to robot arm as real-time client (UR5)')
    parser.add_argument('--rtc_port', dest='rtc_port', type=int, action='store', default=30003,                           help='port to robot arm as real-time client (UR5)')
    parser.add_argument('--heightmap_resolution', dest='heightmap_resolution', type=float, action='store', default=0.002, help='meters per pixel of heightmap')
    parser.add_argument('--random_seed', dest='random_seed', type=int, action='store', default=1234,                      help='random seed for simulation and neural net initialization')
    parser.add_argument('--cpu', dest='force_cpu', action='store_true', default=False,                                    help='force code to run in CPU mode')
    parser.add_argument('--flops', dest='flops', action='store_true', default=False,                                      help='calculate floating point operations of a forward pass then exit')
    parser.add_argument('--show_heightmap', dest='show_heightmap', action='store_true', default=False,                    help='show the background heightmap for collecting a new one and debugging')
    parser.add_argument('--timeout', dest='timeout', type=int, default=60,                                                help='time to wait before environment reset')

    # ------------- Algorithm options -------------
    parser.add_argument('--method', dest='method', action='store', default='reinforcement',                               help='set to \'reactive\' (supervised learning) or \'reinforcement\' (reinforcement learning ie Q-learning)')
    parser.add_argument('--push_rewards', dest='push_rewards', action='store_true', default=False,                        help='use immediate rewards (from change detection) for pushing?')
    parser.add_argument('--future_reward_discount', dest='future_reward_discount', type=float, action='store', default=0.5)
    parser.add_argument('--experience_replay', dest='experience_replay', action='store_true', default=False,              help='use prioritized experience replay?')
    parser.add_argument('--heuristic_bootstrap', dest='heuristic_bootstrap', action='store_true', default=False,          help='use handcrafted grasping algorithm when grasping fails too many times in a row during training?')
    parser.add_argument('--explore_rate_decay', dest='explore_rate_decay', action='store_true', default=False)
    parser.add_argument('--grasp_only', dest='grasp_only', action='store_true', default=False)
    parser.add_argument('--check_row', dest='check_row', action='store_true', default=False,                              help='check for placed rows instead of stacks')
    parser.add_argument('--random_weights', dest='random_weights', action='store_true', default=False,                    help='use random weights rather than weights pretrained on ImageNet')
    parser.add_argument('--max_iter', dest='max_iter', action='store', type=int, default=-1,                              help='Uncommon flag, we recommend --max_train_actions to train then test in one go. Single run max iter for training. -1 (default) trains indefinitely.')
    parser.add_argument('--random_trunk_weights_max', dest='random_trunk_weights_max', type=int, action='store', default=0,                      help='Max Number of times to randomly initialize the model trunk before starting backpropagaion. 0 disables this feature entirely, we have also tried 10 but more experiments are needed.')
    parser.add_argument('--random_trunk_weights_reset_iters', dest='random_trunk_weights_reset_iters', type=int, action='store', default=0,      help='Max number of times a randomly initialized model should be run without success before trying a new model. 0 disables this feature entirely, we have also tried 10 but more experiements are needed.')
    parser.add_argument('--random_trunk_weights_min_success', dest='random_trunk_weights_min_success', type=int, action='store', default=4,      help='The minimum number of successes we must have reached before we keep an initial set of random trunk weights.')
    parser.add_argument('--place', dest='place', action='store_true', default=False,                                      help='enable placing of objects')
    parser.add_argument('--skip_noncontact_actions', dest='skip_noncontact_actions', action='store_true', default=False,  help='enable skipping grasp and push actions when the heightmap is zero')
    parser.add_argument('--common_sense', dest='common_sense', action='store_true', default=False,                        help='Use common sense heuristics to detect and train on regions which do not contact anything, and will thus not result in task progress.')
    parser.add_argument('--no_height_reward', dest='no_height_reward', action='store_true', default=False,                help='disable stack height reward multiplier')
    parser.add_argument('--grasp_color_task', dest='grasp_color_task', action='store_true', default=False,                help='enable grasping specific colored objects')
    parser.add_argument('--transfer_grasp_to_place', dest='transfer_grasp_to_place', action='store_true', default=False,  help='Load the grasping weights as placing weights.')
    parser.add_argument('--check_z_height', dest='check_z_height', action='store_true', default=False,                    help='use check_z_height instead of check_stacks for any stacks')
    # TODO(ahundt) determine a way to deal with the side effect
    parser.add_argument('--trial_reward', dest='trial_reward', action='store_true', default=False,                        help='Experience replay delivers SPOT Trial rewards for the whole trial, not just next step. Decay rate is future_reward_discount.')
    parser.add_argument('--discounted_reward', dest='discounted_reward', action='store_true', default=False,                        help='Experience replay delivers a standard discounted reward aka decaying reward, with the decay rate set by current_reward_t = future_reward_discount * future_reward_t_plus_1, the final reward is set by the regular spot (non-trial) reward. With this parameter we suggest setting --future_reward_discount 0.9')
    parser.add_argument('--disable_two_step_backprop', dest='disable_two_step_backprop', action='store_true', default=False,                        help='There is a local two time step training and backpropagation which does not precisely match trial rewards, this flag disables it. ')
    parser.add_argument('--check_z_height_goal', dest='check_z_height_goal', action='store', type=float, default=4.0,          help='check_z_height goal height, a value of 2.0 is 0.1 meters, and a value of 4.0 is 0.2 meters')
    parser.add_argument('--check_z_height_max', dest='check_z_height_max', action='store', type=float, default=6.0,          help='check_z_height max height above which a problem is detected, a value of 2.0 is 0.1 meters, and a value of 6.0 is 0.4 meters')
    parser.add_argument('--disable_situation_removal', dest='disable_situation_removal', action='store_true', default=False,                        help='Disables situation removal, where rewards are set to 0 and a reset is triggered upon reversal of task progress. Automatically enabled when is_testing is enable.')
    parser.add_argument('--no_common_sense_backprop', dest='no_common_sense_backprop', action='store_true', default=False,                        help='Disables backprop on masked actions, to evaluate SPOT-Q RL algorithm.')
    parser.add_argument('--random_actions', dest='random_actions', action='store_true', default=False,                              help='By default we select both the action type randomly, like push or place, enabling random_actions will ensure the action x, y, theta is also selected randomly from the allowed regions.')
    parser.add_argument('--depth_channels_history', dest='depth_channels_history', action='store_true', default=False, help='Use 2 steps of history instead of replicating depth values 3 times during training/testing')
    parser.add_argument('--use_demo', dest='use_demo', action='store_true', default=False, help='Use demonstration to chose action')
    parser.add_argument('--task_type', dest='task_type', type=str, default=None)
    parser.add_argument('--primitive_distance_method', dest='primitive_distance_method', type=str, default='l2')
    # TODO(adit98) clarify argument name?
    parser.add_argument('--cycle_consistency', dest='cycle_consistency', action='store_true', default=False, help='Use cycle consistency for action matching')

    # Language Mask Options
    parser.add_argument('--is_bisk', dest = 'is_bisk', action='store_true', default = False,                                help='running on bisk (2018) AAAI dataset')
    parser.add_argument('--static_language_mask', dest='static_language_mask', action='store_true', default=False,          help='enable usage of a static transformer model to inform robot grasp and place.')
    parser.add_argument('--baseline_language_mask', dest='baseline_language_mask', action='store_true', default=False,      help='set to true to run random baseline for language experiments, where language mask allows everything')
    parser.add_argument('--train_language_inputs', dest='train_language_inputs', type=str, default='blocks_data/trainset_v2.json',                   help='specify the language data file to use during reinforcement learning')
    parser.add_argument('--language_model_config', dest='language_model_config', type=str, default='blocks_data/config.yaml', help='relative path to the yaml file containing the model hyperparameters')
    parser.add_argument('--language_model_weights', dest='language_model_weights', type=str, default='blocks_data/best.th', help='file containing the language model weights (*.th) as a state dict.')
    parser.add_argument('--separation_threshold', dest='separation_threshold', type=float, default=0.02, help = "threshold distance between blocks to consider them in a row")
    parser.add_argument('--distance_threshold', dest='distance_threshold', type=float, default=0.02, help = "vertical threshold distance between blocks to consider them in a row")

    # -------------- Testing options --------------
    parser.add_argument('--is_testing', dest='is_testing', action='store_true', default=False)
    parser.add_argument('--unstack', dest='unstack', action='store_true', default=False,                                   help='Simulator will reset block positions by unstacking rather than by randomly setting their positions. Only applies when --place is set')
    parser.add_argument('--evaluate_random_objects', dest='evaluate_random_objects', action='store_true', default=False,                help='Evaluate trials with random block positions, for example testing frequency of random rows.')
    parser.add_argument('--max_test_trials', dest='max_test_trials', type=int, action='store', default=100,                help='maximum number of test runs per case/scenario')
    parser.add_argument('--max_train_actions', dest='max_train_actions', type=int, action='store', default=None,                help='INTEGRATED TRAIN VAL TEST - maximum number of actions before training exits automatically at the end of that trial. Note this is slightly different from max_iter.')
    parser.add_argument('--max_trial_actions_train', dest='max_trial_actions_train', type=int, action='store', default=100, help='Number of actions after which to reset environment if trial is not completed')
    parser.add_argument('--max_trial_actions_test', dest='max_trial_actions_test', type=int, action='store', default=30, help='Number of actions after which to reset environment if trial is not completed')
    parser.add_argument('--test_preset_cases', dest='test_preset_cases', action='store_true', default=False)
    parser.add_argument('--test_preset_file', dest='test_preset_file', action='store', default='')
    parser.add_argument('--test_preset_dir', dest='test_preset_dir', action='store', default='simulation/test-cases/')
    parser.add_argument('--show_preset_cases_then_exit', dest='show_preset_cases_then_exit', action='store_true', default=False,    help='just show all the preset cases so you can have a look, then exit')
    parser.add_argument('--ablation', dest='ablation', nargs='?', default=None, const='new',    help='Do a preconfigured ablation study of different algorithms. If not specified, no ablation, if --ablation, a new ablation is run, if --ablation <path> an existing ablation is resumed.')
    parser.add_argument('--end_on_incorrect_order', action='store_true', default=False,          help='set if trial should end immediately whenever blocks are placed in incorrect order')
    # ------ Pre-loading and logging options ------
    parser.add_argument('--snapshot_file', dest='snapshot_file', action='store', default='',                              help='snapshot file to load for the model')
    parser.add_argument('--stack_snapshot_file', dest='stack_snapshot_file', action='store', default='',                  help='multi model stacking snapshot file to load for the model (use --snapshot_file if you are training one model)')
    parser.add_argument('--row_snapshot_file', dest='row_snapshot_file', action='store', default='',                      help='multi model row making snapshot file to load for the model (use --snapshot_file if you are training one model)')
    parser.add_argument('--vertical_square_snapshot_file', dest='vertical_square_snapshot_file', action='store', default='', help='multi model vertical_square making snapshot file to load for the model (use --snapshot_file if you are training one model)')
    parser.add_argument('--unstack_snapshot_file', dest='unstack_snapshot_file', action='store', default='',              help='multi model unstack making snapshot file to load for the model (use --snapshot_file if you are training one model)')
    parser.add_argument('--nn', dest='nn', action='store', default='densenet',                                            help='Neural network architecture choice, options are efficientnet, densenet')
    parser.add_argument('--num_dilation', dest='num_dilation', type=int, action='store', default=0,                       help='Number of dilations to apply to efficientnet, each increment doubles output resolution and increases computational expense.')
    parser.add_argument('--resume', dest='resume', nargs='?', default=None, const='last',                                 help='resume a previous run. If no run specified, resumes the most recent')
    parser.add_argument('--save_visualizations', dest='save_visualizations', action='store_true', default=False,          help='save visualizations of FCN predictions? Costs about 0.6 seconds per action.')
    parser.add_argument('--plot_window', dest='plot_window', type=int, action='store', default=500,                       help='Size of action time window to use when plotting current training progress. The testing mode window is set automatically.')
    parser.add_argument('--demo_path', dest='demo_path', type=str, default=None)

    # Parse args
    args = parser.parse_args()


    # if use_demo is specified, we need a demo_path
    if args.use_demo and args.demo_path is None:
        raise ValueError('Must specify --demo_path if --use_demo is set')

    if not args.ablation:
        one_train_test_run(args)
    else:
        ablation(args)
