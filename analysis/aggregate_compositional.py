import pathlib 
import sys
from itertools import combinations 
import json
from collections import defaultdict
import numpy as np

if __name__ == "__main__":
    output_dir = pathlib.Path(sys.argv[1])
    n_colors = int(sys.argv[2])

    colors = ['blue','red','green','yellow','gray','orange','purple','brown']
    #colors = ['blue','red','green','yellow']
    combos = combinations(colors,n_colors)
    aggregate_dict = defaultdict(list) 
    for c in combos:
        try:
            path_to_read = output_dir.joinpath("".join(c)).joinpath("test_metrics.json")
            with open(path_to_read) as f1:
                data = json.load(f1) 
                for k, v in data.items():
                    aggregate_dict[k].append(v)     
        except FileNotFoundError:
            print(f"Not found: {path_to_read}") 


    aggregate_dict = {k:np.mean(v) for k,v in aggregate_dict.items()} 
    print(aggregate_dict)  
