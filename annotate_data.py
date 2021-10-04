import numpy as np
import json
import pathlib
import torch
import cv2
import re
import copy
import pdb
from matplotlib import pyplot as plt
import matplotlib.patches as patches
from IPython.display import clear_output
from tqdm import tqdm 
from skimage.util import random_noise

def check_success(data, idx):
    return data[idx][0] == 1

class Pair:
    def __init__(self, prev_image, prev_location, next_image, next_location, resolution = 224, w = 40, is_row = True,
                 prev_image_for_inference = None,
                 next_image_for_inference = None,
                 long_command = None):
        self.prev_image = prev_image
        self.prev_location = prev_location
        self.next_image = next_image
        self.next_location = next_location 
        self.w = w 
        self.source_code = None 
        self.source_location = None
        self.target_code = None
        self.target_location = None
        self.relation_code = None
        self.resolution = resolution
        self.prev_state_image = None
        self.next_state_image = None
        self.prev_image_for_inference = prev_image_for_inference
        self.next_image_for_inference = next_image_for_inference
        self.long_command = long_command
    
        self.json_data = None
        self.is_row = is_row

    def show(self):
        fig,ax = plt.subplots(1)
        ax.imshow(self.prev_image_for_inference[:,:,0:3])
        prev_location = self.prev_location - int(self.w/2)
        next_location = self.next_location - int(self.w/2)
        rect = patches.Rectangle(prev_location, self.w, self.w ,linewidth=3,edgecolor='w',facecolor='none')
        # ax.add_patch(rect)
        plt.show()
        fig,ax = plt.subplots(1)
        ax.imshow(self.next_image_for_inference[:,:,0:3])
        rect = patches.Rectangle(next_location, self.w, self.w,linewidth=3,edgecolor='w',facecolor='none')
        # ax.add_patch(rect)
        plt.show()

    def resize(self):

        prev_image, prev_depth = self.prev_image[:,:,0:3], self.prev_image[:,:,3:]
        prev_image = cv2.resize(prev_image, (self.resolution,self.resolution), interpolation = cv2.INTER_AREA)
        prev_depth = cv2.resize(prev_depth, (self.resolution,self.resolution), interpolation = cv2.INTER_AREA)
        self.prev_image = np.concatenate([prev_image, prev_depth], axis=-1)

        if self.next_image is not None:
            next_image, next_depth = self.next_image[:,:,0:3], self.next_image[:,:,3:]
            next_image = cv2.resize(next_image, (self.resolution,self.resolution), interpolation = cv2.INTER_AREA)
            next_depth = cv2.resize(next_depth, (self.resolution,self.resolution), interpolation = cv2.INTER_AREA)
            self.next_image = np.concatenate([next_image, next_depth], axis=-1)

        self.ratio = self.resolution / 224

        # don't double-resize
        if self.prev_state_image is not None and self.prev_state_image.shape[0] != self.resolution:
            self.prev_state_image = (np.tile(self.prev_state_image, (1,1,3))/4) * 255
            self.prev_state_image = cv2.resize(self.prev_state_image, (self.resolution,self.resolution), interpolation = cv2.INTER_NEAREST)
            self.prev_state_image = (self.prev_state_image / 255) * 4
            self.prev_state_image = self.prev_state_image[:,:,0].astype(int)
            assert(np.sum(self.prev_state_image) > 0)

            if self.prev_location is not None:
                self.prev_location = self.prev_location.astype(float).copy() * self.ratio
                self.prev_location = self.prev_location.astype(int)
            if self.next_location is not None:
                self.next_location = self.next_location.astype(float).copy() * self.ratio
                self.next_location = self.next_location.astype(int)

        # normalize location and width
        self.w *= self.ratio
        self.w = int(self.w)


    def get_mask(self, location):
        w, h, __ = self.prev_image.shape
        mask = np.zeros((1, w, h))
        start = location - int(self.w/2)
        start = start.astype(int)
        mask[:, start[1]: start[1] + self.w, start[0]: start[0] + self.w] = 1
        return mask

    @classmethod
    def from_idxs(cls, grasp_idx, place_idx, data, image_home, is_row = True, w = 40, long_command = None ):
        prev_location = data[grasp_idx][2:][::-1]
        next_location = data[place_idx][2:][::-1]
        grasp_prefix = str(1000000 + grasp_idx)[1:]
        place_prefix = str(1000000 + place_idx)[1:]
        depth_home = image_home.parent.joinpath("depth-heightmaps")
        grasp_color_path = str(image_home.joinpath(f"{grasp_prefix}.0.color.png"))
        place_color_path = str(image_home.joinpath(f"{place_prefix}.2.color.png"))
        grasp_depth_path = str(depth_home.joinpath(f"{grasp_prefix}.0.depth.png"))
        place_depth_path = str(depth_home.joinpath(f"{place_prefix}.2.depth.png"))

        prev_image = cv2.imread(grasp_color_path)
        prev_image = cv2.cvtColor(prev_image, cv2.COLOR_BGR2RGB)
        prev_image_for_inference = prev_image.copy() 
        prev_depth = cv2.imread(grasp_depth_path, -1)
        prev_depth = prev_depth.astype(np.float32)/100000
        prev_depth = np.stack([prev_depth] * 3, axis=-1)
        #prev_depth = cv2.cvtColor(prev_depth, cv2.COLOR_BGR2RGB)
        next_image = cv2.imread(place_color_path)
        next_image = cv2.cvtColor(next_image, cv2.COLOR_BGR2RGB)
        next_image_for_inference = next_image.copy() 
        next_depth = cv2.imread(place_depth_path, -1)
        next_depth = next_depth.astype(np.float32)/100000
        next_depth = np.stack([next_depth] * 3, axis=-1)
        #next_depth = cv2.cvtColor(next_depth, cv2.COLOR_BGR2RGB)

        prev_image = np.concatenate([prev_image, prev_depth], axis=-1)
        next_image = np.concatenate([next_image, next_depth], axis=-1)

        return cls(prev_image, prev_location, next_image, next_location, is_row = is_row, w = w,
                    prev_image_for_inference = prev_image_for_inference, 
                    next_image_for_inference = next_image_for_inference,
                    long_command = long_command)
        

    @classmethod
    def from_sim_idxs(cls, grasp_idx, place_idx, data, image_home, json_home, is_row=True, w = 40, filter_colors = False, long_command = None): 
        pair = Pair.from_idxs(grasp_idx, place_idx, data, image_home, is_row = is_row, w = w, long_command = long_command)
        # annotate based on sim data 
        grasp_json_path = json_home.joinpath(f"object_positions_and_orientations_{grasp_idx}_0.json")
        place_json_path = json_home.joinpath(f"object_positions_and_orientations_{place_idx}_2.json")
        json_data = Pair.read_json(grasp_json_path)
        src_color, tgt_color = pair.combine_json_data(json_data, filter_colors = filter_colors)
        if src_color is None or tgt_color is None:
            return None 
        pair.json_data = json_data
        return pair

    @classmethod
    def from_main_idxs(cls, prev_image, prev_heightmap, prev_json, stack_sequence, is_row = True):
        # (elias) infer which block to move from interpolation here
        prev_image = np.concatenate([prev_image, prev_heightmap], axis=-1)
        pair = cls(prev_image, None, None, None, is_row = is_row)
        json_data = Pair.read_json(prev_json)
        pair.json_data = json_data
        src_color, tgt_color = pair.infer_from_stacksequence(stack_sequence)
        return pair

    def infer_from_stacksequence(self, stack_sequence):
        src_idx = stack_sequence.object_color_index
        src_color_idx = stack_sequence.object_color_sequence[src_idx]
        src_color = stack_sequence.color_names[src_color_idx]
        try:
            assert(src_idx >= 0)
            tgt_idx = src_idx - 1
            tgt_color_idx = stack_sequence.object_color_sequence[tgt_idx]
        except (AssertionError, IndexError) as e:
            raise ValueError(f"StackSequence error: object asked for doesn't exist")

        tgt_color = stack_sequence.color_names[tgt_color_idx]
        self.source_code = src_color
        self.target_code = tgt_color
        return src_color, tgt_color

    @staticmethod
    def read_json(json_path):
        if type(json_path) == dict:
            data = json_path
        else:
            with open(json_path) as f1:
                data = json.load(f1)
        num_blocks = data['num_obj']
        colors = data['color_names'][0:num_blocks]
        coords = data['positions']
        assert(len(coords) == num_blocks)
        assert(len(coords[0]) == 3)
        to_ret = {}
        for color, coord in zip(colors, coords):
            # normalize location to resolution
            coord = np.array(coord)
            to_ret[color] = coord
        return to_ret

    def get_moved_block(self, prev_coords, next_coords):
        diff = {k: prev_coords[k][0:2] - next_coords[k][0:2] for k in prev_coords.keys()}
        diff = [(k, np.sum(x)) for k, x in diff.items()]
        # get block with greatest diff in location
        return list(sorted(diff, key = lambda x: x[1]))[-1]

    def make_image(self, json_data):
        state = np.zeros((self.resolution, self.resolution, 1))
        def convert_to_loc(state):
            offset = [0.15, 0.0, 0.0]
            grid_dim = 14
            side_len = 0.035
            x_offset = 0.58
            grid_len = grid_dim*side_len
            state[0] += x_offset
            for i in range(len(state)):
                state[i] = (state[i] * 2) / grid_len - offset[i]
            state = (state + 1)/2 * self.resolution
            return state.astype(int)

        color_to_idx = {"red":1, "blue": 2, "green": 3, "yellow": 4, "brown": 5, "orange": 6, "gray": 7, "purple": 8, "cyan": 9, "pink": 10}
        for color, location in json_data.items():
            idx = color_to_idx[color]
            loc = convert_to_loc(location)
            block_width = 15
            for i in range(loc[0]-block_width, loc[0] + block_width):
                for j in range(loc[1] - block_width, loc[1] + block_width):
                    try:
                        state[j, i, :] = idx
                    except IndexError:
                        continue

        return state

    @staticmethod
    def euclidean_distance(a, b):
        if type(a) == tuple:
            a = np.array(a)
        if type(b) == tuple:
            b = np.array(b)
        return np.linalg.norm(a-b)

    def get_most_common_color(self, color_swatch, same_thresh = 1):
        count_dict = {}
        for i in range(0, color_swatch.shape[0]):
            for j in range(0, color_swatch.shape[1]):
                color = color_swatch[i,j,:]
                already_in = False
                for k in count_dict.keys():
                    if Pair.euclidean_distance(k,color) < same_thresh:
                        count_dict[k] += 1
                        already_in = True
                if not already_in:
                    color = tuple([x for x in color])

                    count_dict[color] = 1

        count_dict_items = count_dict.items()
        most_freq = sorted(count_dict_items, key = lambda x:x[1])[-1][0]
        return most_freq

    def filter_color_against_keycolors(self, color_name, pred_loc, image):
        # check whether color names and actual color match up, filter out examples where they don't 
        # prototypical colors: 
        color_name_dict = { (217,74,76) : "red",
          (76,137,68) :"green",
          (67,103,142) :"blue",
          (202,171,62) :"yellow",
          (206,121,37) : "orange",
          (158,148,146) :"gray",
          (131,100,81) :"brown",
          (148,104,137) :"purple",
          (45,45,45): "background",
          (0,0,0): "shadow"}
        # get most common color in color swatch 
        image = image[:,:,0:3]
        pred_loc = pred_loc.astype(int)
        pred_color_swatch = image[pred_loc[1]: pred_loc[1] + self.w, pred_loc[0]: pred_loc[0] + self.w, :] 
        pred_color = self.get_most_common_color(pred_color_swatch) 
        distances = [(name, Pair.euclidean_distance(k,pred_color)) for k,name in  color_name_dict.items()]

        pred_color_name = sorted(distances, key = lambda x:x[1])[0]
        return pred_color_name[0] == color_name, pred_color_name[0]

    def combine_json_data(self, json_data, next_to=True, filter_left=False, filter_colors=True): 
        # first pass: all prompts say "next to" and reference the closest block to the left of the target location
        # if no such block exists, skip for now
        def euclid_dist(p1, p2):
            total = 0
            for i in range(len(p1)):
                total += (p1[i] - p2[i])**2
            return np.sqrt(total)

        # find block closest to grasp index
        # min_grasp_color = self.get_moved_block(prev_json_data, next_json_data)
        def convert_loc(loc):
            loc = ((loc / 224) * 2) - 1
            offset = [0.15, 0.0, 0.0]
            grid_dim = 14
            side_len = 0.035
            x_offset = 0.58
            grid_len = grid_dim*side_len
            for i in range(len(loc)):
                loc[i] = (loc[i] + offset[i]) * grid_len/2
            loc[0] -= x_offset
            return loc

        def convert_to_loc(state):
            offset = [0.15, 0.0, 0.0]
            grid_dim = 14
            side_len = 0.035
            x_offset = 0.58
            grid_len = grid_dim*side_len
            state[0] += x_offset
            for i in range(len(state)):
                state[i] = (state[i] * 2) / grid_len - offset[i]
            state = (state + 1)/2 * 224
            return state.astype(int)

        assert(np.sum(convert_loc(convert_to_loc(np.array([-0.43000549,  0.08394134,  0.02593102]))) - np.array([-0.43000549,  0.08394134,  0.02593102])) < 0.01)
        json_data_new = copy.deepcopy(json_data)
        self.prev_state_image = self.make_image(json_data)

        #prev_loc = convert_loc(self.prev_location)
        #next_loc = convert_loc(self.next_location)
        if self.is_row:
            json_data_new = {k:convert_to_loc(v)[0:2] for k, v in json_data_new.items() }
        else:
            json_data_new = {k:convert_to_loc(v) for k, v in json_data_new.items() }

        grasp_dists = [(x[0], euclid_dist(self.prev_location, x[1])) for x in json_data_new.items()]
        min_grasp_color = list(sorted(grasp_dists, key = lambda x:x[1]))[0][0]

        remaining_blocks = [x for x in json_data_new.items() if x[0] != min_grasp_color]
        remaining_blocks_before = [x for x in remaining_blocks]
        if not self.is_row:
            # if we're building a stack, restrict to the highest blocks
            thresh = 10
            heights = [x[1][-1] for x in remaining_blocks]
            max_height = max(heights)
            #place_height = self.next_location[-1]
            # soft match blocks within a threshold of 10 pixels of the place location
            remaining_blocks = [x for x in remaining_blocks if np.abs(x[1][-1] - max_height) < thresh ]

        # find block closest to place index
        place_dists = [(x[0], euclid_dist(self.next_location, x[1])) for x in remaining_blocks]
        sorted_place_dists = sorted(place_dists, key = lambda x:x[1])
        match_thresh = 25
        if not self.is_row:
            # if the height restriction was too restrictive, back off to flat match
            if (len(sorted_place_dists) == 0 or sorted_place_dists[0][1] > match_thresh):
                print(f"BACKING OFF to x-z only")
                remaining_blocks = remaining_blocks_before
                place_dists = [(x[0], euclid_dist(self.next_location, x[1])) for x in remaining_blocks]
                sorted_place_dists = sorted(place_dists, key = lambda x:x[1])

        min_place_color = list(sorted_place_dists)[0][0]
        # get relation between place location and place block
        self.source_code = min_grasp_color
        self.target_code = min_place_color

        self.relation_code = "next_to"

        if filter_colors: 
            # use previous image for both so it's not covered up by placed block 
            place_color_correct, pred_place_color = self.filter_color_against_keycolors(min_place_color, self.next_location, self.prev_image_for_inference)
            grasp_color_correct, pred_grasp_color = self.filter_color_against_keycolors(min_grasp_color, self.prev_location, self.prev_image_for_inference)
            if not place_color_correct or not grasp_color_correct:
                #print(f"place color: {min_place_color} vs inferred {pred_place_color}, grasp color: {min_grasp_color} vs inferred {pred_grasp_color}")
                #self.show()
                #pdb.set_trace() 
                return None, None

        return min_grasp_color, min_place_color  

    def clean(self):
        # re-order codes so that "top", "bottom", come bfore "left" "right"
        if self.source_location == "none":
            self.source_location = "n"
        if self.target_location == "none":
            self.target_location = "n"
        if self.source_location == "as":
            self.source_location = "sa"
        if self.target_location == "as":
            self.target_location = "sa"
        if self.source_location == "ds":
            self.source_location = "sd"
        if self.target_location == "ds":
            self.target_location = "sd"
        if self.source_location == "aw":
            self.source_location = "wa"
        if self.target_location == "aw":
            self.target_location = "wa"
        if self.source_location == "dw":
            self.source_location = "wd"
        if self.target_location == "dw":
            self.target_location = "wd"
        if self.relation_code == "ds":
            self.relation_code = "sd"
        if self.relation_code == "wa":
            self.relation_code = "aw"
        if self.relation_code == "dw":
            self.relation_code = "wd"
        if self.relation_code == "sa":
            self.relation_code = "as"

    def generate(self):
        self.clean()
        try:
            if self.long_command is None:
                return self.generate_normal()
            else:
                return self.generate_long_command() 
        except AttributeError:
            return self.generate_normal()


    def generate_normal(self):
        location_lookup_dict = {"w": "top", "d": "right", "a": "left", "s": "bottom", "n":""}

        location_lookup_fxn = lambda x: " ".join([location_lookup_dict[y] for y in list(x)])
        relation_lookup_dict = {"on": "on top of",
                                "next_to": "next to",
                                "w": "over",
                                "s": "under",
                                "a": "to the left of",
                                "d": "to the right of",
                                "aw": "up and to the left of",
                                "as": "down and to the left of",
                                "wd": "up and to the right of",
                                "sd": "down and to the right of"}

        #stack_template = "stack the {source_location} {source_color} block {relation} the {target_location} {target_color} block"
        
        #stack_template = "stack the {source_color} block {relation} the {target_color} block"
        stack_template = "move the {source_color} block {relation} the {target_color} block"
        row_template = "move the {source_color} block {relation} the {target_color} block"

        # is stacking task
        try:
            if self.is_row:
                return row_template.format(source_color = self.source_code,
                                        target_color = self.target_code,
                                        relation = "next to")
                                        # relation = relation_lookup_dict[self.relation_code])
            else:
                return stack_template.format(source_color = self.source_code,
                                            target_color = self.target_code,
                                            relation = "on")
        except KeyError:
            return "bad"

    def generate_long_command(self):
        stack_template = "make a stack of the {color0}, {color1}, {color2}, and {color3} blocks. {source_color}"
        row_template = "make a row of the {color0}, {color1}, {color2}, and {color3} blocks. {source_color}"


        if self.is_row:
            return row_template.format(color0 = self.long_command[0],
                                       color1 = self.long_command[1],
                                       color2 = self.long_command[2],
                                       color3 = self.long_command[3],
                                       source_color = self.source_code)
        else:
            return stack_template.format(color0 = self.long_command[0],
                                       color1 = self.long_command[1],
                                       color2 = self.long_command[2],
                                       color3 = self.long_command[3],
                                       source_color = self.source_color)


def rotate_pair(pair, deg):
    pair.clean()
    assert(deg in [1,2,3])


    new_pair = copy.deepcopy(pair)

    def rotate_image(img):
        for i in range(deg):
            img = cv2.rotate(img, cv2.cv2.ROTATE_90_CLOCKWISE)
        return img

    def rotate_coords(coords):
        # put back into unit square
        coords = ((coords/new_pair.resolution) * 2)-1
        x, y = coords
        x, y = -y, x
        coords = np.array([x,y])
        coords = (coords + 1)/2 * new_pair.resolution
        return coords

    new_pair.prev_image = rotate_image(new_pair.prev_image)
    new_pair.next_image = rotate_image(new_pair.next_image)
    new_pair.prev_location = rotate_coords(new_pair.prev_location)
    new_pair.next_location = rotate_coords(new_pair.next_location)
    return new_pair


def gaussian_augment(pair, params):
    mean, var = params
    new_pair = copy.deepcopy(pair)
    new_pair.prev_image = random_noise(new_pair.prev_image, mode='gaussian', mean=mean, var=var, clip=True)
    new_pair.next_image = random_noise(new_pair.next_image, mode='gaussian', mean=mean, var=var, clip=True)
    return new_pair

def flip_pair(pair, axis):
    pair.clean()
    flip_lookup = {1: {"w": "w", "a": "d", "s": "s", "d": "a"},
                   2: {"w": "d", "a": "s", "s": "a", "d": "w"},
                   3: {"w": "s", "a": "a", "s": "w", "d": "d"},
                   4: {"w": "a", "a": "w", "s": "d", "d": "s"}}

    def replace(code):
        code = list(code)
        if code[0] == "n":
            return "".join(code)
        try:
            code = [flip_lookup[axis][x] for x in code]
        except:
            pdb.set_trace()
        return "".join(code)

    new_pair = copy.deepcopy(pair)
    if new_pair.source_location is not None and new_pair.source_location != "none":
        new_pair.source_location = replace(new_pair.source_location)
    if new_pair.target_location is not None and new_pair.target_location != "none":
        new_pair.target_location = replace(new_pair.target_location)
    if new_pair.relation_code != "on":
        new_pair.relation_code = replace(new_pair.relation_code)

    def flip_image(img):
        if axis == 1:
            # vertical flip
            flipped_img = cv2.flip(img, 1)
        elif axis == 3:
            # horizontal flip
            flipped_img = cv2.flip(img, 0)
        elif axis == 2:
            # along backward diag
            flipped_img = np.transpose(np.rot90(img,2, axes=(0,1)), axes = (1,0,2))
        elif axis == 4:
            # along regular diag
            flipped_img = np.transpose(img, axes = (1,0,2))
        else:
            raise AssertionError("Axis must be one of [1,2,3,4]")
        return flipped_img

    def flip_coords(coords):
        max = 224
        if axis == 1:
            # x coord flips
            coords[0] = 224 - coords[0]
        elif axis == 2:
            # transpose and rotate
            coords[0], coords[1] = coords[1], coords[0]
            coords[0] = 224 - coords[0]
            coords[1] = 224 - coords[1]
        elif axis == 3:
            # y coord flips
            coords[1] = 224 - coords[1]
        elif axis == 4:
            # transpose
            coords[0], coords[1] = coords[1], coords[0]
        else:
            pass
        return coords

    new_pair.prev_image = flip_image(new_pair.prev_image)
    new_pair.next_image = flip_image(new_pair.next_image)
    new_pair.prev_location = flip_coords(new_pair.prev_location)
    new_pair.next_location = flip_coords(new_pair.next_location)
    if hasattr(new_pair, "prev_state_image"):
        new_pair.prev_state_image = flip_image(new_pair.prev_state_image).reshape(224, 224, 1)
    return new_pair


def get_pairs(data_home, resolution = 224, w = 40, is_sim = False, is_row = True, filter_colors = False, long_command = False):
    # to get whole string:
    # - use clearance to split into separate trials
    # - use existing code to get all grasp/place success pairs from a trial 
    # - keep current code to get correct current action, but add in whole trial history to generate command 


    image_home = data_home.joinpath("data/color-heightmaps")
    if is_sim:
        json_home = data_home.joinpath("data/variables")
    executed_action_path = data_home.joinpath("transitions/executed-action.log.txt")
    place_successes_path = data_home.joinpath("transitions/place-success.log.txt")
    grasp_successes_path = data_home.joinpath("transitions/grasp-success.log.txt")

    if long_command:
        clearance_path = data_home.joinpath("transitions/clearance.log.txt")
        stack_height_path = data_home.joinpath("transitions/stack-height.log.txt")


    kwargs = {'delimiter': ' ', 'ndmin': 2}
    executed_action_data = np.loadtxt(executed_action_path, **kwargs)
    place_succ_data = np.loadtxt(place_successes_path, **kwargs)
    grasp_succ_data = np.loadtxt(grasp_successes_path, **kwargs)

    if long_command:
        # TODO elias: use json data to get final block order 
        clearance_data = np.loadtxt(clearance_path, **kwargs).astype(int)
        stack_height_data = np.loadtxt(stack_height_path, **kwargs).astype(int)
        color_sequences = {}
        trial_start = 0
        for i, trial_end in enumerate(clearance_data):
            trial_end = trial_end[0] - 1
            stack_height = stack_height_data[trial_end][0]
            if stack_height < 4:
                continue 
            place_json_path = json_home.joinpath(f"object_positions_and_orientations_{trial_end}_2.json")
            json_data = Pair.read_json(place_json_path)
            # first get all at lowest position 
            # filter out weird ones with super negative y
            json_data = {k:v for k,v in json_data.items() if v[2] >= 0}
            lowest_y_coord = min([x[2] for x in json_data.values()])
            lowest_colors = [x for x in json_data.items() if abs(x[1][2] - lowest_y_coord) < 0.001]
            lowest_color_names = [x[0] for x in lowest_colors]
            higher_colors = [x for x in json_data.items() if x[0] not in lowest_color_names]

            stack_candidates = {}
            for lcolor, lcoord in lowest_colors:
                stack_candidates[lcolor] = []
                lcoord_xy = lcoord[0:2]
                for hcolor, hcoord in higher_colors:
                    hcoord_xy = hcoord[0:2]
                    lh_dist = Pair.euclidean_distance(lcoord_xy, hcoord_xy)
                    if lh_dist < 0.04:
                        stack_candidates[lcolor].append((hcolor, hcoord))
            try:
                final_candidate = [x for x in stack_candidates.items() if len(x[1]) == 3][0]
            except IndexError:
                print(json_data)
                print(stack_candidates)
                raise IndexError(f"There should be 4 block candidates!")
            colors_sorted = sorted(final_candidate[1], key = lambda x: x[1][2])
            colors_sorted = [x[0] for x in colors_sorted]
            colors_sorted = [final_candidate[0]] + colors_sorted 
            for i in range(trial_start, trial_end + 1):
                color_sequences[i] = colors_sorted
            trial_start = trial_end + 1

    prev_act = None
    prev_grasp_idx = None
    pick_place_pairs = []
    skipped_by_filter, skipped_for_push = 0, 0
    successes = 0
    num_grasps = 0
    grasp_and_success = 0
    for demo_idx in range(len(executed_action_data)):
        ex_act = executed_action_data[demo_idx]
        grasp = False
        if int(ex_act[0]) == 0:
            skipped_push += 1
            continue
        elif int(ex_act[0]) == 1:
            data = grasp_succ_data
            grasp = True
            num_grasps += 1
        elif int(ex_act[0]) == 2:
            data = place_succ_data
        else:
            raise AssertionError(f"action must be of on [0, 1, 2]")

        try:
            was_success = check_success(data, demo_idx) 
            if was_success: 
                successes += 1
        except IndexError:
            print(f"hit end!")
            break

        if prev_act == "grasp":
            # next action must be place if prev was successful grasp
            try:
                assert(not grasp)
            except AssertionError:  
                print(f"double grasp at {demo_idx}")
        if prev_act == "place":
            try:
                assert(grasp)
            except AssertionError:
                print(f"double place at {demo_idx}")

        # sanity checks
        if grasp and was_success:
            prev_act = "grasp"
            prev_grasp_idx = demo_idx
        if not grasp and was_success:
            grasp_and_success += 1
            prev_act = "place"
            # now you can create a pair with the previous action's grasp and current place
            if is_sim:
                if long_command:
                    try:
                        color_sequence = color_sequences[demo_idx]
                    except KeyError:
                        print(max(color_sequences.keys()))
                        print(demo_idx)
                        pdb.set_trace() 
                    pair = Pair.from_sim_idxs(prev_grasp_idx, demo_idx, executed_action_data, image_home, json_home, is_row = is_row, w = w, filter_colors = filter_colors, long_command = color_sequence)
                else:
                    pair = Pair.from_sim_idxs(prev_grasp_idx, demo_idx, executed_action_data, image_home, json_home, is_row = is_row, w = w, filter_colors = filter_colors)
                if pair is None:
                    skipped_by_filter += 1
                    prev_grasp_idx = None 
                    continue 
            else:
                pair = Pair.from_idxs(prev_grasp_idx, demo_idx, executed_action_data, image_home)
            pick_place_pairs.append(pair)
            prev_grasp_idx = None

    print(f"grasp and success {grasp_and_success}")
    print(f"total successes {successes}")
    print(f"total grasps {num_grasps}")

    print(f"skipped for push {skipped_for_push}")
    print(f"skipped {skipped_by_filter} of {len(executed_action_data)}: {skipped_by_filter * 100 / len(executed_action_data):.2f}%")
    return pick_place_pairs

def get_input(prompt, valid_gex):
    var = None
    while var is None:
        inp = input(prompt)
        if valid_gex.match(inp) is not None:
            var = inp
        else:
            continue
    return var

def annotate_pairs(pairs,
                  is_stack = False):
    pairs_with_actions = []

    color_gex = re.compile("(bad)|[rbyg]")
    relation_gex = re.compile("[wasd]{1,2}")
    location_gex = re.compile("[wasd]{1,2}|(none)")
    for p in tqdm(pairs):
        p.show()
        source_color = get_input("Source color: ", color_gex)
        if is_stack:
            source_location = get_input("Source location: ", location_gex)
        target_color = get_input("Target color: ", color_gex)
        if is_stack:
            target_location = get_input("Target location: ", location_gex)

        if not is_stack:
            # if row-making, get position
            relation = get_input("Relation: ", relation_gex)
            target_location, source_location = None, None
        else:
            # stacking only has one
            relation = 'on'

        p.source_code = source_color
        p.target_code = target_color
        p.relation_code = relation
        p.source_location = source_location
        p.target_location = target_location
        p.clean()
        pairs_with_actions.append(p)
        clear_output(wait=True)

    return pairs_with_actions
