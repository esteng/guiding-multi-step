from itertools import combinations
import yaml 
import pathlib
import sys

def read_config(path):
    with open(path) as f1:
        return yaml.load(f1) 



def write_config(config, path):
    with open(path, "w") as f1:
        yaml.dump(config, f1) 


def enumerate_configs(original_path, out_path, n_colors=2):
    colors = ['blue','red','green','yellow','gray','orange','purple','brown']
    #colors = ['blue','red','green', 'yellow']
    combos = combinations(colors,n_colors) 
    original_config = read_config(original_path) 
    for combo in combos:
        original_config['color_pair'] = ",".join(combo) 
        write_config(original_config, out_path.joinpath("".join(combo) + ".yaml"))


if __name__ == "__main__":
    original_config = sys.argv[1]
    out_path = sys.argv[2]
    n_colors = int(sys.argv[3])


    enumerate_configs(pathlib.Path(original_config), pathlib.Path(out_path), n_colors)
