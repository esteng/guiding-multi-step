# Data Readme 

Data is organized by experiment and dataset. There are two different file formats. For the first set of experiments (simulated), data is stored as json serialized `annotate_data.Pair` objects, which handle image preprocessing, data augmentation, and template generation. 
The data from Bisk et al. (2018) is stored in the same format as in their paper. For convenience we have included the original dataset in this repo as well as our filtered subset of the dataset. 

## Simulated data 
Each directory in `blocks_data/` contains a file called `pairs.jsonlines`. Each line is a json dump of a Pair object. This file can be loaded as follows: 

```
import json
from annotate_data import Pair 

with open("pairs.jsonlines") as f1:
    lines = f1.readlines()
    data = [Pair.from_jsonline(l) for l in lines]
``` 

### Pair class 
The `Pair` class is a container for simulated grasp/place actions. Each `Pair` is a pair of a grasp and place action, with the corresponding state, image, and location information. 
`Pair`s can be defined over the simulated or real data. For the real data, the `Pair`s were manually annotated. 
Each pair has fields for "previous" information and "next" information, where "previous" corresponds to a grasp action, and "next" corresponds to place actions. 
Pairs hold images (`prev_image`, `next_image`) as well as state information for training. 
They also hold location information (where the grasp or place occurred), and info on what type of action occurred (`relation_code`), what color blocks were involved (`source_code`, `target_code`) 

Beyond serving as a container for state and image data, the `Pair` class also handles key functionalities. These including loading data from file, generating commands from the templates, showing debugging images, image re-sizing, and augmentation. 
A key function is the `infer_from_stacksequence` function, which infers source and target colors from a StackSequence object. 

The `annotate_data.py` file also contains a function called `get_pairs()`. This function gets pairs, either from the SpotQ trials, or from the `main.py:main` function, which interacts with the simulator. It serves as an endpoint for interacting with the simulator. 


## Blocks with Logos 
The data is organized into json lines format, with a single line representing a trajectory. The instructions are in the "notes" field, and can be sorted via their "start" and "end" fields. The "states" field contains the (x, y, z ) coordinates of the blocks.

To load the data into the simulator, follow the instructions in the README to install and set up the CoppeliaSim environment. Then `generate_logoblocks_images.py` can be used to generate top-down images as well as scene captures, and to load the data into the sim. For convenience, we include the generated images in `blocks_data/generated_images` 

All data loading is handled by the `DatasetReader` class in `data.py`, which handles reading and constructing trajectories through the `SimpleTrajectory` class. 
